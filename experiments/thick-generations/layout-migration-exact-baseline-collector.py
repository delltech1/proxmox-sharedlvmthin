#!/usr/bin/env python3
"""Unshipped root-only local baseline collector. No transport/effect CLI.

Reuses the existing zero-consumer collector inside a bounded isolated child.
Any service override/drop-in or unsupported vendor unit is a refusal. This is
deliberately a smaller supported surface, not an assertion of universal PVE
unit support. Kernel proc/sysfs reads remain the existing collector's boundary.
"""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import selectors
import signal
import socket
import stat
import subprocess
import time

HERE = Path(__file__).resolve().parent


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


M = load("exact_collector_model", "layout-migration-exact-restore-model.py")
C = load("exact_collector_legacy", "layout-migration-barrier-collector.py")
J = load("exact_collector_journal", "layout-migration-exact-local-journal.py")
MAX_OUTPUT = 8 * 1024 * 1024
Refusal, need = M.Refusal, M.need
SOURCE_FIELDS = ("Id", "LoadState", "FragmentPath", "DropInPaths", "SourcePath",
                 "UnitFileState", "Transient", "NeedDaemonReload")
STOCK_UNIT_STATES = {"enabled", "disabled", "static", "indirect"}


def parse_source_properties(raw, unit):
    need(unit in M.UNITS and type(raw) is bytes and 0 < len(raw) <= 65536
         and raw.endswith(b"\n") and all(byte == 10 or 32 <= byte <= 126 for byte in raw),
         "source property framing")
    result = {}
    for line in raw.decode("ascii").splitlines():
        key, separator, value = line.partition("=")
        need(separator and key in SOURCE_FIELDS and key not in result, "source property duplicate/unknown")
        result[key] = value
    need(set(result) == set(SOURCE_FIELDS), "source properties missing (empty is not absent)")
    need(result["Id"] == unit and result["Transient"] == "no" and result["NeedDaemonReload"] == "no"
         and result["SourcePath"] == "" and result["DropInPaths"] == "",
         "transient/generated/drop-in/dirty unit source")
    need(result["LoadState"] in ("loaded", "masked") and result["FragmentPath"] != ""
         and result["UnitFileState"] != "", "empty/unknown effective unit source")
    return result


def source_properties(unit):
    need(type(unit) is str and unit in M.UNITS, "unit outside fixed source set")
    argv = ["/usr/bin/systemctl", "show", "--all", "--no-pager",
            *["--property=" + field for field in SOURCE_FIELDS], "--", unit]
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=10, check=False,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "LANG": "C"})
    need(result.returncode == 0 and result.stderr == b"", "source property probe failed/ambiguous")
    return parse_source_properties(result.stdout, unit)


def validate_source(properties, unit, persistent_mask, alias=None):
    """Only exact stock fragment or the observed persistent /dev/null mask."""
    if alias is not None:
        need(type(alias) is dict and set(alias) == {"path", "target", "identity"}
             and alias["path"] == "/lib" and alias["target"] in ("usr/lib", "/usr/lib")
             and type(alias["identity"]) is list and len(alias["identity"]) == 9
             and all(type(v) is int and v >= 0 for v in alias["identity"])
             and alias["identity"][1] > 0 and stat.S_ISLNK(alias["identity"][2])
             and alias["identity"][3] == 0 and alias["identity"][5] == 1, "usrmerge alias proof malformed")
    if persistent_mask:
        need(properties["LoadState"] == "masked" and properties["UnitFileState"] == "masked"
             and properties["FragmentPath"] == "/dev/null", "persistent mask/effective source mismatch")
    else:
        need(properties["LoadState"] == "loaded" and properties["UnitFileState"] in STOCK_UNIT_STATES,
             "nonstock unit-file state")
        accepted = {"/usr/lib/systemd/system/" + unit}
        if alias is not None: accepted.add("/lib/systemd/system/" + unit)
        need(properties["FragmentPath"] in accepted, "foreign/generated/control unit fragment")


def stock_lib_alias(pins):
    """Explicit usrmerge alias exception, never arbitrary symlink traversal."""
    root = pins.directory("/")
    before = os.stat("lib", dir_fd=root, follow_symlinks=False)
    need(stat.S_ISLNK(before.st_mode) and before.st_uid == 0 and before.st_nlink == 1,
         "legacy /lib fragment is not a root-owned usrmerge alias")
    target = os.readlink("lib", dir_fd=root)
    need(target in ("usr/lib", "/usr/lib") and J.identity(before)
         == J.identity(os.stat("lib", dir_fd=root, follow_symlinks=False)), "usrmerge alias changed/foreign")
    pins.check()
    return {"path": "/lib", "target": target, "identity": list(J.identity(before))}


