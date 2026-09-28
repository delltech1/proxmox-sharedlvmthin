#!/usr/bin/python3
"""Inert systemd InvocationID/cgroup qualification; never touches storage."""

import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import uuid


ACK = "DISPOSABLE-NONSTORAGE-SYSTEMD-LAB"
PARENT = pathlib.Path("/var/tmp")
PREFIX = "slt-executor-systemd-lab-"
HEX32 = re.compile(r"^[a-f0-9]{32}$")
UNIT_PREFIX = "slt-thick-lab-exec-"


def atomic_json(root, name, value):
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    temporary = f".{name}.tmp.{uuid.uuid4().hex}"
    try:
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600, dir_fd=root_fd,
        )
        try:
            payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
            offset = 0
            while offset < len(payload):
                offset += os.write(fd, payload[offset:])
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        os.fsync(root_fd)
    finally:
        os.close(root_fd)


def exclusive_json(root, name, value):
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        temporary = f".{name}.tmp.{uuid.uuid4().hex}"
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600, dir_fd=root_fd,
        )
        try:
            payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
            offset = 0
            while offset < len(payload):
                offset += os.write(fd, payload[offset:])
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(temporary, name, src_dir_fd=root_fd, dst_dir_fd=root_fd,
                    follow_symlinks=False)
        finally:
            os.unlink(temporary, dir_fd=root_fd)
        os.fsync(root_fd)
    finally:
        os.close(root_fd)


def read_json(root, name):
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root_fd)
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                fd = -1
                return json.load(stream)
        finally:
            if fd >= 0:
                os.close(fd)
    finally:
        os.close(root_fd)


def runner(script, root, attempt, run_nonce, mode):
    invocation = os.environ.get("INVOCATION_ID", "")
    if not HEX32.fullmatch(invocation) or not HEX32.fullmatch(attempt) \
            or not HEX32.fullmatch(run_nonce):
        raise RuntimeError("runner identity is invalid")
    boot_id = pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    start_ticks = pathlib.Path("/proc/self/stat").read_text().split()[21]
    runner_digest = hashlib.sha256(script.read_bytes()).hexdigest()
    report = {
        "attempt": attempt,
        "invocation_id": invocation,
        "pid": os.getpid(),
        "boot_id": boot_id,
        "cgroup": pathlib.Path("/proc/self/cgroup").read_text().strip(),
        "mode": mode, "run_nonce": run_nonce, "start_ticks": start_ticks,
        "runner_sha256": runner_digest,
    }
    atomic_json(root, "runner.json", report)
    deadline = time.monotonic() + 30
    grant = None
    while time.monotonic() < deadline:
        try:
            grant = read_json(root, "grant.json")
        except FileNotFoundError:
            time.sleep(0.05)
            continue
        break
    if grant != {
        "attempt": attempt, "run_nonce": run_nonce,
        "invocation_id": invocation,
        "unit": f"{UNIT_PREFIX}{attempt}.service",
        "boot_id": boot_id, "pid": os.getpid(), "start_ticks": start_ticks,
        "runner_sha256": runner_digest,
    }:
        received_digest = hashlib.sha256(
            (json.dumps(grant, sort_keys=True, separators=(",", ":")) + "\n").encode()
        ).hexdigest()
        exclusive_json(root, "refusal.json", {
            "reason": "EXACT_GRANT_MISMATCH", "attempt": attempt,
            "run_nonce": run_nonce, "invocation_id": invocation,
            "received_grant_sha256": received_digest,
        })
        return 42
    exclusive_json(root, "dispatch-marker.json", {
        "attempt": attempt, "run_nonce": run_nonce,
        "invocation_id": invocation, "pid": os.getpid(),
    })
    if mode == "child":
        child = os.fork()
        if child == 0:
            atomic_json(root, "child.json", {
                "pid": os.getpid(), "ppid": os.getppid(), "invocation_id": invocation,
                "cgroup": pathlib.Path("/proc/self/cgroup").read_text().strip(),
            })
            child_deadline = time.monotonic() + 20
            while time.monotonic() < child_deadline:
                if (root / "release-child").exists():
                    exclusive_json(root, "child-complete.json", {
                        "pid": os.getpid(), "invocation_id": invocation,
                    })
                    os._exit(0)
                time.sleep(0.05)
            os._exit(0)
    return 0


