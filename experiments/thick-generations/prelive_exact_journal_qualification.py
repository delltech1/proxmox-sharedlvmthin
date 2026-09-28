#!/usr/bin/python3
"""One preserved file-only exact-journal qualification; no process or storage."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

sys.dont_write_bytecode = True

import prelive_exact_journal_file_backend as FILES
import prelive_fixture_journal_adapter as JOURNAL
import prelive_supervisor_model as SUP


ACK = "QUALIFY_FILE_ONLY_EXACT_JOURNAL_PRESERVE_ARTIFACTS"
TRUE = "/usr/bin/true"


def _starttime(pid):
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    close = raw.rfind(")")
    fields = raw[close + 2:].split()
    if close < 1 or len(fields) < 20 or not fields[19].isdigit():
        raise JOURNAL.Refusal("owner starttime unavailable")
    return int(fields[19])


def _file_identity(path):
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        raise JOURNAL.Refusal("fixture executable identity")
    data = Path(path).read_bytes()
    return {"path": path, "sha256": hashlib.sha256(data).hexdigest(),
            "dev": info.st_dev, "inode": info.st_ino}


def _events(owner, executable):
    request = {"schema": 1, "request_id": "c" * 32,
               "purpose": "DISPOSABLE_KERNEL_LAB",
               "boot_id": owner["boot_id"], "argv": [TRUE],
               "environment": dict(SUP.ENVIRONMENT),
               "executable": dict(executable), "launcher": dict(executable),
               "timeout_ms": 5000, "cleanup_ms": 1000,
               "capture_limit": 4096, "signal_policy": "NONE",
               "storage_authorized": False,
               "postcondition_verified": False}
    first = SUP.canonical({"schema": 1, "request_id": "c" * 32,
        "sequence": 1, "kind": "INTENT", "previous_digest": "0" * 64,
        "payload": {"request": request, "request_sha256": SUP.digest(request),
                    "owner": owner, "unarmed_deadline_ns": 5000000000,
                    "pre_fork_ticket_bound": True}})
    first_digest = hashlib.sha256(first).hexdigest()
    child = {"request_id": "c" * 32, "boot_id": owner["boot_id"],
             "pid": owner["pid"] + 1000000, "starttime": 1,
             "owner_pid": owner["pid"],
             "owner_starttime": owner["starttime"],
             "launcher_sha256": executable["sha256"], "armed": False}
    second = SUP.canonical({"schema": 1, "request_id": "c" * 32,
        "sequence": 2, "kind": "CHILD_BOUND",
        "previous_digest": first_digest,
        "payload": {"intent_digest": first_digest, "child": child,
                    "lifecycle_token": "d" * 32, "launcher": executable,
                    "channel_identity": {"token": "e" * 32,
                                         "writer_open": True,
                                         "reader_owned": True}}})
    second_digest = hashlib.sha256(second).hexdigest()
    third = SUP.canonical({"schema": 1, "request_id": "c" * 32,
        "sequence": 3, "kind": "EXEC_ISSUED",
        "previous_digest": second_digest,
        "payload": {"intent_digest": first_digest,
                    "child_bound_digest": second_digest,
                    "attempt_id": "f" * 32,
                    "grant_protocol": "ONE_BYTE_G_THEN_WRITER_EOF"}})
    return [first, second, third]


def _independent_verify(root, expected, persisted):
    names = [f"exact-event-{index:06d}.json" for index in range(1, 4)]
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                      | os.O_CLOEXEC)
    failure = None
    evidence = []
    try:
        info = os.fstat(root_fd)
        named_root = os.stat(root, follow_symlinks=False)
        root_expected = persisted["root_identity"]
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700
                or (info.st_dev, info.st_ino, info.st_uid,
                    stat.S_IMODE(info.st_mode))
                != (root_expected["dev"], root_expected["inode"],
                    root_expected["uid"], root_expected["mode"])
                or (info.st_dev, info.st_ino)
                != (named_root.st_dev, named_root.st_ino)
                or sorted(os.listdir(root_fd)) != names):
            raise JOURNAL.Refusal("independent directory verification")
        if len(persisted["records"]) != 3:
            raise JOURNAL.Refusal("persisted record evidence count")
        for name, raw, recorded in zip(names, expected, persisted["records"]):
            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
                         | os.O_CLOEXEC,
                         dir_fd=root_fd)
            record_failure = None
            try:
                current = os.fstat(fd)
                named = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                identity = recorded["record_identity"]
                expected_tuple = (identity["dev"], identity["inode"],
                                  identity["uid"], identity["mode"],
                                  identity["nlink"], len(raw))
                if (recorded["name"] != name
                        or recorded["sha256"] != hashlib.sha256(raw).hexdigest()
                        or not stat.S_ISREG(current.st_mode)
                        or (current.st_dev, current.st_ino, current.st_uid,
                            stat.S_IMODE(current.st_mode), current.st_nlink,
                            current.st_size) != expected_tuple
                        or (named.st_dev, named.st_ino, named.st_uid,
                            stat.S_IMODE(named.st_mode), named.st_nlink,
                            named.st_size) != expected_tuple):
                    raise JOURNAL.Refusal("record differs from persistence evidence")
                content = b""
                while len(content) < len(raw):
                    chunk = os.read(fd, len(raw) - len(content))
                    if not chunk: break
                    content += chunk
                extra = os.read(fd, 1)
                final_record = os.fstat(fd)
                final_named = os.stat(name, dir_fd=root_fd,
                                      follow_symlinks=False)
                if (content != raw or extra != b""
                        or (final_record.st_dev, final_record.st_ino,
                            final_record.st_uid, stat.S_IMODE(final_record.st_mode),
                            final_record.st_nlink, final_record.st_size)
                        != expected_tuple
                        or (final_named.st_dev, final_named.st_ino,
                            final_named.st_uid, stat.S_IMODE(final_named.st_mode),
                            final_named.st_nlink, final_named.st_size)
                        != expected_tuple):
                    raise JOURNAL.Refusal("independent record verification")
                evidence.append({"name": name, "bytes": len(raw),
                                 "sha256": hashlib.sha256(raw).hexdigest(),
                                 "dev": current.st_dev, "inode": current.st_ino})
            except BaseException as exc:
                record_failure = exc
            try:
                os.close(fd)
            except BaseException as exc:
                record_failure = record_failure or exc
            if record_failure is not None: raise record_failure
        final_root = os.fstat(root_fd)
        final_named = os.stat(root, follow_symlinks=False)
        final_expected = (root_expected["dev"], root_expected["inode"],
                          root_expected["uid"], root_expected["mode"])
        if (not stat.S_ISDIR(final_root.st_mode)
                or not stat.S_ISDIR(final_named.st_mode)
                or (final_root.st_dev, final_root.st_ino, final_root.st_uid,
                    stat.S_IMODE(final_root.st_mode)) != final_expected
                or (final_named.st_dev, final_named.st_ino, final_named.st_uid,
                    stat.S_IMODE(final_named.st_mode)) != final_expected
                or sorted(os.listdir(root_fd)) != names):
            raise JOURNAL.Refusal("independent final directory verification")
    except BaseException as exc:
        failure = exc
    try:
        os.close(root_fd)
    except BaseException as exc:
        failure = failure or exc
    if failure is not None: raise failure
    return evidence


def qualify(nonce):
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip().lower()
    owner = {"pid": os.getpid(), "starttime": _starttime(os.getpid()),
             "boot_id": boot}
    expected = _events(owner, _file_identity(TRUE))
    backend = FILES.create_fresh_file_backend(nonce)
    controller = object()
    try:
        session = JOURNAL.create_source_session("c" * 32, owner, backend)
        session.claim(controller)
        receipts = [session.append_exact_event(raw, controller)
                    for raw in expected]
        seal = session.seal_pregrant(controller)
        root = backend.artifact_path()
        persisted = backend.qualification_evidence()
        backend.close_preserving_artifacts()
        verified = _independent_verify(root, expected, persisted)
        return {"schema": 1,
                "classification": "FIXTURE_JOURNAL_EXACT_BYTES_AND_ORDER_PASS",
                "artifact_path": root, "events": verified,
                "receipt_digests": [item.digest for item in receipts],
                "seal_digest": seal.final_digest,
                "artifacts_preserved": True, "child_started": False,
                "grant_authorized": False, "storage_authorized": False,
                "power_loss_durability_proven": False}
    except BaseException:
        if backend.root_fd is not None or backend.parent_fd is not None:
            try: backend.release_descriptors_preserving_state()
            except BaseException: pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--ack", required=True)
    parser.add_argument("--nonce", required=True)
    args = parser.parse_args(argv)
    if args.ack != ACK:
        raise SystemExit("explicit file-only acknowledgement required")
    print(json.dumps(qualify(args.nonce), sort_keys=True))


if __name__ == "__main__":
    main()