class Pins:
    def __init__(self): self.fds = []
    def directory(self, path):
        path = Path(path)
        need(path.is_absolute() and ".." not in path.parts, "unsafe fixed path")
        parent = None
        for name in ("/",) + path.parts[1:]:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            self.fds.append((parent, name, fd, os.fstat(fd)))
            parent = fd
        self.check()
        return parent
    def check(self):
        for parent, name, fd, original in self.fds:
            for value in (os.fstat(fd), os.stat(name, dir_fd=parent, follow_symlinks=False)):
                need(stat.S_ISDIR(value.st_mode) and value.st_uid == 0 and not value.st_mode & 0o022
                     and (value.st_dev, value.st_ino) == (original.st_dev, original.st_ino), "directory identity drift")
    def close(self):
        for _, _, fd, _ in reversed(self.fds): os.close(fd)
        self.fds = []


def read_regular(directory, name, maximum=4 * 1024 * 1024):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
    try:
        initial = os.fstat(fd)
        need(stat.S_ISREG(initial.st_mode) and initial.st_uid == 0 and initial.st_nlink == 1
             and not initial.st_mode & 0o022 and 0 < initial.st_size <= maximum, "unsafe source file")
        raw = bytearray()
        while len(raw) <= maximum:
            chunk = os.read(fd, min(65536, maximum + 1 - len(raw)))
            if not chunk: break
            raw.extend(chunk)
        need(len(raw) == initial.st_size and J.identity(initial) == J.identity(os.fstat(fd))
             == J.identity(os.stat(name, dir_fd=directory, follow_symlinks=False)), "source changed during read")
        return bytes(raw), list(J.identity(initial))
    finally: os.close(fd)


