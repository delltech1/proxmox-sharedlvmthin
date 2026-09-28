#!/usr/bin/python3
"""Pure pre-grant composition model; contains no OS or storage backend."""

import copy
import hashlib
import re
import threading

import prelive_supervisor_model as SUP
import prelive_owned_child as OWNED


EVENT_ORDER = ("INTENT", "CHILD_BOUND", "EXEC_ISSUED")
_CAP_KEY = object()
_PERMIT_KEY = object()
_BINDING_KEY = object()
_SEAL_KEY = object()
_V2_SESSION_KEY = object()
_V2_CHAIN_KEY = object()


class Refusal(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise Refusal(message)


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def hex32(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{32}", value) is not None


class PersistedEventCapability:
    def __init__(self, key, session, sequence, kind, event_digest, previous_digest,
                 event_bytes):
        require(key is _CAP_KEY, "event capability construction is private")
        self.session = session
        self.sequence = sequence
        self.kind = kind
        self.event_digest = event_digest
        self.previous_digest = previous_digest
        self.event_bytes = bytes(event_bytes)

    def __copy__(self):
        raise Refusal("persisted event capability is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("persisted event capability is noncopyable")


class OneShotGrantPermit:
    def __init__(self, key, controller, intent, child_bound, issued, binding,
                 attempt_id):
        require(key is _PERMIT_KEY, "grant permit construction is private")
        self.controller = controller
        self.intent = intent
        self.child_bound = child_bound
        self.issued = issued
        self.binding = binding
        self.attempt_id = attempt_id
        self.owner_thread = threading.current_thread()
        self.used = False

    def __copy__(self):
        raise Refusal("grant permit is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("grant permit is noncopyable")


class ModelGrantChannel:
    def __init__(self, key, token):
        require(key is _BINDING_KEY and hex32(token),
                "grant channel construction is private")
        self.token = token
        self.writer_open = True
        self.phase = "WRITER_OPEN"
        self.owner_thread = threading.current_thread()

    def __copy__(self):
        raise Refusal("grant channel is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("grant channel is noncopyable")

    def begin_attempt(self, key):
        require(key is _PERMIT_KEY and self.phase == "WRITER_OPEN",
                "grant channel attempt unavailable")
        self.phase = "ATTEMPTED"

    def confirm_eof(self, key):
        require(key is _PERMIT_KEY and self.phase == "ATTEMPTED",
                "grant channel EOF confirmation unavailable")
        self.writer_open = False
        self.phase = "EOF_CONFIRMED"


class LaunchBinding:
    def __init__(self, key, child, launcher, owned_handle, capture_adapter,
                 fork_origin, grant_channel):
        require(key is _BINDING_KEY, "launch binding construction is private")
        self.child = copy.deepcopy(child)
        self.lifecycle_token = owned_handle.lifecycle["token"]
        self.launcher = copy.deepcopy(launcher)
        self.channel_identity = {"token": grant_channel.token,
                                 "writer_open": grant_channel.writer_open,
                                 "reader_owned": True}
        self.owned_identity = {
            "child": copy.deepcopy(owned_handle.child),
            "supervisor": copy.deepcopy(owned_handle.lifecycle["supervisor"]),
            "lifecycle_token": owned_handle.lifecycle["token"],
            "origin_pid": fork_origin.pid,
            "launcher": copy.deepcopy(owned_handle.launcher),
            "pidfd": owned_handle.pidfd}
        self.fork_origin = fork_origin
        self.pidfd_handle = owned_handle
        self.capture_adapter = capture_adapter
        self.grant_channel = grant_channel
        self.owner_thread = threading.current_thread()
        self.identity_digest = SUP.digest({
            "child": self.child, "lifecycle_token": self.lifecycle_token,
            "launcher": self.launcher, "channel_identity": self.channel_identity,
            "owned_identity": self.owned_identity})

    def __copy__(self):
        raise Refusal("launch binding is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("launch binding is noncopyable")


class JournalSessionModel:
    """Closed three-event chain around an explicitly model-only backend."""
    def __init__(self, backend, request_id, owner):
        require(getattr(backend, "model_only", None) is True,
                "model-only journal backend required")
        require(hex32(request_id), "journal request identity")
        self.backend = backend
        self.request_id = request_id
        self.owner = copy.deepcopy(owner)
        self.owner_thread = threading.current_thread()
        self.state = "READY"
        self.sequence = 0
        self.last_digest = "0" * 64
        self.capabilities = []
        self.controller = None

    def __copy__(self):
        raise Refusal("journal session is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("journal session is noncopyable")

    def claim_controller(self, controller):
        require(self.controller is None and controller is not None,
                "journal controller claim unavailable")
        self.controller = controller

    def _poison(self):
        self.state = "UNKNOWN"

    def persist(self, kind, payload, controller):
        if (threading.current_thread() is not self.owner_thread
                or self.state != "READY" or controller is not self.controller):
            self._poison()
            raise Refusal("foreign, reentrant or sealed journal persistence")
        expected = EVENT_ORDER[self.sequence] if self.sequence < 3 else None
        require(kind == expected, "journal event order")
        controller._validate_event_payload(kind, payload)
        sequence = self.sequence + 1
        event = {"schema": 1, "request_id": self.request_id,
                 "sequence": sequence, "kind": kind,
                 "previous_digest": self.last_digest,
                 "payload": copy.deepcopy(payload)}
        raw = SUP.canonical(event)
        digest = hashlib.sha256(raw).hexdigest()
        self.state = "PERSISTING"
        try:
            ack = self.backend.persist_model(raw, copy.deepcopy(event))
            require(self.state == "PERSISTING"
                    and controller.state.startswith("PERSISTING_")
                    and type(ack) is dict
                    and set(ack) == {"bytes_written", "file_synced", "dir_synced"}
                    and type(ack["bytes_written"]) is int
                    and ack["bytes_written"] == len(raw)
                    and ack["file_synced"] is True
                    and ack["dir_synced"] is True,
                    "journal persistence acknowledgement")
        except BaseException:
            self._poison()
            raise
        cap = PersistedEventCapability(
            _CAP_KEY, self, sequence, kind, digest, self.last_digest, raw)
        self.sequence = sequence
        self.last_digest = digest
        self.capabilities.append(cap)
        self.state = "READY"
        return cap

    def seal_active(self, key, controller):
        require(key is _SEAL_KEY and self.state == "READY"
                and self.sequence == 3
                and controller is self.controller
                and controller.state == "SEALING_FOR_GRANT",
                "journal active seal unavailable")
        self.state = "SEALED_ACTIVE"


class V2JournalSessionModel:
    """Private model-only V2 session; deliberately distinct from its backend."""
    model_only_v2_journal = True

    def __init__(self, key, backend):
        require(key is _V2_SESSION_KEY,
                "V2 journal session construction is private")
        backend_type = type(backend)
        require(backend_type.__dict__.get(
                    "model_only_v2_journal_backend") is True
                and callable(backend_type.__dict__.get("append_v2_model")),
                "model-only V2 journal backend required")
        self.backend = backend
        self.controller = None
        self.chain = None
        self._chain = None
        self.controller_claim = None
        self.owner_thread = threading.current_thread()
        self.state = "UNCLAIMED"

    def __copy__(self):
        raise Refusal("V2 journal session is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("V2 journal session is noncopyable")

    def claim_controller(self, controller):
        require(threading.current_thread() is self.owner_thread
                and self.state == "UNCLAIMED"
                and self.controller is None and controller is not None,
                "V2 journal controller claim unavailable")
        self.controller = controller
        self.state = "CONTROLLER_CLAIMED"

    def claim_chain(self, key, chain, controller_claim):
        require(key is _V2_CHAIN_KEY
                and threading.current_thread() is self.owner_thread
                and self.state == "CONTROLLER_CLAIMED"
                and self.chain is None and chain is not None,
                "V2 journal chain claim unavailable")
        self.chain = chain
        self._chain = chain
        self.controller_claim = controller_claim
        self.state = "READY"

    def append_v2_model(self, event_bytes):
        if not (threading.current_thread() is self.owner_thread
                and self.state == "READY"
                and type(event_bytes) is bytes
                and type(self.controller) is PreGrantController
                and self.controller.journal is self
                and self.chain is self._chain
                and self.controller_claim is not None
                and getattr(self.chain, "backend", None) is self
                and getattr(self.chain, "_backend", None) is self
                and getattr(self.chain, "state", None) == "APPENDING"
                and getattr(self.chain, "_append_inflight", None) is True
                and getattr(self.chain, "_attempted_bytes_private", None)
                == event_bytes):
            self.state = "UNKNOWN"
            raise Refusal("active exact V2 chain append required")
        self.state = "BUSY"
        try:
            ack = self.backend.append_v2_model(event_bytes)
            require(self.state == "BUSY",
                    "V2 journal callback changed session authority")
            self.state = "READY"
            return ack
        except BaseException:
            self.state = "UNKNOWN"
            raise


def create_v2_journal_session_model(backend):
    return V2JournalSessionModel(_V2_SESSION_KEY, backend)


def claim_v2_event_chain_model(session, chain, controller_claim):
    # Delayed import avoids the module cycle while retaining exact-type checks.
    import prelive_launcher_identity_consumer as consumer
    require(type(session) is V2JournalSessionModel
            and type(chain) is consumer.V2EventChain
            and type(controller_claim) is consumer.V2ControllerClaim
            and chain.controller_claim is controller_claim
            and chain._controller_claim is controller_claim
            and controller_claim.session is session
            and controller_claim.controller is session.controller
            and chain.binding is controller_claim.binding
            and chain.backend is session and chain._backend is session,
            "exact canonical V2 event chain required")
    session.claim_chain(_V2_CHAIN_KEY, chain, controller_claim)


class PreGrantController:
    def __init__(self, request, owner, journal, launcher, started_ns,
                 unarmed_deadline_ns):
        self.request = request
        self.owner = owner
        self.journal = journal
        self._construction_journal = journal
        self.launcher = launcher
        self.started_ns = started_ns
        self.unarmed_deadline_ns = unarmed_deadline_ns
        self.last_clock_ns = started_ns
        self.owner_thread = threading.current_thread()
        self.state = "NEW"
        self.intent = None
        self.binding = None
        self.child_bound = None
        self.exec_issued = None
        self.permit = None
        self.grant_result = None
        self.settlement_required = False
        self.error = None
        self.child_may_exist = False
        self.owned_lifecycle = {}
        self.grant_consumed = False
        self.descendant_binding = object()
        self.pre_fork_ticket = OWNED.prepare_descendant_fork_enrollment(
            OWNED._PREFORK_KEY, self.descendant_binding, self.owner)
        self.grant_channel = ModelGrantChannel(
            _BINDING_KEY, hashlib.sha256(
                (self.request["request_id"] + ":grant").encode("ascii")
            ).hexdigest()[:32])
        self.original_grant_channel = self.grant_channel
        self.owned_child_evidence = None
        self.attempt_id = None
        self.v2_identity_claim = None
        self._v2_identity_claim = None
        self.model_contract = (
            "PREGRANT_V2_MODEL_ONLY"
            if type(journal) is V2JournalSessionModel
            else "PREGRANT_V1_MODEL_ONLY"
            if type(journal) is JournalSessionModel else None)
        self._construction_contract = self.model_contract
        self.journal.claim_controller(self)
        self._construction_sealed = True

    def __setattr__(self, name, value):
        if (getattr(self, "_construction_sealed", False)
                and name in {"journal", "_construction_journal",
                             "model_contract", "_construction_contract"}):
            raise Refusal("controller construction identity is immutable")
        object.__setattr__(self, name, value)

    def __copy__(self):
        raise Refusal("pre-grant controller is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("pre-grant controller is noncopyable")

    def _poison(self, reason):
        self.state = "UNKNOWN"
        self.error = reason
        if self.child_may_exist:
            self.settlement_required = True

    def _enter(self, expected, operation):
        self._require_v1_transition()
        if (threading.current_thread() is not self.owner_thread
                or self.state != expected):
            self._poison("foreign, reentrant or out-of-phase operation")
            raise Refusal(self.error)
        self.state = operation

    def _require_v1_transition(self):
        require(self.model_contract == "PREGRANT_V1_MODEL_ONLY"
                and self._construction_contract
                == "PREGRANT_V1_MODEL_ONLY"
                and type(self.journal) is JournalSessionModel
                and self.journal is self._construction_journal,
                "exact V1 controller required")

    def _clock(self):
        now = self.launcher.monotonic_ns_model()
        require(self.state.startswith("PERSISTING_")
                or self.state in ("BINDING_UNARMED", "VALIDATING_GRANT"),
                "clock callback reentrancy")
        require(integer(now) and now >= self.last_clock_ns,
                "pre-grant clock regressed")
        self.last_clock_ns = now
        require(now < self.unarmed_deadline_ns,
                "unarmed launcher deadline reached")
        return now

    def _owner(self):
        observed = self.launcher.owner_identity_model()
        require((self.state.startswith("PERSISTING_")
                 or self.state in ("BINDING_UNARMED", "VALIDATING_GRANT"))
                and SUP.validate_owner(copy.deepcopy(observed),
                                       self.request["boot_id"]) == self.owner,
                "pre-grant owner changed")

    def _validate_owned_identity(self, value):
        require(type(value) is dict and set(value) == {
                    "child", "supervisor", "lifecycle_token", "origin_pid",
                    "launcher", "pidfd"}, "opaque identity schema")
        child = value["child"]
        require(type(child) is dict
                and set(child) == {"pid", "starttime", "boot_id"}
                and integer(child["pid"], 1)
                and integer(child["starttime"], 1)
                and type(child["boot_id"]) is str
                and child["boot_id"] == self.request["boot_id"],
                "opaque child identity schema")
        supervisor = SUP.validate_owner(
            copy.deepcopy(value["supervisor"]), self.request["boot_id"])
        require(supervisor == self.owner
                and hex32(value["lifecycle_token"])
                and integer(value["origin_pid"], 1),
                "opaque lifecycle identity schema")
        launcher = value["launcher"]
        require(type(launcher) is dict
                and set(launcher) == {"kind", "exe_dev", "exe_inode",
                                     "cmdline_sha256"}
                and launcher["kind"] == "UNARMED_LAUNCHER_NOT_PAYLOAD"
                and integer(launcher["exe_dev"])
                and integer(launcher["exe_inode"])
                and type(launcher["cmdline_sha256"]) is str
                and re.fullmatch(r"[0-9a-f]{64}",
                                 launcher["cmdline_sha256"]) is not None
                and integer(value["pidfd"]),
                "opaque launcher/pidfd identity schema")
        return value

    def _validate_chain(self):
        caps = (self.intent, self.child_bound, self.exec_issued)
        require(all(type(cap) is PersistedEventCapability
                    and cap.session is self.journal
                    for cap in caps)
                and tuple(cap.kind for cap in caps) == EVENT_ORDER
                and tuple(cap.sequence for cap in caps) == (1, 2, 3)
                and self.child_bound.previous_digest == self.intent.event_digest
                and self.exec_issued.previous_digest == self.child_bound.event_digest
                and self.journal.capabilities == list(caps)
                and self.journal.sequence == 3
                and self.journal.last_digest == self.exec_issued.event_digest
                and type(self.binding) is LaunchBinding
                and self.binding.owner_thread is self.owner_thread,
                "pre-grant capability chain changed")
        require(self.binding.identity_digest == SUP.digest({
                    "child": self.binding.child,
                    "lifecycle_token": self.binding.lifecycle_token,
                    "launcher": self.binding.launcher,
                    "channel_identity": self.binding.channel_identity,
                    "owned_identity": self.binding.owned_identity})
                and self.binding.owner_thread is self.owner_thread,
                "launch binding changed")
        require(self.binding.grant_channel is self.original_grant_channel
                and self.binding.fork_origin
                is self.binding.pidfd_handle.lifecycle["origin"]
                and self.binding.fork_origin.pre_fork_enrollment
                is self.pre_fork_ticket
                and self.pre_fork_ticket.used is True
                and self.binding.capture_adapter.handle
                is self.binding.pidfd_handle
                and self.binding.pidfd_handle.capture_adapter
                is self.binding.capture_adapter
                and self.binding.pidfd_handle.state == "BOUND"
                and self.binding.pidfd_handle.lifecycle["state"] == "PIDFD_BOUND"
                and self.binding.capture_adapter.state == "PRIMARY"
                and self.binding.capture_adapter.cached is None,
                "opaque process graph changed")
        expected_owned = {
            "child": {"pid": self.binding.child["pid"],
                      "starttime": self.binding.child["starttime"],
                      "boot_id": self.binding.child["boot_id"]},
            "supervisor": copy.deepcopy(self.owner),
            "lifecycle_token": self.binding.lifecycle_token,
            "origin_pid": self.binding.child["pid"],
            "launcher": {"kind": "UNARMED_LAUNCHER_NOT_PAYLOAD",
                         "exe_dev": self.binding.launcher["dev"],
                         "exe_inode": self.binding.launcher["inode"],
                         "cmdline_sha256": self.binding.launcher["sha256"]},
            "pidfd": self.binding.pidfd_handle.pidfd}
        self._validate_owned_identity(self.binding.owned_identity)
        current_owned = {
            "child": self.binding.pidfd_handle.child,
            "supervisor": self.binding.pidfd_handle.lifecycle["supervisor"],
            "lifecycle_token": self.binding.pidfd_handle.lifecycle["token"],
            "origin_pid": self.binding.fork_origin.pid,
            "launcher": self.binding.pidfd_handle.launcher,
            "pidfd": self.binding.pidfd_handle.pidfd}
        self._validate_owned_identity(current_owned)
        require(self.binding.owned_identity == expected_owned
                and self.binding.pidfd_handle.child
                == self.binding.owned_identity["child"]
                and self.binding.pidfd_handle.lifecycle["supervisor"]
                == self.binding.owned_identity["supervisor"]
                and self.binding.pidfd_handle.lifecycle["token"]
                == self.binding.owned_identity["lifecycle_token"]
                and self.binding.fork_origin.pid
                == self.binding.owned_identity["origin_pid"]
                and self.binding.pidfd_handle.launcher
                == self.binding.owned_identity["launcher"]
                and self.binding.pidfd_handle.pidfd
                == self.binding.owned_identity["pidfd"],
                "opaque process identity changed")
        require(self.original_grant_channel.token
                == self.binding.channel_identity["token"]
                and self.original_grant_channel.owner_thread is self.owner_thread
                and ((self.grant_consumed is False
                      and self.original_grant_channel.phase == "WRITER_OPEN"
                      and self.original_grant_channel.writer_open is True)
                     or (self.grant_consumed is True
                         and self.original_grant_channel.phase
                         in ("ATTEMPTED", "EOF_CONFIRMED"))),
                "grant channel authority changed")
        for cap in caps:
            require(hashlib.sha256(cap.event_bytes).hexdigest()
                    == cap.event_digest, "journal event preimage changed")
        previous = "0" * 64
        for sequence, (kind, cap) in enumerate(zip(EVENT_ORDER, caps), 1):
            expected = {"schema": 1, "request_id": self.request["request_id"],
                        "sequence": sequence, "kind": kind,
                        "previous_digest": previous,
                        "payload": self._event_payload(kind)}
            require(SUP.canonical(expected) == cap.event_bytes,
                    "journal event differs from exact preimage")
            previous = cap.event_digest

    def _validate_permit(self):
        require(type(self.permit) is OneShotGrantPermit
                and self.permit.controller is self
                and self.permit.intent is self.intent
                and self.permit.child_bound is self.child_bound
                and self.permit.issued is self.exec_issued
                and self.permit.binding is self.binding
                and self.permit.owner_thread is self.owner_thread
                and self.permit.used is True,
                "one-shot grant permit changed")
        require(self.grant_consumed is True
                and self.binding.grant_channel is self.original_grant_channel,
                "grant monotonic latch changed")
        require(self.permit.attempt_id == self.attempt_id
                and self.exec_issued.kind == "EXEC_ISSUED",
                "grant attempt identity changed")

    def _capture_owned_evidence(self):
        child = self.owned_lifecycle.get("owned_child")
        token = self.owned_lifecycle.get("token")
        origin = self.owned_lifecycle.get("origin")
        if (type(child) is dict and integer(child.get("pid"), 1)
                and integer(child.get("starttime"), 1) and hex32(token)
                and type(origin) is OWNED._ForkOrigin):
            self.owned_child_evidence = {
                "pid": child["pid"], "starttime": child["starttime"],
                "lifecycle_token": token, "fork_origin_present": True}

    def _event_payload(self, kind):
        if kind == "INTENT":
            return {"request": copy.deepcopy(self.request),
                    "request_sha256": SUP.digest(self.request),
                    "owner": copy.deepcopy(self.owner),
                    "unarmed_deadline_ns": self.unarmed_deadline_ns,
                    "pre_fork_ticket_bound": True}
        if kind == "CHILD_BOUND":
            require(self.binding is not None and self.intent is not None,
                    "child-bound event prerequisites")
            return {"intent_digest": self.intent.event_digest,
                    "child": copy.deepcopy(self.binding.child),
                    "lifecycle_token": self.binding.lifecycle_token,
                    "launcher": copy.deepcopy(self.binding.launcher),
                    "channel_identity": copy.deepcopy(
                        self.binding.channel_identity)}
        if kind == "EXEC_ISSUED":
            require(self.intent is not None and self.child_bound is not None
                    and hex32(self.attempt_id), "exec-issued event prerequisites")
            return {"intent_digest": self.intent.event_digest,
                    "child_bound_digest": self.child_bound.event_digest,
                    "attempt_id": self.attempt_id,
                    "grant_protocol": "ONE_BYTE_G_THEN_WRITER_EOF"}
        raise Refusal("unsupported journal event")

    def _validate_event_payload(self, kind, payload):
        require(type(payload) is dict, "journal payload schema")
        if kind == "INTENT":
            require(set(payload) == {"request", "request_sha256", "owner",
                                     "unarmed_deadline_ns",
                                     "pre_fork_ticket_bound"}
                    and payload["request"] == self.request
                    and payload["request_sha256"] == SUP.digest(self.request)
                    and payload["owner"] == self.owner
                    and type(payload["unarmed_deadline_ns"]) is int
                    and payload["unarmed_deadline_ns"] == self.unarmed_deadline_ns
                    and payload["pre_fork_ticket_bound"] is True,
                    "INTENT payload binding")
        elif kind == "CHILD_BOUND":
            require(self.binding is not None
                    and set(payload) == {"intent_digest", "child",
                                         "lifecycle_token", "launcher",
                                         "channel_identity"}
                    and payload["intent_digest"] == self.intent.event_digest
                    and payload["child"] == self.binding.child
                    and payload["lifecycle_token"] == self.binding.lifecycle_token
                    and payload["launcher"] == self.binding.launcher
                    and payload["channel_identity"] == self.binding.channel_identity,
                    "CHILD_BOUND payload binding")
        elif kind == "EXEC_ISSUED":
            require(self.binding is not None and self.child_bound is not None
                    and set(payload) == {"intent_digest", "child_bound_digest",
                                         "attempt_id", "grant_protocol"}
                    and payload["intent_digest"] == self.intent.event_digest
                    and payload["child_bound_digest"]
                    == self.child_bound.event_digest
                    and payload["attempt_id"] == self.attempt_id
                    and hex32(payload["attempt_id"])
                    and payload["grant_protocol"]
                    == "ONE_BYTE_G_THEN_WRITER_EOF",
                    "EXEC_ISSUED payload binding")
        else:
            raise Refusal("unsupported journal event")

    def persist_intent(self):
        self._require_v1_transition()
        try:
            self._enter("NEW", "PERSISTING_INTENT")
            self._owner(); self._clock()
            payload = self._event_payload("INTENT")
            self.intent = self.journal.persist("INTENT", payload, self)
            require(self.state == "PERSISTING_INTENT", "intent callback reentrancy")
            self._owner(); self._clock()
            self.state = "INTENT_PERSISTED"
            return self.snapshot()
        except BaseException as exc:
            self._poison(type(exc).__name__)
            return self.snapshot()

    def prepare_and_bind_unarmed(self):
        self._require_v1_transition()
        try:
            self._enter("INTENT_PERSISTED", "BINDING_UNARMED")
            self._owner(); self._clock()
            self.child_may_exist = True
            self.settlement_required = True
            observed = self.launcher.prepare_bind_unarmed_model(
                copy.deepcopy(self.request), copy.deepcopy(self.owner),
                self.pre_fork_ticket, self.owned_lifecycle,
                self.grant_channel)
            self._capture_owned_evidence()
            require(self.state == "BINDING_UNARMED", "launcher callback reentrancy")
            require(type(observed) is dict and set(observed) == {
                "child", "launcher", "owned_handle", "capture_adapter",
                "fork_origin", "grant_channel", "readiness"},
                "unarmed binding schema")
            child = SUP.validate_child(
                copy.deepcopy(observed["child"]), self.request, self.owner, False)
            launcher = SUP.validate_executable(copy.deepcopy(observed["launcher"]))
            owned_handle = observed["owned_handle"]
            adapter = observed["capture_adapter"]
            origin = observed["fork_origin"]
            require(launcher == self.request["launcher"]
                    and type(owned_handle) is OWNED._OwnedPidfdHandle
                    and type(adapter) is OWNED._CaptureChildAdapter
                    and type(origin) is OWNED._ForkOrigin
                    and observed["grant_channel"] is self.grant_channel
                    and origin is owned_handle.lifecycle["origin"]
                    and origin.pre_fork_enrollment is self.pre_fork_ticket
                    and self.pre_fork_ticket.used is True
                    and adapter.handle is owned_handle
                    and owned_handle.capture_adapter is adapter
                    and owned_handle.child == {
                        "pid": child["pid"], "starttime": child["starttime"],
                        "boot_id": child["boot_id"]}
                    and owned_handle.lifecycle["supervisor"] == self.owner
                    and hex32(owned_handle.lifecycle["token"])
                    and origin.pid == child["pid"]
                    and owned_handle.launcher == {
                        "kind": "UNARMED_LAUNCHER_NOT_PAYLOAD",
                        "exe_dev": launcher["dev"],
                        "exe_inode": launcher["inode"],
                        "cmdline_sha256": launcher["sha256"]}
                    and owned_handle.state == "BOUND"
                    and adapter.state == "PRIMARY"
                    and observed["readiness"] == "R"
                    and self.owned_lifecycle.get("origin") is origin,
                    "unarmed launcher binding")
            self.binding = LaunchBinding(
                _BINDING_KEY, child, launcher, owned_handle, adapter, origin,
                self.grant_channel)
            self._owner(); self._clock()
            self.state = "PERSISTING_CHILD_BOUND"
            payload = self._event_payload("CHILD_BOUND")
            self.child_bound = self.journal.persist("CHILD_BOUND", payload, self)
            require(self.state == "PERSISTING_CHILD_BOUND",
                    "child-bound callback reentrancy")
            self._owner(); self._clock()
            self.state = "CHILD_BOUND_PERSISTED"
            return self.snapshot()
        except BaseException as exc:
            self._capture_owned_evidence()
            self._poison(type(exc).__name__)
            return self.snapshot()

    def issue_grant_once(self, attempt_id):
        self._require_v1_transition()
        try:
            self._enter("CHILD_BOUND_PERSISTED", "PERSISTING_EXEC_ISSUED")
            require(hex32(attempt_id), "grant attempt identity")
            self.attempt_id = attempt_id
            self._owner(); self._clock()
            payload = self._event_payload("EXEC_ISSUED")
            self.exec_issued = self.journal.persist(
                "EXEC_ISSUED", payload, self)
            require(self.state == "PERSISTING_EXEC_ISSUED",
                    "exec-issued callback reentrancy")
            self.state = "VALIDATING_GRANT"
            self._owner(); self._clock()
            recheck = self.launcher.recheck_unarmed_model(
                self.binding, self.original_grant_channel)
            require(self.state == "VALIDATING_GRANT"
                    and type(recheck) is dict
                    and set(recheck) == {"launcher_alive", "pidfd_bound",
                                         "channel_exact", "readiness"}
                    and all(type(recheck[key]) is bool and recheck[key] is True
                            for key in ("launcher_alive", "pidfd_bound",
                                        "channel_exact"))
                    and recheck["readiness"] == "R",
                    "fresh unarmed launcher recheck")
            self._owner(); self._clock()
            self.permit = OneShotGrantPermit(
                _PERMIT_KEY, self, self.intent, self.child_bound,
                self.exec_issued, self.binding, attempt_id)
            self._validate_chain()
            self.state = "SEALING_FOR_GRANT"
            self.journal.seal_active(_SEAL_KEY, self)
            self.state = "GRANT_ATTEMPTED"
            self.grant_consumed = True
            self.permit.used = True
            self._validate_permit()
            self.original_grant_channel.begin_attempt(_PERMIT_KEY)
            result = self.launcher.grant_once_model(
                self.binding, self.permit, self.binding.grant_channel)
            require(self.state == "GRANT_ATTEMPTED"
                    and self.journal.state == "SEALED_ACTIVE"
                    and type(result) is dict
                    and set(result) == {"bytes_written", "writer_eof", "channel"}
                    and type(result["bytes_written"]) is int
                    and result["bytes_written"] == 1
                    and result["writer_eof"] is True
                    and result["channel"] is self.binding.grant_channel,
                    "one-shot grant outcome is ambiguous")
            self._validate_permit()
            require(self.original_grant_channel.phase == "ATTEMPTED"
                    and self.original_grant_channel.writer_open is True,
                    "grant channel changed during callback")
            self.original_grant_channel.confirm_eof(_PERMIT_KEY)
            self._validate_chain()
            self.grant_result = {"bytes_written": 1, "writer_eof": True}
            self.state = "GRANT_PROTOCOL_COMPLETED"
            return self.snapshot()
        except BaseException as exc:
            self._poison(type(exc).__name__)
            return self.snapshot()

    def snapshot(self):
        classification = {
            "NEW": "PREGRANT_NEW",
            "INTENT_PERSISTED": "MODEL_INTENT_PERSISTED",
            "CHILD_BOUND_PERSISTED": "MODEL_CHILD_BOUND_PERSISTED",
            "GRANT_PROTOCOL_COMPLETED": "MODEL_GRANT_PROTOCOL_COMPLETED_EXEC_UNPROVEN",
            "UNKNOWN": "UNKNOWN",
        }.get(self.state, "UNKNOWN_BUSY")
        return {"schema": 1, "classification": classification,
                "request_id": self.request["request_id"],
                "journal_sequence": self.journal.sequence,
                "journal_state": self.journal.state,
                "child_may_exist": self.child_may_exist,
                "child_identity_known": self.owned_child_evidence is not None,
                "owned_child_evidence": copy.deepcopy(self.owned_child_evidence),
                "settlement_required": self.settlement_required,
                "grant_attempted": self.grant_consumed,
                "grant_result": copy.deepcopy(self.grant_result),
                "error": self.error,
                "runtime_authorized": False, "storage_authorized": False,
                "postcondition_verified": False, "exec_proven": False}


def construct_v2_controller_model(request, owner, journal_session,
                                  launcher_backend, started_ns, deadline_ns):
    """Explicit V2 construction branch; never reinterprets a V1 session."""
    require(type(request) is dict and type(request.get("schema")) is int
            and request["schema"] == 2,
            "exact V2 controller request required")
    require(type(journal_session) is V2JournalSessionModel
            and threading.current_thread() is journal_session.owner_thread
            and journal_session.controller is None,
            "fresh V2 journal session required")
    require(integer(started_ns) and integer(deadline_ns, 1)
            and deadline_ns > started_ns,
            "V2 controller deadline required")
    controller = PreGrantController(
        request, owner, journal_session, launcher_backend,
        started_ns, deadline_ns)
    require(journal_session.controller is controller,
            "V2 controller did not claim its session")
    return controller


def prepare_pregrant_model(request, owner, journal_backend, launcher_backend):
    require(getattr(launcher_backend, "model_only", None) is True,
            "model-only launcher backend required")
    request = SUP.validate_request(copy.deepcopy(request))
    require(request["purpose"] == "DISPOSABLE_KERNEL_LAB"
            and request["argv"] == ["/usr/bin/true"]
            and request["executable"]["path"] == "/usr/bin/true",
            "pre-grant model is fixed to the disposable true fixture")
    owner = SUP.validate_owner(copy.deepcopy(owner), request["boot_id"])
    started = launcher_backend.monotonic_ns_model()
    require(integer(started), "pre-grant start clock")
    deadline = started + min(request["timeout_ms"], 5000) * 1000000
    journal = JournalSessionModel(
        journal_backend, request["request_id"], owner)
    return PreGrantController(
        request, owner, journal, launcher_backend, started, deadline)
