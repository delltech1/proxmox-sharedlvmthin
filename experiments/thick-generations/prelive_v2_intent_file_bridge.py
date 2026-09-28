#!/usr/bin/python3
"""Exact file-only V2 INTENT persistence; no fork, grant, exec or storage."""

import copy
import hashlib
import json
import os
import stat
import threading

import prelive_launcher_identity_consumer as CONSUMER
import prelive_exact_journal_file_backend as FILES
import prelive_supervisor_model as SUP


class Refusal(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise Refusal(message)


def _pairs(values):
    result = {}
    for key, value in values:
        require(type(key) is str and key not in result,
                "duplicate or non-string JSON key")
        result[key] = value
    return result


def _strict_event(raw):
    require(type(raw) is bytes and 0 < len(raw) <= 65536,
            "strict V2 INTENT bytes required")
    try:
        event = json.loads(raw.decode("ascii"), object_pairs_hook=_pairs,
                           parse_float=lambda value: (_ for _ in ()).throw(
                               Refusal("JSON floats are forbidden")),
                           parse_constant=lambda value: (_ for _ in ()).throw(
                               Refusal("JSON constants are forbidden")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refusal("canonical V2 INTENT JSON required") from exc
    CONSUMER._builtin_tree(event, "V2 INTENT")
    require(type(event) is dict and set(event) == {
                "schema", "request_id", "sequence", "kind",
                "previous_digest", "payload"}
            and type(event["schema"]) is int and event["schema"] == 2
            and type(event["sequence"]) is int and event["sequence"] == 1
            and type(event["kind"]) is str and event["kind"] == "INTENT"
            and type(event["previous_digest"]) is str
            and event["previous_digest"] == "0" * 64,
            "closed V2 INTENT event required")
    payload = event["payload"]
    require(type(payload) is dict and set(payload) == {
                "request", "request_digest", "descriptor_digest", "owner",
                "unarmed_deadline_ns", "fd_graph_digest", "enrollment_scope"},
            "closed V2 INTENT payload required")
    request = CONSUMER.validate_v2_request(payload["request"])
    owner = SUP.validate_owner(copy.deepcopy(payload["owner"]),
                               request["boot_id"])
    descriptor = request["launcher_identity"]
    require(event["request_id"] == request["request_id"]
            and type(payload["request_digest"]) is str
            and CONSUMER.IDENTITY.HEX64.fullmatch(payload["request_digest"])
            and payload["request_digest"] == SUP.digest(request)
            and type(payload["descriptor_digest"]) is str
            and CONSUMER.IDENTITY.HEX64.fullmatch(
                payload["descriptor_digest"])
            and payload["descriptor_digest"]
            == CONSUMER.IDENTITY.digest(descriptor)
            and owner == descriptor["run_binding"]["supervisor"]
            and type(payload["unarmed_deadline_ns"]) is int
            and payload["unarmed_deadline_ns"]
            == descriptor["run_binding"]["unarmed_deadline_ns"]
            and type(payload["fd_graph_digest"]) is str
            and CONSUMER.IDENTITY.HEX64.fullmatch(payload["fd_graph_digest"])
            and payload["fd_graph_digest"]
            == descriptor["run_binding"]["fd_graph_digest"]
            and type(payload["enrollment_scope"]) is str
            and payload["enrollment_scope"]
            == "MODEL_V2_IDENTITY_ENROLLMENT"
            and SUP.canonical(event) == raw,
            "V2 INTENT identity or canonical bytes changed")
    return event


class V2IntentFileBridge:
    """One owner-thread, one-record source bridge over an exact file backend."""
    source_exact_v2_intent_bridge = True

    def __init__(self, backend):
        require(type(backend) is FILES.ExactJournalFileBackend,
                "exact file backend required")
        self.backend = backend
        self._backend = backend
        require(type(FILES.PARENT) is str
                and type(backend.root_name) is str,
                "strict V2 INTENT artifact namespace required")
        self._parent = FILES.PARENT
        self._root_name = backend.root_name
        self._artifact_path = backend.artifact_path()
        require(type(self._artifact_path) is str,
                "strict V2 INTENT artifact path required")
        self._root_authority = self._strict_root_authority()
        self.owner_thread = threading.current_thread()
        self.state = "READY"
        self.receipt = None

    def __copy__(self):
        raise Refusal("V2 INTENT file bridge is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("V2 INTENT file bridge is noncopyable")

    def _poison(self):
        self.state = "UNKNOWN"

    def _strict_root_authority(self):
        require(self.backend is self._backend
                and type(self.backend) is FILES.ExactJournalFileBackend
                and type(FILES.PARENT) is str and FILES.PARENT == self._parent
                and type(self.backend.root_name) is str
                and self.backend.root_name == self._root_name
                and os.path.join(self._parent, self._root_name)
                == self._artifact_path,
                "V2 INTENT backend or namespace authority changed")
        root_identity = self.backend.root_identity
        CONSUMER._builtin_tree(root_identity, "V2 INTENT root identity")
        require(type(root_identity) is dict and set(root_identity) == {
                    "dev", "inode", "uid", "mode"}
                and all(type(root_identity[key]) is int
                        and root_identity[key] >= 0
                        for key in ("dev", "inode", "uid", "mode"))
                and root_identity["inode"] > 0
                and root_identity["mode"] == 0o700,
                "exact V2 INTENT root identity required")
        return tuple(root_identity[key] for key in
                     ("dev", "inode", "uid", "mode"))

    def persist_intent(self, raw):
        if (threading.current_thread() is not self.owner_thread
                or self.state != "READY" or self.receipt is not None):
            self._poison()
            raise Refusal("fresh owner V2 INTENT bridge required")
        try:
            event = _strict_event(raw)
            digest = hashlib.sha256(raw).hexdigest()
            require(self._strict_root_authority() == self._root_authority,
                    "V2 INTENT root authority changed before persistence")
            self.state = "PERSISTING"
            ack = self.backend.persist_exact_file(
                "exact-event-000001.json", raw)
            CONSUMER._builtin_tree(ack, "V2 INTENT file ACK")
            identity = ack.get("record_identity") if type(ack) is dict else None
            require(self.state == "PERSISTING"
                    and type(ack) is dict and set(ack) == {
                        "bytes_written", "file_synced", "dir_synced", "closed",
                        "name", "sha256", "record_identity"}
                    and type(ack["bytes_written"]) is int
                    and ack["bytes_written"] == len(raw)
                    and type(ack["file_synced"]) is bool
                    and ack["file_synced"] is True
                    and type(ack["dir_synced"]) is bool
                    and ack["dir_synced"] is True
                    and type(ack["closed"]) is bool
                    and ack["closed"] is True
                    and type(ack["name"]) is str
                    and ack["name"] == "exact-event-000001.json"
                    and type(ack["sha256"]) is str
                    and CONSUMER.IDENTITY.HEX64.fullmatch(ack["sha256"])
                    and ack["sha256"] == digest
                    and type(identity) is dict and set(identity) == {
                        "dev", "inode", "uid", "mode", "nlink"}
                    and all(type(identity[key]) is int and identity[key] >= 0
                            for key in ("dev", "inode", "uid"))
                    and identity["inode"] > 0
                    and type(identity["mode"]) is int
                    and identity["mode"] == 0o600
                    and type(identity["nlink"]) is int
                    and identity["nlink"] == 1,
                    "exact V2 INTENT file ACK required")
            record_authority = tuple(identity[key] for key in
                                     ("dev", "inode", "uid", "mode", "nlink"))
            require(self._strict_root_authority() == self._root_authority,
                    "V2 INTENT root authority changed during persistence")
            self._independent_reread(
                raw, record_authority, self._root_authority)
            require(self.state == "PERSISTING",
                    "V2 INTENT bridge changed during verification")
            require(self._strict_root_authority() == self._root_authority,
                    "V2 INTENT root authority changed after verification")
            frozen_record = dict(zip(
                ("dev", "inode", "uid", "mode", "nlink"),
                record_authority))
            self.receipt = {
                "schema": 1,
                "classification": "V2_INTENT_EXACT_BYTES_VERIFIED",
                "request_id": event["request_id"], "bytes": len(raw),
                "sha256": digest, "name": "exact-event-000001.json",
                "record_identity": frozen_record,
                "child_started": False, "grant_attempted": False,
                "runtime_authorized": False, "storage_authorized": False,
                "exec_proven": False,
                "power_loss_durability_proven": False}
            self.state = "SEALED_INTENT"
            return copy.deepcopy(self.receipt)
        except BaseException:
            self._poison()
            raise

    def _independent_reread(self, expected, record_authority, root_authority):
        root = self._artifact_path
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                          | os.O_CLOEXEC)
        record_fd = None
        failure = None
        try:
            root_before = os.fstat(root_fd)
            root_named = os.stat(root, follow_symlinks=False)
            require(stat.S_ISDIR(root_before.st_mode)
                    and (root_before.st_dev, root_before.st_ino,
                         root_before.st_uid, stat.S_IMODE(root_before.st_mode))
                    == root_authority
                    and (root_named.st_dev, root_named.st_ino,
                         root_named.st_uid, stat.S_IMODE(root_named.st_mode))
                    == root_authority,
                    "V2 INTENT root identity changed")
            record_fd = os.open(
                "exact-event-000001.json",
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=root_fd)
            before = os.fstat(record_fd)
            named = os.stat("exact-event-000001.json", dir_fd=root_fd,
                            follow_symlinks=False)
            expected_identity = record_authority + (len(expected),)
            require(stat.S_ISREG(before.st_mode)
                    and (before.st_dev, before.st_ino, before.st_uid,
                         stat.S_IMODE(before.st_mode), before.st_nlink,
                         before.st_size) == expected_identity
                    and (named.st_dev, named.st_ino, named.st_uid,
                         stat.S_IMODE(named.st_mode), named.st_nlink,
                         named.st_size) == expected_identity,
                    "V2 INTENT record identity changed")
            content = b""
            while len(content) < len(expected):
                chunk = os.read(record_fd, len(expected) - len(content))
                require(type(chunk) is bytes and chunk,
                        "V2 INTENT reread made no progress")
                content += chunk
            require(content == expected and os.read(record_fd, 1) == b"",
                    "V2 INTENT reread bytes changed")
            after = os.fstat(record_fd)
            final_named = os.stat("exact-event-000001.json", dir_fd=root_fd,
                                  follow_symlinks=False)
            root_after = os.fstat(root_fd)
            root_final_named = os.stat(root, follow_symlinks=False)
            require((after.st_dev, after.st_ino, after.st_uid,
                     stat.S_IMODE(after.st_mode), after.st_nlink,
                     after.st_size) == expected_identity
                    and (final_named.st_dev, final_named.st_ino,
                         final_named.st_uid, stat.S_IMODE(final_named.st_mode),
                         final_named.st_nlink, final_named.st_size)
                    == expected_identity,
                    "V2 INTENT record changed during reread")
            require((root_after.st_dev, root_after.st_ino, root_after.st_uid,
                     stat.S_IMODE(root_after.st_mode)) == root_authority
                    and (root_final_named.st_dev, root_final_named.st_ino,
                         root_final_named.st_uid,
                         stat.S_IMODE(root_final_named.st_mode))
                    == root_authority,
                    "V2 INTENT root changed during reread")
        except BaseException as exc:
            failure = exc
        for fd in (record_fd, root_fd):
            if fd is not None:
                try: os.close(fd)
                except BaseException as exc: failure = failure or exc
        if failure is not None:
            raise Refusal("V2 INTENT independent reread ambiguous") from failure


def create_v2_intent_file_bridge(backend):
    return V2IntentFileBridge(backend)