def namespace(pins):
    directories = {name: pins.directory(name) for name in
        ("/etc/systemd/system", "/run/systemd/system", "/usr/lib/systemd/system")}
    records = {}
    for path, fd in directories.items():
        for name in ("service.d",) + tuple(unit + ".d" for unit in M.UNITS):
            try: os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError: continue
            raise Refusal("service drop-in requires separately qualified support")
        for unit in M.UNITS:
            try: info = os.stat(unit, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                need(path != "/usr/lib/systemd/system", "vendor unit missing")
                records[path + "/" + unit] = None
                continue
            if path == "/etc/systemd/system":
                need(stat.S_ISLNK(info.st_mode) and info.st_uid == 0 and os.readlink(unit, dir_fd=fd) == "/dev/null",
                     "local override is not exact persistent mask")
                need(J.identity(info) == J.identity(os.stat(unit, dir_fd=fd, follow_symlinks=False)), "mask inode changed")
                records[path + "/" + unit] = {"mask_identity": list(J.identity(info)), "target": "/dev/null"}
            else:
                need(path == "/usr/lib/systemd/system", "runtime override present")
                raw, identity = read_regular(fd, unit)
                records[path + "/" + unit] = {"identity": identity, "sha256": hashlib.sha256(raw).hexdigest()}
    for unit in M.UNITS:
        before = source_properties(unit)
        alias = stock_lib_alias(pins) if before["FragmentPath"] == "/lib/systemd/system/" + unit else None
        mask = records["/etc/systemd/system/" + unit] is not None
        validate_source(before, unit, mask, alias)
        # Reopen the precise pinned vendor inode between the two effective
        # source reads. No caller-provided FragmentPath is opened.
        raw, identity = read_regular(directories["/usr/lib/systemd/system"], unit)
        vendor = records["/usr/lib/systemd/system/" + unit]
        need(vendor == {"identity": identity, "sha256": hashlib.sha256(raw).hexdigest()}, "vendor inode changed")
        need(source_properties(unit) == before, "effective source changed during pin")
        if alias is not None:
            need(stock_lib_alias(pins) == alias, "usrmerge alias changed during pin")
        records["effective:" + unit] = {"properties": before, "usrmerge_alias": alias}
    pins.check()
    return records


def legacy_plan(plan):
    names = [r["node"] for r in plan["participants"]]
    return {"schema": "slt-maintenance-barrier-model/v1", "tx": plan["tx"],
        "participants": [{"node": r["node"], "boot_id": r["boot_id"], "san_role": r["role"] == "SAN"}
                         for r in plan["participants"]],
        "workload_snapshot_sha256": plan["workload_sha256"], "storage_cfg_sha256": plan["storage_cfg_sha256"],
        "candidate_sha256": plan["candidate_sha256"], "issued_at": plan["issued_at"], "deadline": plan["expires_at"],
        "actions": [{"step": step, "node": node} for step in ("ENTRY_BLOCK", "SERVICE_DRAIN", "FINAL_AUDIT") for node in names]}


def project(plan, raw, package_sha256):
    node = raw["observation"]["node"]
    scoped = C.B.scoped_observation(raw, legacy_plan(plan), node)
    observation = {"node": node, "boot_id": scoped["boot_id"], "membership": scoped["membership"],
        "quorate": scoped["quorate"], "storage_cfg_sha256": scoped["storage_cfg_sha256"],
        "workload_sha256": scoped["workload_snapshot_sha256"], "package_sha256": package_sha256,
        "consumers": scoped["running_guests"], "workers": scoped["workers"], "tasks": scoped["tasks"],
        "start_demands": scoped["pending_jobs"], "services": {r["unit"]: {
            "active": r["active_state"] == "active", "substate": r["sub_state"],
            "masked": r["mask_target"] is not None, "job": r["job"], "control_pid": r["control_pid"]}
            for r in raw["services"]}}
    # Validate using the exact model schema, without supplying a clock/backend.
    validator = object.__new__(M.Coordinator); validator.plan = plan
    validator.validate_record(observation, node, before=True)
    return observation


def _collect(plan, workload):
    node = socket.gethostname()
    need(node in [r["node"] for r in plan["participants"]], "local node not an exact participant")
    pins = Pins()
    try:
        pins.directory("/etc/pve")
        for row in plan["participants"]:
            for kind in ("qemu-server", "lxc"):
                pins.directory("/etc/pve/nodes/" + row["node"] + "/" + kind)
        package_dir = pins.directory("/usr/share/pve-sharedlvmthin")
        artifact, artifact_identity = read_regular(package_dir, "package-artifact-sha256", 65)
        need(len(artifact) == 65 and artifact[-1:] == b"\n" and M.sha(artifact[:-1].decode("ascii")), "artifact marker invalid")
        before = namespace(pins)
        raw = C.collect(legacy_plan(plan), workload)
        first = project(plan, raw, artifact[:-1].decode("ascii"))
        second_raw = C.collect(legacy_plan(plan), workload)
        need(first == project(plan, second_raw, artifact[:-1].decode("ascii")), "baseline changed between collections")
        need(namespace(pins) == before, "service namespace changed")
        need(read_regular(package_dir, "package-artifact-sha256", 65) == (artifact, artifact_identity), "package identity changed")
        pins.check()
        return {"schema": "slt-exact-local-baseline/v1", "authority": "NONE", "mutation_performed": False,
                "tx": plan["tx"], "plan_sha256": M.digest(plan), "observation": first,
                "service_namespace": before, "artifact_identity": artifact_identity,
                "source_evidence_sha256": [M.digest(raw), M.digest(second_raw)]}
    finally: pins.close()


def collect_local(plan, workload, *, timeout=120):
    need(os.getuid() == os.geteuid() == 0, "root required")
    need(type(timeout) is int and 1 <= timeout <= 120, "collector timeout outside bound")
    M.validate_plan(plan, int(time.time()))
    read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            os.setsid()
            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
            resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout))
            value = _collect(copy.deepcopy(plan), copy.deepcopy(workload))
            raw = J.canonical(value) + b"\n"
            need(len(raw) <= MAX_OUTPUT, "collector output budget")
            while raw:
                count = os.write(write_fd, raw); need(count > 0, "collector pipe short write"); raw = raw[count:]
            os._exit(0)
        except BaseException: os._exit(2)
    os.close(write_fd)
    selector = selectors.DefaultSelector(); selector.register(read_fd, selectors.EVENT_READ)
    deadline, output = time.monotonic() + timeout, bytearray()
    try:
        while True:
            remaining = deadline - time.monotonic()
            need(remaining > 0 and selector.select(remaining), "collector deadline expired")
            block = os.read(read_fd, 65536)
            if not block: break
            output.extend(block); need(len(output) <= MAX_OUTPUT, "collector output exceeded bound")
        # PID remains unreaped until cleanup, so its process-group ID cannot
        # be recycled while the parent kills leftover read-only probe children.
        try: os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError: pass
        _, status = os.waitpid(pid, 0); pid = None
        need(os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, "collector failed or ambiguous")
        value = J.decode(bytes(output))
        M.validate_plan(plan, int(time.time()))
        return value
    finally:
        selector.close(); os.close(read_fd)
        if pid is not None:
            try: os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                try: os.kill(pid, signal.SIGKILL)
                except ProcessLookupError: pass
            os.waitpid(pid, 0)


def capture_once(journal, plan, workload):
    """Persist local evidence only; no claim of a completed four-node barrier."""
    value = collect_local(plan, workload)
    key = "node-baseline:" + value["observation"]["node"]
    ack = journal.create_once(plan["tx"], key, value)
    return {"authority": "NONE", "state": "LOCAL_BASELINE_RECORDED", "record": value, "ack": ack,
            "cluster_held": False, "release_authorized": False}
