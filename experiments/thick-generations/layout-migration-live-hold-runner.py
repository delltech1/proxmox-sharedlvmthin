#!/usr/bin/env python3
"""Create one exact PREPARE hold at the fixed packaged maintenance root."""
import argparse
import importlib.util
import json
from pathlib import Path
import socket
import sys
import time

HERE = Path(__file__).resolve().parent
FIXED_ROOT = Path("/var/lib/pve-sharedlvmthin/maintenance")


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


W = load("live_hold_runner_writer", "layout-migration-live-hold-writer.py")


def strict(path, maximum=16 * 1024 * 1024):
    raw = Path(path).read_bytes()
    W.require(0 < len(raw) <= maximum, "input size invalid")
    def pairs(rows):
        value = {}
        for key, item in rows:
            W.require(key not in value, "duplicate JSON key")
            value[key] = item
        return value
    value = json.loads(raw, object_pairs_hook=pairs)
    W.require(raw == W.canonical(value) + b"\n", "input is not canonical")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "sidecar", "authorization", "facts"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    try:
        manifest, sidecar = strict(args.manifest), strict(args.sidecar)
        authorization, facts = strict(args.authorization, 64 * 1024), strict(args.facts, 64 * 1024)
        W.require(facts.get("node") == socket.gethostname(), "facts belong to another node")
        result = W.Writer(FIXED_ROOT).create_prepare_once(
            manifest, sidecar, authorization, facts, int(time.time()))
        print(json.dumps({"schema": "slt-live-hold-runner/v1", "verdict": "COMPLETED",
                          "result": result}, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, ValueError, TypeError, KeyError, W.Refusal) as error:
        print(json.dumps({"schema": "slt-live-hold-runner/v1", "verdict": "BLOCKED",
                          "reason": str(error)}, sort_keys=True, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    sys.exit(main())
