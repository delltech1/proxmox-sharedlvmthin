#!/usr/bin/env python3
"""Closed node-local maintenance-barrier runner.

The CLI accepts canonical plan, workload and request documents only. It has
no generic command, retry, release, unmask or service-start operation.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = load("barrier_runner_backend", "layout-migration-barrier-backend.py")
C = load("barrier_runner_collector", "layout-migration-barrier-collector.py")
L = load("barrier_runner_latch", "layout-migration-barrier-node-latch.py")
Refusal, require = B.Refusal, B.require
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}
COMMANDS = {
    ("/usr/bin/systemctl", "mask", "--", *sorted(B.MODEL.ENTRY_UNITS)),
    ("/usr/bin/systemctl", "stop", "--", *B.MODEL.TERMINAL_INGRESS),
    ("/usr/bin/systemctl", "mask", "--", *sorted(B.MODEL.REQUIRED_STOPS)),
    ("/usr/bin/systemctl", "stop", "--", *sorted(B.MODEL.REQUIRED_STOPS)),
}


def execute(argv):
    argv = tuple(argv)
    require(argv in COMMANDS, "command outside closed runner set")
    result = subprocess.run(list(argv), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=60, env=ENV, check=False)
    require(result.returncode == 0 and len(result.stdout) <= 1024 * 1024
            and len(result.stderr) <= 1024 * 1024,
            "systemctl result is not a confirmed success")
    return True


def observe_receipt(agent, request, *, inspect=L.inspect, clock=None):
    """Read completed local latches before a new, timed final observation.

    This adds no effect operation. Latch records remain historical; only the
    separately collected response describes current state. Transport and wall
    clock authenticity remain the coordinator's trust boundary.
    """
    clock = clock or (lambda: int(time.time()))
    require(request.get("operation") == "OBSERVE", "v2 receipt is observation-only")
    started = clock()
    require(type(started) is int and agent.plan["issued_at"] <= started <= agent.plan["deadline"],
            "observation start clock invalid")
    attempts = {}
    for operation in ("ENTRY_BLOCK", "SERVICE_DRAIN"):
        result = inspect({**request, "operation": operation})
        require(result.get("state") == "COMPLETED_HISTORICAL", "completed latch absent")
        attempts[operation] = result
    response = agent.request(request)
    B.MODEL.validate_observation(B.scoped_observation(response["evidence"], agent.plan, agent.node),
                                 agent.plan, agent.node, final=True)
    observed = clock()
    require(type(observed) is int and started <= observed <= agent.plan["deadline"],
            "observation completion clock invalid")
    return {"schema": "slt-barrier-node-runner/v2", "verdict": "COMPLETED",
            "started_at": started, "observed_at": observed,
            "attempts": attempts, "response": response}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--workload", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--receipt-v2", action="store_true",
                        help="OBSERVE only: include completed latch records and fresh collection times")
    args = parser.parse_args()
    try:
        plan = C.strict(C.read(args.plan))
        workload = C.strict(C.read(args.workload))
        request = C.strict(C.read(args.request, maximum=4096))
        require(L.canonical(request) == C.read(args.request, maximum=4096),
                "request is not canonical")
        node = socket.gethostname()
        require(request.get("node") == node, "request is for another node")
        collect = lambda: C.collect(plan, workload)
        agent = B.NodeAgent(plan, node, collect, execute, lambda: int(time.time()))
        if args.receipt_v2:
            result = observe_receipt(agent, request)
        else:
            response = (agent.request(request) if request.get("operation") == "OBSERVE"
                        else L.DurableNodeAgent(agent).request(request))
            result = {"schema": "slt-barrier-node-runner/v1",
                      "verdict": "COMPLETED", "response": response}
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, ValueError, KeyError, IndexError, TypeError, Refusal,
            C.V1.Refusal, subprocess.TimeoutExpired) as error:
        print(json.dumps({"schema": "slt-barrier-node-runner/v1",
                          "verdict": "BLOCKED", "reason": str(error)},
                         sort_keys=True, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    sys.exit(main())