def show(unit):
    fields = (
        "InvocationID", "MainPID", "ControlGroup", "ActiveState", "SubState",
        "Result", "ExecMainCode", "ExecMainStatus", "Job", "Id", "Type",
        "ExitType", "Restart", "RemainAfterExit", "KillMode", "Delegate",
        "NoNewPrivileges", "PrivateDevices", "ProtectSystem", "ProtectHome",
        "ProtectControlGroups", "CapabilityBoundingSet", "TasksMax", "MemoryMax",
        "ReadWritePaths", "ExecStart", "LoadState",
    )
    result = subprocess.run(
        ["/usr/bin/systemctl", "show", unit, "--property=" + ",".join(fields)],
        check=True, capture_output=True, text=True, timeout=5,
    )
    values = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


def wait_for(predicate, deadline, description):
    while time.monotonic() < deadline:
        value = predicate()
        if value is not None:
            return value
        time.sleep(0.05)
    raise RuntimeError(f"timed out waiting for {description}")


def validate_root(root):
    root = pathlib.Path(root)
    if not root.is_absolute() or root.parent != PARENT or not root.name.startswith(PREFIX):
        raise RuntimeError("root must be a direct /var/tmp/slt-executor-systemd-lab-* path")
    return root


def require_unit_absent(unit):
    result = subprocess.run(
        ["/usr/bin/systemctl", "show", unit, "--property=LoadState", "--value"],
        check=False, capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 0 or result.stdout.strip() != "not-found":
        raise RuntimeError("exact disposable lab unit absence is not positively proven")


def qualify(script, root, mode):
    root = validate_root(root)
    parent_fd = os.open(PARENT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.mkdir(root.name, 0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    attempt = uuid.uuid4().hex
    run_nonce = uuid.uuid4().hex
    unit = f"{UNIT_PREFIX}{attempt}.service"
    require_unit_absent(unit)
    command = [
        "/usr/bin/systemd-run", "--quiet", f"--unit={unit}", "--service-type=exec",
        "--property=ExitType=cgroup", "--property=Restart=no",
        "--property=RemainAfterExit=yes", "--property=KillMode=control-group",
        "--property=NoNewPrivileges=yes", "--property=PrivateDevices=yes",
        "--property=ProtectSystem=strict", "--property=ProtectHome=yes",
        "--property=ProtectControlGroups=yes", f"--property=ReadWritePaths={root}",
        "--property=Delegate=no", "--property=CapabilityBoundingSet=",
        "--property=TasksMax=16", "--property=MemoryMax=64M",
        "--property=StandardOutput=null", "--property=StandardError=null",
        "--property=TimeoutStopSec=10s", "/usr/bin/python3", "-I", "-B",
        str(script), "--root", str(root), "--attempt", attempt,
        "--run-nonce", run_nonce, "--mode", mode, "runner",
    ]
    runner_digest = hashlib.sha256(script.read_bytes()).hexdigest()
    evidence = {
        "unit": unit, "attempt": attempt, "run_nonce": run_nonce,
        "mode": mode, "command": command, "runner_sha256": runner_digest,
    }
    owned_unit = False

    def cleanup_owned():
        nonlocal owned_unit
        current = show(unit)
        exec_start = current.get("ExecStart", "")
        if current.get("Id") != unit or current.get("InvocationID") \
                != evidence.get("runner", {}).get("invocation_id") \
                or str(script) not in exec_start or str(root) not in exec_start \
                or run_nonce not in exec_start or attempt not in exec_start:
            raise RuntimeError("exact lab unit ownership is not proven; cleanup refused")
        stopped = subprocess.run(
            ["/usr/bin/systemctl", "stop", unit], check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
        )
        if stopped.returncode != 0:
            raise RuntimeError("exact lab unit cleanup failed")
        post_stop = show(unit)
        reset_returncode = None
        if post_stop.get("ActiveState") == "failed":
            if post_stop.get("Id") != unit or post_stop.get("InvocationID") \
                    != evidence["runner"]["invocation_id"]:
                raise RuntimeError("failed lab unit lifecycle changed before reset")
            reset = subprocess.run(
                ["/usr/bin/systemctl", "reset-failed", unit], check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
            )
            reset_returncode = reset.returncode
            if reset.returncode != 0:
                raise RuntimeError("exact failed lab unit reset failed")
            post_stop = show(unit)
        recognized_inactive = post_stop.get("LoadState") == "not-found" or (
            post_stop.get("LoadState") == "loaded"
            and post_stop.get("ActiveState") == "inactive"
            and post_stop.get("SubState") == "dead"
        )
        if not recognized_inactive or post_stop.get("Job") not in ("", "0"):
            raise RuntimeError("exact lab unit inactivity is not positively proven after cleanup")
        owned_unit = False
        return {
            "stop_returncode": stopped.returncode, "ownership_rechecked": True,
            "reset_failed_returncode": reset_returncode, "post_stop": post_stop,
        }
    try:
        subprocess.run(command, check=True, timeout=10)
        deadline = time.monotonic() + 15
        report = wait_for(
            lambda: read_json(root, "runner.json") if (root / "runner.json").exists() else None,
            deadline, "runner identity report",
        )
        manager = show(unit)
        expected_properties = {
            "Id": unit, "Type": "exec", "ExitType": "cgroup", "Restart": "no",
            "RemainAfterExit": "yes", "KillMode": "control-group", "Delegate": "no",
            "NoNewPrivileges": "yes", "PrivateDevices": "yes",
            "ProtectSystem": "strict", "ProtectHome": "yes",
            "ProtectControlGroups": "yes", "TasksMax": "16", "MemoryMax": "67108864",
            "CapabilityBoundingSet": "", "ReadWritePaths": str(root),
        }
        if any(manager.get(key) != value for key, value in expected_properties.items()):
            raise RuntimeError("effective systemd properties do not match the qualification contract")
        if report["attempt"] != attempt or report["invocation_id"] != manager.get("InvocationID"):
            raise RuntimeError("runner and manager invocation identity disagree")
        if str(report["pid"]) != manager.get("MainPID"):
            raise RuntimeError("runner and manager MainPID disagree")
        proc_cgroup = pathlib.Path(f"/proc/{report['pid']}/cgroup").read_text().strip()
        unified = [line[3:] for line in proc_cgroup.splitlines() if line.startswith("0::")]
        if proc_cgroup != report["cgroup"] or len(unified) != 1 \
                or not manager.get("ControlGroup") or unified[0] != manager["ControlGroup"]:
            raise RuntimeError("runner cgroup identity is not independently proven")
        cgroup_relative = manager["ControlGroup"]
        cgroup_path = pathlib.Path("/sys/fs/cgroup" + cgroup_relative)
        if report.get("run_nonce") != run_nonce or report.get("boot_id") \
                != pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip() \
                or report.get("start_ticks") != pathlib.Path(f"/proc/{report['pid']}/stat").read_text().split()[21] \
                or report.get("runner_sha256") != runner_digest:
            raise RuntimeError("runner nonce, boot, start-time or code identity mismatch")
        evidence["runner"] = report
        evidence["initial_manager"] = manager
        owned_unit = True
        if (root / "dispatch-marker.json").exists():
            raise RuntimeError("runner dispatched before receiving its exact grant")
        grant = {
            "attempt": attempt, "run_nonce": run_nonce,
            "invocation_id": report["invocation_id"], "unit": unit,
            "boot_id": report["boot_id"], "pid": report["pid"],
            "start_ticks": report["start_ticks"], "runner_sha256": runner_digest,
        }
        if mode == "invalid":
            grant["invocation_id"] = "0" * 32
        grant_digest = hashlib.sha256(
            (json.dumps(grant, sort_keys=True, separators=(",", ":")) + "\n").encode()
        ).hexdigest()
        atomic_json(root, "grant.json", grant)
        if mode == "invalid":
            failed = wait_for(
                lambda: (state if (state := show(unit)).get("Result") not in ("", "success")
                         and state.get("SubState") in ("failed", "dead", "exited") else None),
                time.monotonic() + 15, "invalid-grant runner refusal",
            )
            refusal = wait_for(
                lambda: read_json(root, "refusal.json") if (root / "refusal.json").exists() else None,
                time.monotonic() + 15, "exact invalid-grant refusal evidence",
            )
            if refusal != {
                "reason": "EXACT_GRANT_MISMATCH", "attempt": attempt,
                "run_nonce": run_nonce, "invocation_id": report["invocation_id"],
                "received_grant_sha256": grant_digest,
            } or failed.get("InvocationID") != report["invocation_id"] \
                    or failed.get("ExecMainStatus") != "42" or failed.get("Job") not in ("", "0"):
                raise RuntimeError("invalid grant did not produce the exact refusal lifecycle")
            if (root / "dispatch-marker.json").exists():
                raise RuntimeError("invalid grant created a dispatch marker")
            evidence.update({
                "runner": report, "invalid_grant": grant,
                "refusal": refusal, "terminal": failed,
            })
            cleanup = cleanup_owned()
            evidence["cleanup"] = cleanup
            atomic_json(root, "evidence.json", evidence)
            print(json.dumps({
                "kind": "executor-systemd-inert-qualification", "result": "PASS",
                "scenario": "INVALID_GRANT_REFUSED", "unit": unit,
                "limitations": ["no admission backend or storage I/O was exercised"],
            }, sort_keys=True, indent=2))
            return
        marker = wait_for(
            lambda: read_json(root, "dispatch-marker.json")
            if (root / "dispatch-marker.json").exists() else None,
            deadline, "inert dispatch marker",
        )
        if marker["attempt"] != attempt or marker["invocation_id"] != report["invocation_id"]:
            raise RuntimeError("dispatch marker identity mismatch")
        child = None
        if mode == "child":
            child = wait_for(
                lambda: read_json(root, "child.json") if (root / "child.json").exists() else None,
                deadline, "controlled child report",
            )
            during_child = show(unit)
            if during_child.get("ControlGroup") != cgroup_relative:
                raise RuntimeError("controlled child lifecycle changed cgroup identity")
            procs = cgroup_path.joinpath("cgroup.procs").read_text().split()
            if str(child["pid"]) not in procs:
                raise RuntimeError("controlled child is absent from the exact unit cgroup")
            wait_for(
                lambda: True if not pathlib.Path(f"/proc/{report['pid']}").exists() else None,
                deadline, "main runner exit while child remains",
            )
            after_parent = show(unit)
            procs = cgroup_path.joinpath("cgroup.procs").read_text().split()
            if procs != [str(child["pid"])] or after_parent.get("InvocationID") \
                    != report["invocation_id"] or after_parent.get("SubState") == "exited":
                raise RuntimeError("parent exit did not leave the exact controlled child alone")
            if during_child.get("SubState") == "exited":
                raise RuntimeError("unit became terminal while its controlled child remained")
            evidence["during_child"] = during_child
            evidence["after_parent"] = after_parent
            evidence["child"] = child
            (root / "release-child").touch(mode=0o600, exist_ok=False)
            child_complete = wait_for(
                lambda: read_json(root, "child-complete.json")
                if (root / "child-complete.json").exists() else None,
                deadline, "controlled child completion",
            )
            evidence["child_complete"] = child_complete
            if child_complete.get("pid") != child.get("pid") \
                    or child_complete.get("invocation_id") != report["invocation_id"]:
                raise RuntimeError("controlled child completion identity mismatch")
        terminal = wait_for(
            lambda: (state if (state := show(unit)).get("SubState") == "exited" else None),
            time.monotonic() + 15, "retained terminal unit state",
        )
        if terminal.get("Result") != "success" or terminal.get("ExecMainStatus") != "0":
            raise RuntimeError("retained unit terminal result is not exact success")
        if terminal.get("InvocationID") != report["invocation_id"] \
                or terminal.get("Job") not in ("", "0") \
                or terminal.get("MainPID") not in ("", "0"):
            raise RuntimeError("terminal invocation or pending-job proof is incomplete")
        if pathlib.Path(f"/proc/{report['pid']}").exists() \
                or (child is not None and pathlib.Path(f"/proc/{child['pid']}").exists()):
            raise RuntimeError("recorded executor identity still exists at terminal classification")
        if cgroup_path.exists():
            terminal_procs = cgroup_path.joinpath("cgroup.procs").read_text().split()
            terminal_events = dict(
                line.split(maxsplit=1)
                for line in cgroup_path.joinpath("cgroup.events").read_text().splitlines()
            )
            if terminal_procs or terminal_events.get("populated") != "0":
                raise RuntimeError("retained terminal cgroup is not empty")
            cgroup_terminal = {"classification": "DIRECT_EMPTY", "path": cgroup_relative}
        else:
            if terminal.get("ControlGroup") not in ("", cgroup_relative):
                raise RuntimeError("terminal service reports a different cgroup after pruning")
            cgroup_terminal = {
                "classification": "PRUNED_AFTER_SAME_LIFECYCLE_TERMINAL",
                "path": cgroup_relative,
            }
        evidence["cgroup_terminal"] = cgroup_terminal
        evidence.update({"runner": report, "marker": marker, "terminal": terminal})
        cleanup = cleanup_owned()
        evidence["cleanup"] = cleanup
        atomic_json(root, "evidence.json", evidence)
        print(json.dumps({
            "kind": "executor-systemd-inert-qualification", "result": "PASS",
            "unit": unit, "attempt": attempt, "mode": mode,
            "limitations": [
                "dispatch is an inert marker file, not storage I/O",
                "no Perl admission backend, reboot, power loss or cross-node behavior was tested",
            ],
        }, sort_keys=True, indent=2))
    except Exception as error:
        try:
            atomic_json(root, "failure.json", {
                "error": str(error), "unit": unit, "attempt": attempt,
                "run_nonce": run_nonce, "evidence": evidence,
            })
        except Exception:
            pass
        raise
    finally:
        if owned_unit:
            try:
                cleanup_owned()
            except Exception as error:
                atomic_json(root, "cleanup-refused.json", {"error": str(error), "unit": unit})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--attempt")
    parser.add_argument("--run-nonce")
    parser.add_argument("--mode", choices=("simple", "invalid", "child"), default="simple")
    parser.add_argument("--ack")
    parser.add_argument("action", choices=("qualify", "runner"))
    args = parser.parse_args()
    root = validate_root(args.root)
    if args.action == "runner":
        if os.geteuid() != 0 or not args.attempt or not args.run_nonce:
            raise SystemExit("runner requires root and exact attempt")
        raise SystemExit(runner(pathlib.Path(__file__).resolve(strict=True), root,
                                args.attempt, args.run_nonce, args.mode))
    if args.ack != ACK or os.geteuid() != 0:
        raise SystemExit("qualification requires root and explicit disposable acknowledgement")
    qualify(pathlib.Path(__file__).resolve(strict=True), root, args.mode)


if __name__ == "__main__":
    main()
