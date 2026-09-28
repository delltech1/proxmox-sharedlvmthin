#!/usr/bin/python3
"""Source-only exact-event journal adapter; no filesystem or process backend."""

import copy
import hashlib
import json
import re
import threading

import prelive_supervisor_model as SUP


EVENT_ORDER = ("INTENT", "CHILD_BOUND", "EXEC_ISSUED")
MAX_EVENT_BYTES = 65536
_RECEIPT_KEY = object()
_SEAL_KEY = object()


class Refusal(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise Refusal(message)


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def hex_value(value, length):
    return (type(value) is str
            and re.fullmatch(r"[0-9a-f]{%d}" % length, value) is not None)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refusal("duplicate JSON key")
        result[key] = value
    return result


def decode_canonical(raw):
    require(type(raw) is bytes and 0 < len(raw) <= MAX_EVENT_BYTES,
            "exact event byte bound")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except Refusal:
        raise
    except BaseException as exc:
        raise Refusal("exact event JSON") from exc
    require(type(value) is dict and SUP.canonical(value) == raw,
            "event bytes are not canonical")
    return value


class ExactEventReceipt:
    def __init__(self, key, session, sequence, kind, name, digest,
                 previous_digest, byte_count):
        require(key is _RECEIPT_KEY, "receipt construction is private")
        object.__setattr__(self, "session", session)
        object.__setattr__(self, "sequence", sequence)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "digest", digest)
        object.__setattr__(self, "previous_digest", previous_digest)
        object.__setattr__(self, "byte_count", byte_count)
        object.__setattr__(self, "_frozen", True)

    def __setattr__(self, name, value):
        if getattr(self, "_frozen", False):
            raise Refusal("event receipt is immutable")
        object.__setattr__(self, name, value)

    def __copy__(self):
        raise Refusal("event receipt is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("event receipt is noncopyable")


class PreGrantSeal:
    def __init__(self, key, session, final_digest):
        require(key is _SEAL_KEY, "seal construction is private")
        object.__setattr__(self, "session", session)
        object.__setattr__(self, "final_digest", final_digest)
        object.__setattr__(self, "sequence", 3)
        object.__setattr__(self, "_frozen", True)

    def __setattr__(self, name, value):
        if getattr(self, "_frozen", False):
            raise Refusal("pre-grant seal is immutable")
        object.__setattr__(self, name, value)

    def __copy__(self):
        raise Refusal("pre-grant seal is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("pre-grant seal is noncopyable")


class ExactEventSession:
    """Single-owner, create-only exact-byte chain over an injected backend."""

    def __init__(self, backend, request_id, owner):
        require(getattr(backend, "source_only", None) is True,
                "source-only backend boundary required")
        require(hex_value(request_id, 32), "request identity")
        require(type(owner) is dict and set(owner) == {
                    "pid", "starttime", "boot_id"}, "owner schema")
        self.owner = SUP.validate_owner(copy.deepcopy(owner), owner["boot_id"])
        self.backend = backend
        self.request_id = request_id
        self.owner_thread = threading.current_thread()
        self.state = "READY"
        self.sequence = 0
        self.last_digest = "0" * 64
        self.receipts = []
        self._frozen_receipts = []
        self.controller = None
        self.request = None
        self.attempt_id = None
        self.seal = None

    def __copy__(self):
        raise Refusal("exact event session is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("exact event session is noncopyable")

    def claim(self, controller):
        if (threading.current_thread() is not self.owner_thread
                or self.state != "READY" or self.controller is not None
                or controller is None):
            self._poison()
            raise Refusal("exact controller claim")
        self.controller = controller

    def _poison(self):
        self.state = "UNKNOWN"

    def _validate_event(self, event):
        require(set(event) == {"schema", "request_id", "sequence", "kind",
                               "previous_digest", "payload"}
                and type(event["schema"]) is int and event["schema"] == 1
                and event["request_id"] == self.request_id
                and type(event["sequence"]) is int
                and event["sequence"] == self.sequence + 1
                and event["kind"] == EVENT_ORDER[self.sequence]
                and event["previous_digest"] == self.last_digest
                and type(event["payload"]) is dict,
                "exact event envelope")
        payload = event["payload"]
        if event["kind"] == "INTENT":
            require(set(payload) == {"request", "request_sha256", "owner",
                                     "unarmed_deadline_ns",
                                     "pre_fork_ticket_bound"},
                    "INTENT payload schema")
            request = SUP.validate_request(copy.deepcopy(payload["request"]))
            owner = SUP.validate_owner(copy.deepcopy(payload["owner"]),
                                       request["boot_id"])
            require(request["request_id"] == self.request_id
                    and owner == self.owner
                    and payload["request_sha256"] == SUP.digest(request)
                    and integer(payload["unarmed_deadline_ns"], 1)
                    and payload["pre_fork_ticket_bound"] is True,
                    "INTENT payload identity")
            self.request = request
        elif event["kind"] == "CHILD_BOUND":
            require(self.request is not None and set(payload) == {
                        "intent_digest", "child", "lifecycle_token",
                        "launcher", "channel_identity"},
                    "CHILD_BOUND payload schema")
            child = SUP.validate_child(copy.deepcopy(payload["child"]),
                                       self.request, self.owner, False)
            launcher = SUP.validate_executable(copy.deepcopy(payload["launcher"]))
            channel = payload["channel_identity"]
            require(payload["intent_digest"] == self.last_digest
                    and child["request_id"] == self.request_id
                    and hex_value(payload["lifecycle_token"], 32)
                    and launcher == self.request["launcher"]
                    and type(channel) is dict
                    and set(channel) == {"token", "writer_open", "reader_owned"}
                    and hex_value(channel["token"], 32)
                    and channel["writer_open"] is True
                    and channel["reader_owned"] is True,
                    "CHILD_BOUND payload identity")
        else:
            require(self.request is not None and set(payload) == {
                        "intent_digest", "child_bound_digest", "attempt_id",
                        "grant_protocol"}, "EXEC_ISSUED payload schema")
            require(payload["intent_digest"] == self._frozen_receipts[0][3]
                    and payload["child_bound_digest"] == self.last_digest
                    and hex_value(payload["attempt_id"], 32)
                    and payload["grant_protocol"]
                    == "ONE_BYTE_G_THEN_WRITER_EOF",
                    "EXEC_ISSUED payload identity")
            self.attempt_id = payload["attempt_id"]

    def append_exact_event(self, raw, controller):
        if (threading.current_thread() is not self.owner_thread
                or self.controller is None or controller is None
                or controller is not self.controller
                or self.state != "READY"):
            self._poison()
            raise Refusal("foreign, unclaimed, reentrant or sealed append")
        try:
            event = decode_canonical(raw)
            self._validate_event(event)
            sequence = self.sequence + 1
            kind = event["kind"]
            digest = hashlib.sha256(raw).hexdigest()
            name = "exact-event-%06d.json" % sequence
            self.state = "PERSISTING"
            ack = self.backend.persist_exact_file(name, raw)
            identity = ack.get("record_identity") if type(ack) is dict else None
            require(self.state == "PERSISTING"
                    and type(ack) is dict and set(ack) == {
                        "bytes_written", "file_synced", "dir_synced",
                        "closed", "name", "sha256", "record_identity"}
                    and type(ack["bytes_written"]) is int
                    and ack["bytes_written"] == len(raw)
                    and ack["file_synced"] is True
                    and ack["dir_synced"] is True
                    and ack["closed"] is True
                    and ack["name"] == name and ack["sha256"] == digest
                    and type(identity) is dict
                    and set(identity) == {"dev", "inode", "uid", "mode", "nlink"}
                    and integer(identity["dev"])
                    and integer(identity["inode"], 1)
                    and integer(identity["uid"])
                    and type(identity["mode"]) is int
                    and identity["mode"] == 0o600
                    and type(identity["nlink"]) is int
                    and identity["nlink"] == 1,
                    "exact persistence acknowledgement")
            receipt = ExactEventReceipt(
                _RECEIPT_KEY, self, sequence, kind, name, digest,
                self.last_digest, len(raw))
            self.sequence = sequence
            self.last_digest = digest
            self.receipts.append(receipt)
            self._frozen_receipts.append((
                sequence, kind, name, digest, receipt.previous_digest, len(raw)))
            self.state = "READY"
            return receipt
        except BaseException:
            self._poison()
            raise

    def seal_pregrant(self, controller):
        if (threading.current_thread() is not self.owner_thread
                or self.controller is None or controller is None
                or controller is not self.controller or self.state != "READY"
                or self.sequence != 3):
            self._poison()
            raise Refusal("pre-grant seal unavailable")
        try:
            for receipt, frozen in zip(self.receipts, self._frozen_receipts):
                require(type(receipt) is ExactEventReceipt
                        and receipt.session is self
                        and (receipt.sequence, receipt.kind, receipt.name,
                             receipt.digest, receipt.previous_digest,
                             receipt.byte_count) == frozen,
                        "event receipt authority changed")
            require(len(self.receipts) == 3 and len(self._frozen_receipts) == 3
                    and tuple(value[1] for value in self._frozen_receipts)
                    == EVENT_ORDER
                    and self._frozen_receipts[-1][3] == self.last_digest,
                    "sealed receipt chain changed")
        except BaseException:
            self._poison()
            raise
        self.state = "SEALED"
        self.seal = PreGrantSeal(_SEAL_KEY, self, self.last_digest)
        return self.seal

    def snapshot(self):
        return {"schema": 1, "classification": {
                    "READY": "SOURCE_EXACT_EVENTS_PARTIAL",
                    "SEALED": "SOURCE_EXACT_BYTES_AND_ORDER_SEALED",
                    "UNKNOWN": "UNKNOWN"}.get(self.state, "UNKNOWN_BUSY"),
                "sequence": self.sequence, "last_digest": self.last_digest,
                "sealed": self.state == "SEALED", "grant_authorized": False,
                "runtime_authorized": False, "storage_authorized": False,
                "postcondition_verified": False}


def create_source_session(request_id, owner, backend):
    return ExactEventSession(backend, request_id, owner)
