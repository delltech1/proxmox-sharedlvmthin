#!/usr/bin/python3
"""Closed PREGRANT V2 request/enrollment boundary; no process or grant I/O."""

import copy
import hashlib
import threading

import prelive_launcher_identity_model as IDENTITY
import prelive_pregrant_composition as PREGRANT
import prelive_supervisor_model as SUPERVISOR


class Refusal(RuntimeError):
    pass


_BINDING_KEY = object()
_CHAIN_KEY = object()
_EVENT_KEY = object()
_CLAIM_KEY = object()
_PREEXEC_AUTHORITY_KEY = object()
V2_REQUEST_FIELDS = frozenset(SUPERVISOR.REQUEST_FIELDS | {"launcher_identity"})
EVENT_ORDER = ("INTENT", "CHILD_BOUND", "EXEC_ISSUED")


def require(value, message):
    if not value:
        raise Refusal(message)


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _builtin_tree(value, path="request"):
    """Reject callbacks hidden in container/scalar subclasses before copying."""
    if type(value) is dict:
        require(all(type(key) is str for key in value),
                f"{path} has a non-builtin key")
        for key, item in value.items():
            _builtin_tree(item, f"{path}.{key}")
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _builtin_tree(item, f"{path}[{index}]")
        return
    require(type(value) in (str, int, bool, type(None)),
            f"{path} has a non-builtin value")


def _projection(value):
    return {"path": value["path_hint"], "sha256": value["content_sha256"],
            "dev": value["dev"], "inode": value["inode"]}


def validate_v2_request(value):
    _builtin_tree(value)
    require(type(value) is dict and frozenset(value) == V2_REQUEST_FIELDS,
            "closed PREGRANT V2 request required")
    require(type(value["schema"]) is int and value["schema"] == 2,
            "PREGRANT V2 schema required")
    try:
        descriptor = IDENTITY._descriptor(value["launcher_identity"])
    except IDENTITY.Refusal as exc:
        raise Refusal(str(exc)) from exc
    legacy = copy.deepcopy(value)
    legacy.pop("launcher_identity")
    legacy["schema"] = 1
    try:
        common = SUPERVISOR.validate_request(legacy)
    except SUPERVISOR.Refusal as exc:
        raise Refusal(str(exc)) from exc
    run = descriptor["run_binding"]
    payload = descriptor["payload"]
    require(run["request_id"] == common["request_id"]
            and run["boot_id"] == common["boot_id"],
            "V2 descriptor/request identity mismatch")
    require(payload["argv"] == common["argv"]
            and payload["environment"] == common["environment"]
            and _projection(payload["object"]) == common["executable"],
            "V2 payload projection mismatch")
    require(_projection(descriptor["interpreter"]) == common["launcher"],
            "V2 interpreter projection mismatch")
    frozen = copy.deepcopy(common)
    frozen["schema"] = 2
    frozen["launcher_identity"] = descriptor
    return frozen


class V2RequestBinding:
    def __init__(self, key, request, owner, started_ns, deadline_ns,
                 identity_handle):
        require(key is _BINDING_KEY, "V2 binding construction is private")
        self.request = request
        self.request_digest = SUPERVISOR.digest(request)
        self.owner = owner
        self.started_ns = started_ns
        self.deadline_ns = deadline_ns
        self.identity_handle = identity_handle
        self._identity_handle = identity_handle
        self.descriptor_digest = identity_handle.descriptor_digest
        self.owner_thread = threading.current_thread()
        self.state = "ENROLLED_V2"
        self.used = False
        self.controller_claim = None

    def __copy__(self):
        raise Refusal("V2 request binding is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("V2 request binding is noncopyable")


def bind_v2_request(value, owner, started_ns):
    request = validate_v2_request(value)
    _builtin_tree(owner, "owner")
    try:
        owner = SUPERVISOR.validate_owner(
            copy.deepcopy(owner), request["boot_id"])
    except SUPERVISOR.Refusal as exc:
        raise Refusal(str(exc)) from exc
    require(integer(started_ns), "V2 start clock")
    deadline_ns = started_ns + min(request["timeout_ms"], 5000) * 1000000
    descriptor = request["launcher_identity"]
    require(descriptor["run_binding"]["supervisor"] == owner,
            "V2 supervisor binding mismatch")
    require(descriptor["run_binding"]["unarmed_deadline_ns"] == deadline_ns,
            "V2 absolute deadline mismatch")
    try:
        identity_handle = IDENTITY.enroll_model(descriptor)
    except IDENTITY.Refusal as exc:
        raise Refusal(str(exc)) from exc
    return V2RequestBinding(
        _BINDING_KEY, request, owner, started_ns, deadline_ns, identity_handle)


def _validate_binding_phase(binding, binding_state, used, identity_state):
    require(type(binding) is V2RequestBinding
            and threading.current_thread() is binding.owner_thread
            and type(binding.state) is str and binding.state == binding_state
            and type(binding.used) is bool and binding.used is used
            and type(binding.request_digest) is str
            and IDENTITY.HEX64.fullmatch(binding.request_digest)
            and type(binding.descriptor_digest) is str
            and IDENTITY.HEX64.fullmatch(binding.descriptor_digest),
            "active V2 request binding required")
    current = validate_v2_request(binding.request)
    _builtin_tree(binding.owner, "stored owner")
    try:
        current_owner = SUPERVISOR.validate_owner(
            copy.deepcopy(binding.owner), current["boot_id"])
    except SUPERVISOR.Refusal as exc:
        raise Refusal(str(exc)) from exc
    require(integer(binding.started_ns)
            and integer(binding.deadline_ns, 1)
            and binding.deadline_ns == binding.started_ns
            + min(current["timeout_ms"], 5000) * 1000000,
            "V2 stored deadline changed")
    require(SUPERVISOR.digest(current) == binding.request_digest
            and current["launcher_identity"]
            ["run_binding"]["supervisor"] == current_owner
            and current["launcher_identity"]
            ["run_binding"]["unarmed_deadline_ns"] == binding.deadline_ns,
            "V2 request authority changed")
    require(type(binding.identity_handle) is IDENTITY.LauncherIdentityHandle
            and binding.identity_handle is binding._identity_handle
            and binding.identity_handle.owner_thread is binding.owner_thread
            and type(binding.identity_handle.state) is str
            and binding.identity_handle.state == identity_state
            and type(binding.identity_handle.descriptor_digest) is str
            and IDENTITY.HEX64.fullmatch(
                binding.identity_handle.descriptor_digest)
            and binding.identity_handle.descriptor_digest
            == binding.descriptor_digest,
            "V2 identity enrollment changed")
    try:
        current_descriptor = IDENTITY._descriptor(
            binding.identity_handle.descriptor)
    except IDENTITY.Refusal as exc:
        raise Refusal(str(exc)) from exc
    require(IDENTITY.digest(current_descriptor) == binding.descriptor_digest
            and current_descriptor == current["launcher_identity"],
            "V2 identity descriptor changed")
    return current


def validate_current_binding(binding):
    return _validate_binding_phase(
        binding, "ENROLLED_V2", False, "ENROLLED")


class V2ControllerClaim:
    def __init__(self, key, binding, controller, session,
                 pregrant_control_origin):
        require(key is _CLAIM_KEY, "V2 controller claim construction is private")
        self.binding = binding
        self.controller = controller
        self.session = session
        self.backend = session.backend
        self._backend = session.backend
        self.pre_fork_ticket = controller.pre_fork_ticket
        self.pregrant_control_origin = pregrant_control_origin
        self.owner_thread = threading.current_thread()
        self.used = False
        self._owner_bytes = SUPERVISOR.canonical(controller.owner)
        self._started_ns = controller.started_ns
        self._deadline_ns = controller.unarmed_deadline_ns
        self._channel_token = pregrant_control_origin.token
        self._pre_fork_binding = controller.pre_fork_ticket.binding
        self._pre_fork_supervisor_bytes = SUPERVISOR.canonical(
            controller.pre_fork_ticket.supervisor)
        self._sealed = True

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise Refusal("V2 controller claim is immutable")
        object.__setattr__(self, name, value)

    def _mark_used(self, key):
        require(key is _CLAIM_KEY and self.used is False,
                "V2 controller claim already used")
        object.__setattr__(self, "used", True)

    def __copy__(self):
        raise Refusal("V2 controller claim is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("V2 controller claim is noncopyable")


def _validate_controller_claim(claim, binding, session, used):
    require(type(claim) is V2ControllerClaim
            and type(claim.controller) is PREGRANT.PreGrantController,
            "exact V2 controller claim changed")
    _builtin_tree(binding.request, "V2 stored request")
    current_request = validate_v2_request(binding.request)
    boot_id = current_request["boot_id"]
    _builtin_tree(claim.controller.owner, "V2 controller owner")
    _builtin_tree(claim.pre_fork_ticket.supervisor,
                  "V2 pre-fork supervisor")
    try:
        controller_owner = SUPERVISOR.validate_owner(
            copy.deepcopy(claim.controller.owner), boot_id)
        ticket_owner = SUPERVISOR.validate_owner(
            copy.deepcopy(claim.pre_fork_ticket.supervisor),
            boot_id)
    except SUPERVISOR.Refusal as exc:
        raise Refusal(str(exc)) from exc
    require(type(claim) is V2ControllerClaim
            and threading.current_thread() is claim.owner_thread
            and claim.owner_thread is binding.owner_thread
            and claim.binding is binding
            and claim.session is session
            and claim.backend is claim._backend
            and session.backend is claim._backend
            and type(claim.used) is bool and claim.used is used
            and type(claim.controller) is PREGRANT.PreGrantController
            and session.controller is claim.controller
            and claim.controller.journal is session
            and type(claim.controller.model_contract) is str
            and claim.controller.model_contract == "PREGRANT_V2_MODEL_ONLY"
            and type(claim.controller._construction_contract) is str
            and claim.controller._construction_contract
            == "PREGRANT_V2_MODEL_ONLY"
            and claim.controller._construction_journal is session
            and type(claim.controller.state) is str
            and claim.controller.state == "NEW"
            and claim.controller.request is binding.request
            and SUPERVISOR.canonical(controller_owner)
            == claim._owner_bytes
            and claim.controller.owner_thread is claim.owner_thread
            and type(claim.controller.started_ns) is int
            and claim.controller.started_ns == claim._started_ns
            and type(claim.controller.unarmed_deadline_ns) is int
            and claim.controller.unarmed_deadline_ns == claim._deadline_ns
            and claim.controller.v2_identity_claim is claim
            and claim.controller._v2_identity_claim is claim
            and claim.controller.pre_fork_ticket is claim.pre_fork_ticket
            and claim.pre_fork_ticket.binding is claim._pre_fork_binding
            and SUPERVISOR.canonical(ticket_owner)
            == claim._pre_fork_supervisor_bytes
            and type(claim.pre_fork_ticket.used) is bool
            and claim.pre_fork_ticket.used is False
            and claim.controller.original_grant_channel
            is claim.pregrant_control_origin
            and claim.controller.grant_channel
            is claim.pregrant_control_origin
            and type(claim.pregrant_control_origin)
            is PREGRANT.ModelGrantChannel
            and claim.pregrant_control_origin.owner_thread
            is claim.owner_thread
            and type(claim.pregrant_control_origin.token) is str
            and claim.pregrant_control_origin.token == claim._channel_token
            and type(claim.pregrant_control_origin.writer_open) is bool
            and claim.pregrant_control_origin.writer_open is True
            and type(claim.pregrant_control_origin.phase) is str
            and claim.pregrant_control_origin.phase == "WRITER_OPEN",
            "exact V2 controller claim changed")
    return claim


def create_v2_journal_session(backend):
    try:
        return PREGRANT.create_v2_journal_session_model(backend)
    except PREGRANT.Refusal as exc:
        raise Refusal(str(exc)) from exc


def v2_intent_payload(request, owner):
    """Pure canonical INTENT payload; mints no claim or capability."""
    current = validate_v2_request(request)
    _builtin_tree(owner, "V2 INTENT owner")
    try:
        current_owner = SUPERVISOR.validate_owner(
            copy.deepcopy(owner), current["boot_id"])
    except SUPERVISOR.Refusal as exc:
        raise Refusal(str(exc)) from exc
    descriptor = current["launcher_identity"]
    require(current_owner == descriptor["run_binding"]["supervisor"],
            "V2 INTENT owner mismatch")
    return {"request": current,
            "request_digest": SUPERVISOR.digest(current),
            "descriptor_digest": IDENTITY.digest(descriptor),
            "owner": current_owner,
            "unarmed_deadline_ns": descriptor["run_binding"]
            ["unarmed_deadline_ns"],
            "fd_graph_digest": descriptor["run_binding"]
            ["fd_graph_digest"],
            "enrollment_scope": "MODEL_V2_IDENTITY_ENROLLMENT"}


def encode_v2_intent(request, owner):
    """Pure canonical first-event encoder; performs no persistence."""
    payload = v2_intent_payload(request, owner)
    event = {"schema": 2, "request_id": payload["request"]["request_id"],
             "sequence": 1, "kind": "INTENT",
             "previous_digest": "0" * 64, "payload": payload}
    _builtin_tree(event, "V2 INTENT event")
    return SUPERVISOR.canonical(event)


def construct_and_claim_v2_controller(binding, session, launcher_backend):
    validate_current_binding(binding)
    require(binding.controller_claim is None,
            "V2 controller already claimed")
    try:
        controller = PREGRANT.construct_v2_controller_model(
            binding.request, binding.owner, session, launcher_backend,
            binding.started_ns, binding.deadline_ns)
    except PREGRANT.Refusal as exc:
        raise Refusal(str(exc)) from exc
    pregrant_control_origin = controller.original_grant_channel
    require(type(controller) is PREGRANT.PreGrantController
            and threading.current_thread() is controller.owner_thread
            and controller.owner_thread is binding.owner_thread
            and type(controller.state) is str and controller.state == "NEW"
            and controller.model_contract == "PREGRANT_V2_MODEL_ONLY"
            and controller.request is binding.request
            and controller.owner == binding.owner
            and type(controller.started_ns) is int
            and controller.started_ns == binding.started_ns
            and type(controller.unarmed_deadline_ns) is int
            and controller.unarmed_deadline_ns == binding.deadline_ns
            and type(session) is PREGRANT.V2JournalSessionModel
            and controller.journal is session
            and session.controller is controller
            and controller.original_grant_channel is pregrant_control_origin
            and controller.grant_channel is pregrant_control_origin
            and controller.pre_fork_ticket.used is False,
            "exact V2 controller claim required")
    claim = V2ControllerClaim(
        _CLAIM_KEY, binding, controller, session, pregrant_control_origin)
    binding.controller_claim = claim
    controller.v2_identity_claim = claim
    controller._v2_identity_claim = claim
    _validate_controller_claim(claim, binding, session, False)
    return claim


class V2EventCapability:
    def __init__(self, key, chain, kind, sequence, event_bytes, event_digest,
                 previous_digest):
        require(key is _EVENT_KEY, "V2 event capability construction is private")
        self.chain = chain
        self.kind = kind
        self.sequence = sequence
        self.event_bytes = event_bytes
        self.event_digest = event_digest
        self.previous_digest = previous_digest
        self.owner_thread = threading.current_thread()
        self.used = False
        self._sealed = True

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise Refusal("V2 event capability is immutable")
        object.__setattr__(self, name, value)

    def __copy__(self):
        raise Refusal("V2 event capability is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("V2 event capability is noncopyable")


class V2EventChain:
    def __init__(self, key, binding, backend, controller_claim):
        require(key is _CHAIN_KEY, "V2 event chain construction is private")
        self.binding = binding
        self._binding = binding
        self.backend = backend
        self._backend = backend
        self.owner_thread = threading.current_thread()
        self.state = "READY_INTENT"
        self.sequence = 0
        self.last_digest = "0" * 64
        self.capabilities = []
        self._capabilities = ()
        self._event_bytes = ()
        self._event_digests = ()
        self.attempted_event_bytes = None
        self.attempted_event_digest = None
        self.write_may_exist = False
        self.unarmed_observation = None
        self.unarmed_observation_digest = None
        self.attempt_id = None
        self._issued_attempt_id = None
        self.attempt_consumed = False
        self.preexec_authority = None
        self._request_bytes = SUPERVISOR.canonical(binding.request)
        self._request_digest = binding.request_digest
        self._descriptor_digest = binding.descriptor_digest
        self._owner_bytes = SUPERVISOR.canonical(binding.owner)
        self._started_ns = binding.started_ns
        self._deadline_ns = binding.deadline_ns
        self._identity_handle = binding.identity_handle
        self.controller_claim = controller_claim
        self._controller_claim = controller_claim
        self.controller = controller_claim.controller
        self.pregrant_control_origin = controller_claim.pregrant_control_origin
        self._frozen_unarmed_bytes = None
        self._frozen_unarmed_digest = None
        self._frozen_child_bytes = None
        self._append_inflight = False
        self._attempted_bytes_private = None
        self._attempted_digest_private = None

    def __copy__(self):
        raise Refusal("V2 event chain is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("V2 event chain is noncopyable")

    def _poison(self):
        self.state = "UNKNOWN"
        if self._append_inflight:
            self.write_may_exist = True
            self.attempted_event_bytes = self._attempted_bytes_private
            self.attempted_event_digest = self._attempted_digest_private
        self._binding.state = "UNKNOWN_V2_EVENT_CHAIN"

    def _validate(self, state, identity_state, expected_attempt_id=None):
        require(type(self.binding) is V2RequestBinding
                and self.binding is self._binding
                and threading.current_thread() is self.owner_thread
                and self.owner_thread is self.binding.owner_thread
                and self.backend is self._backend
                and self.binding.identity_handle is self._identity_handle
                and _validate_controller_claim(
                    self.controller_claim, self._binding,
                    self._backend, True) is self.controller_claim
                and self.controller_claim is self._controller_claim
                and self.binding.controller_claim is self.controller_claim
                and self.controller_claim.used is True
                and self.controller_claim.binding is self._binding
                and self.controller_claim.controller is self.controller
                and self.controller_claim.session is self._backend
                and self.controller_claim.backend is self._backend.backend
                and self.controller_claim.pregrant_control_origin
                is self.pregrant_control_origin
                and self.controller_claim.pre_fork_ticket
                is self.controller.pre_fork_ticket
                and self._backend.chain is self
                and self._backend._chain is self
                and self._backend.controller_claim
                is self.controller_claim
                and self._backend.state == "READY"
                and type(self.controller) is PREGRANT.PreGrantController
                and self.controller.state == "NEW"
                and self.controller.request is self._binding.request
                and self.controller.journal is self._backend
                and self.controller.v2_identity_claim
                is self.controller_claim
                and self.controller._v2_identity_claim
                is self.controller_claim
                and self.controller.original_grant_channel
                is self.pregrant_control_origin
                and self.controller.grant_channel
                is self.pregrant_control_origin
                and type(self.state) is str and self.state == state
                and integer(self.sequence)
                and type(self.last_digest) is str
                and IDENTITY.HEX64.fullmatch(self.last_digest)
                and type(self.capabilities) is list
                and len(self.capabilities) == self.sequence
                and len(self._capabilities) == self.sequence
                and len(self._event_bytes) == self.sequence
                and len(self._event_digests) == self.sequence
                and all(public is private for public, private in zip(
                    self.capabilities, self._capabilities))
                and self.last_digest == (self._event_digests[-1]
                    if self._event_digests else "0" * 64),
                "V2 event chain authority changed")
        require((expected_attempt_id is None and self.attempt_id is None)
                or (type(expected_attempt_id) is str
                    and type(self.attempt_id) is str
                    and self.attempt_id == expected_attempt_id),
                "V2 attempt authority changed")
        previous = "0" * 64
        current = _validate_binding_phase(
            self.binding, "V2_EVENT_CHAIN_ACTIVE", True, identity_state)
        require(SUPERVISOR.canonical(current) == self._request_bytes
                and self.binding.request_digest == self._request_digest
                and self.binding.descriptor_digest == self._descriptor_digest
                and SUPERVISOR.canonical(self.binding.owner) == self._owner_bytes
                and type(self.binding.started_ns) is int
                and self.binding.started_ns == self._started_ns
                and type(self.binding.deadline_ns) is int
                and self.binding.deadline_ns == self._deadline_ns,
                "V2 frozen enrollment changed")
        if identity_state in ("UNARMED_BOUND", "MODEL_PREEXEC_VALIDATED"):
            current_child = IDENTITY._child(
                self._identity_handle.child, self._identity_handle.descriptor)
            _builtin_tree(self.unarmed_observation,
                          "stored unarmed observation")
            current_observation = IDENTITY._unarmed_observation(
                self.unarmed_observation, self._identity_handle.descriptor)
            require(type(self._identity_handle.unarmed_digest) is str
                    and self._identity_handle.unarmed_digest
                    == self._frozen_unarmed_digest
                    and type(self.unarmed_observation_digest) is str
                    and self.unarmed_observation_digest
                    == self._frozen_unarmed_digest
                    and SUPERVISOR.canonical(current_child)
                    == self._frozen_child_bytes
                    and SUPERVISOR.canonical(current_observation)
                    == self._frozen_unarmed_bytes,
                    "V2 frozen child binding changed")
        for index, capability in enumerate(self.capabilities, 1):
            require(type(capability) is V2EventCapability
                    and capability.chain is self
                    and capability.owner_thread is self.owner_thread
                    and type(capability.sequence) is int
                    and capability.sequence == index
                    and type(capability.kind) is str
                    and capability.kind == EVENT_ORDER[index - 1]
                    and type(capability.used) is bool
                    and capability.used is False
                    and type(capability.previous_digest) is str
                    and capability.previous_digest == previous
                    and type(capability.event_bytes) is bytes
                    and capability.event_bytes == self._event_bytes[index - 1]
                    and type(capability.event_digest) is str
                    and capability.event_digest
                    == self._event_digests[index - 1]
                    and hashlib.sha256(capability.event_bytes).hexdigest()
                    == capability.event_digest,
                    "V2 event capability chain changed")
            previous = capability.event_digest
        return current

    def _append(self, kind, payload, ready_state, next_state, identity_state,
                expected_attempt_id=None):
        if self.state != ready_state:
            self._poison()
            raise Refusal("V2 event order or reentrancy")
        try:
            current = self._validate(
                ready_state, identity_state, expected_attempt_id)
            sequence = self.sequence + 1
            require(kind == EVENT_ORDER[self.sequence], "V2 event order")
            event = {"schema": 2, "request_id": current["request_id"],
                     "sequence": sequence, "kind": kind,
                     "previous_digest": self.last_digest,
                     "payload": payload}
            _builtin_tree(event, "event")
            event_bytes = SUPERVISOR.canonical(event)
            event_digest = hashlib.sha256(event_bytes).hexdigest()
            self.attempted_event_bytes = event_bytes
            self.attempted_event_digest = event_digest
            self._attempted_bytes_private = event_bytes
            self._attempted_digest_private = event_digest
            self._append_inflight = True
            self.write_may_exist = True
            self.state = "APPENDING"
            ack = self.backend.append_v2_model(event_bytes)
            require(self.state == "APPENDING"
                    and self.binding.state == "V2_EVENT_CHAIN_ACTIVE"
                    and self.attempted_event_bytes == event_bytes
                    and self.attempted_event_digest == event_digest
                    and self._append_inflight is True
                    and self._attempted_bytes_private == event_bytes
                    and self._attempted_digest_private == event_digest
                    and ((expected_attempt_id is None and self.attempt_id is None)
                         or self.attempt_id == expected_attempt_id),
                    "V2 event append callback changed authority")
            self.state = ready_state
            self._validate(ready_state, identity_state, expected_attempt_id)
            _builtin_tree(ack, "ack")
            require(type(ack) is dict and frozenset(ack) == {
                        "contract", "kind", "sequence", "byte_length",
                        "sha256", "persisted"}
                    and type(ack["contract"]) is str
                    and ack["contract"] == "MODEL_V2_DURABLE_ACK"
                    and type(ack["kind"]) is str and ack["kind"] == kind
                    and type(ack["sequence"]) is int
                    and ack["sequence"] == sequence
                    and type(ack["byte_length"]) is int
                    and ack["byte_length"] == len(event_bytes)
                    and type(ack["sha256"]) is str
                    and ack["sha256"] == event_digest
                    and type(ack["persisted"]) is bool
                    and ack["persisted"] is True,
                    "V2 durable ACK mismatch")
            self._validate(ready_state, identity_state, expected_attempt_id)
            require(self.state == ready_state
                    and self.binding is self._binding
                    and self._binding.state == "V2_EVENT_CHAIN_ACTIVE"
                    and self._append_inflight is True
                    and self._attempted_bytes_private == event_bytes
                    and self._attempted_digest_private == event_digest,
                    "V2 final append authority changed")
            capability = V2EventCapability(
                _EVENT_KEY, self, kind, sequence, event_bytes, event_digest,
                self.last_digest)
            self.capabilities.append(capability)
            self._capabilities = self._capabilities + (capability,)
            self._event_bytes = self._event_bytes + (event_bytes,)
            self._event_digests = self._event_digests + (event_digest,)
            self.sequence = sequence
            self.last_digest = event_digest
            self.attempted_event_bytes = None
            self.attempted_event_digest = None
            self._attempted_bytes_private = None
            self._attempted_digest_private = None
            self._append_inflight = False
            self.write_may_exist = False
            self.state = next_state
            return capability
        except BaseException:
            self._poison()
            raise

    def persist_intent(self):
        try:
            if self.state != "READY_INTENT":
                raise Refusal("V2 intent phase")
            request = self._validate("READY_INTENT", "ENROLLED")
            payload = v2_intent_payload(request, self.binding.owner)
            require(payload["request_digest"] == self.binding.request_digest
                    and payload["descriptor_digest"]
                    == self.binding.descriptor_digest
                    and payload["unarmed_deadline_ns"]
                    == self.binding.deadline_ns,
                    "V2 INTENT binding changed")
            return self._append(
                "INTENT", payload, "READY_INTENT", "READY_CHILD_BOUND",
                "ENROLLED")
        except BaseException:
            self._poison()
            raise

    def persist_child_bound_fixture(self, first, second):
        if self.state != "READY_CHILD_BOUND":
            self._poison()
            raise Refusal("V2 child-bound phase")
        try:
            self._validate("READY_CHILD_BOUND", "ENROLLED")
            first = IDENTITY._unarmed_observation(
                first, self.binding.identity_handle.descriptor)
            second = IDENTITY._unarmed_observation(
                second, self.binding.identity_handle.descriptor)
            result = IDENTITY.bind_unarmed_model(
                self.binding.identity_handle, first, second)
            require(result["classification"]
                    == "MODEL_FORKED_LAUNCHER_BINDING_CONSISTENT",
                    "V2 model child fixture binding")
            self.unarmed_observation = copy.deepcopy(first)
            self.unarmed_observation_digest = IDENTITY.digest(first)
            self._frozen_unarmed_bytes = SUPERVISOR.canonical(first)
            self._frozen_unarmed_digest = self.unarmed_observation_digest
            self._frozen_child_bytes = SUPERVISOR.canonical(first["child"])
            payload = {
                "intent_digest": self.capabilities[0].event_digest,
                "descriptor_digest": self.binding.descriptor_digest,
                "child": copy.deepcopy(first["child"]),
                "unarmed_observation": copy.deepcopy(first),
                "unarmed_observation_digest": self.unarmed_observation_digest,
                "interpreter_projection": {
                    "dev": first["interpreter"]["dev"],
                    "inode": first["interpreter"]["inode"],
                    "proc_cmdline_sha256": first["proc_cmdline_sha256"]},
                "fd_graph_digest": first["fd_graph_digest"],
                "evidence_scope": "SYNTHETIC_MODEL_FIXTURE"}
            return self._append(
                "CHILD_BOUND", payload, "READY_CHILD_BOUND",
                "READY_EXEC_ISSUED", "UNARMED_BOUND")
        except BaseException:
            self._poison()
            raise

    def persist_exec_issued(self, attempt_id):
        try:
            if self.state != "READY_EXEC_ISSUED":
                raise Refusal("V2 exec-issued phase")
            self._validate("READY_EXEC_ISSUED", "UNARMED_BOUND")
            require(type(attempt_id) is str
                    and IDENTITY.HEX32.fullmatch(attempt_id),
                    "V2 attempt identity")
            self.attempt_id = attempt_id
            payload = {"intent_digest": self.capabilities[0].event_digest,
                   "child_bound_digest": self.capabilities[1].event_digest,
                   "descriptor_digest": self.binding.descriptor_digest,
                   "unarmed_observation_digest": self.unarmed_observation_digest,
                   "attempt_id": attempt_id,
                   "unarmed_deadline_ns": self.binding.deadline_ns,
                   "grant_protocol": "ONE_BYTE_G_THEN_WRITER_CLOSE",
                       "dispatch_proven": False, "exec_proven": False}
            capability = self._append(
                "EXEC_ISSUED", payload, "READY_EXEC_ISSUED",
                "SEALED_MODEL_ONLY", "UNARMED_BOUND", attempt_id)
            self._issued_attempt_id = attempt_id
            return capability
        except BaseException:
            self._poison()
            raise


class V2PreExecAuthority:
    def __init__(self, key, chain, capability, observation, now_ns):
        require(key is _PREEXEC_AUTHORITY_KEY,
                "V2 pre-exec authority construction is private")
        self.chain = chain
        self.controller_claim = chain.controller_claim
        self.controller = chain.controller
        self.session = chain.backend
        self.pregrant_control_origin = chain.pregrant_control_origin
        self.identity_handle = chain.binding.identity_handle
        self.capability = capability
        self.attempt_id = chain.attempt_id
        self.observation_bytes = SUPERVISOR.canonical(observation)
        self.observation_digest = IDENTITY.digest(observation)
        self.now_ns = now_ns
        self.owner_thread = threading.current_thread()
        self.used = False
        self._sealed = True

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise Refusal("V2 pre-exec authority is immutable")
        object.__setattr__(self, name, value)

    def __copy__(self):
        raise Refusal("V2 pre-exec authority is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("V2 pre-exec authority is noncopyable")

    def _consume(self, key):
        require(key is _PREEXEC_AUTHORITY_KEY and self.used is False,
                "V2 pre-exec authority already consumed")
        object.__setattr__(self, "used", True)


def mint_v2_preexec_authority(chain, fresh_observation, now_ns):
    require(type(chain) is V2EventChain, "exact V2 event chain required")
    try:
        require(chain.state == "SEALED_MODEL_ONLY"
                and chain.preexec_authority is None
                and chain.attempt_consumed is False
                and type(chain._issued_attempt_id) is str
                and IDENTITY.HEX32.fullmatch(chain._issued_attempt_id)
                and chain.attempt_id == chain._issued_attempt_id,
                "sealed V2 EXEC chain required")
        chain._validate(
            "SEALED_MODEL_ONLY", "UNARMED_BOUND", chain.attempt_id)
        require(fresh_observation is not chain.unarmed_observation,
                "fresh pre-exec observation required")
        _builtin_tree(fresh_observation, "fresh pre-exec observation")
        require(integer(now_ns) and now_ns >= chain.binding.started_ns
                and now_ns < chain.binding.deadline_ns,
                "fresh pre-exec time required")
        capability, result = IDENTITY.validate_preexec_model(
            chain.binding.identity_handle, fresh_observation, now_ns)
        require(type(capability) is IDENTITY.PreExecModelCapability
                and chain.binding.identity_handle.preexec_capability
                is capability
                and result["classification"]
                == "MODEL_PREEXEC_BINDING_CONSISTENT"
                and result["runtime_authorized"] is False
                and result["storage_authorized"] is False
                and result["exec_proven"] is False,
                "exact V2 pre-exec capability required")
        authority = V2PreExecAuthority(
            _PREEXEC_AUTHORITY_KEY, chain, capability,
            fresh_observation, now_ns)
        chain.preexec_authority = authority
        chain.state = "PREEXEC_READY"
        chain._validate(
            "PREEXEC_READY", "MODEL_PREEXEC_VALIDATED", chain.attempt_id)
        return authority
    except BaseException:
        chain._poison()
        raise


def consume_v2_preexec_authority(authority, now_ns):
    require(type(authority) is V2PreExecAuthority,
            "exact V2 pre-exec authority required")
    chain = authority.chain
    try:
        require(threading.current_thread() is authority.owner_thread
                and authority.used is False
                and chain.preexec_authority is authority
                and chain.attempt_consumed is False,
                "unused V2 pre-exec authority required")
        chain._validate(
            "PREEXEC_READY", "MODEL_PREEXEC_VALIDATED", chain.attempt_id)
        handle = chain.binding.identity_handle
        capability = authority.capability
        require(type(capability) is IDENTITY.PreExecModelCapability,
                "exact V2 identity capability required")
        require(type(capability.descriptor_digest) is str
                and IDENTITY.HEX64.fullmatch(capability.descriptor_digest)
                and type(capability.observation_digest) is str
                and IDENTITY.HEX64.fullmatch(capability.observation_digest),
                "strict V2 identity capability digests required")
        _builtin_tree(capability.observation,
                      "stored pre-exec capability observation")
        normalized_observation = IDENTITY._unarmed_observation(
            capability.observation, handle.descriptor)
        require(handle is authority.identity_handle
                and handle.preexec_capability is capability
                and handle.preexec_used is True
                and capability.handle is handle
                and capability.owner_thread is authority.owner_thread
                and capability.used is False
                and capability.descriptor_digest == handle.descriptor_digest
                and type(capability.now_ns) is int
                and capability.now_ns == authority.now_ns
                and capability.observation_digest
                == authority.observation_digest
                and IDENTITY.digest(normalized_observation)
                == capability.observation_digest
                and SUPERVISOR.canonical(normalized_observation)
                == authority.observation_bytes
                and authority.controller_claim is chain.controller_claim
                and authority.controller is chain.controller
                and authority.session is chain.backend
                and authority.pregrant_control_origin
                is chain.pregrant_control_origin
                and authority.attempt_id == chain.attempt_id
                and chain.attempt_id == chain._issued_attempt_id
                and integer(now_ns) and now_ns >= authority.now_ns
                and now_ns < chain.binding.deadline_ns,
                "V2 pre-exec authority changed or expired")
        chain.state = "CONSUMING_PREEXEC_AUTHORITY"
        chain._validate(
            "CONSUMING_PREEXEC_AUTHORITY", "MODEL_PREEXEC_VALIDATED",
            chain.attempt_id)
        require(chain.preexec_authority is authority
                and authority.used is False
                and capability.used is False
                and chain.attempt_consumed is False,
                "V2 final pre-exec authority changed")
        authority._consume(_PREEXEC_AUTHORITY_KEY)
        capability.used = True
        chain.attempt_consumed = True
        chain.state = "PREEXEC_AUTHORITY_CONSUMED"
        return {"schema": 1,
                "classification": "MODEL_PREEXEC_AUTHORITY_CONSUMED",
                "attempt_id": chain.attempt_id,
                "runtime_authorized": False,
                "storage_authorized": False,
                "grant_attempted": False,
                "exec_proven": False}
    except BaseException:
        chain._poison()
        raise


def start_v2_event_chain(binding, backend):
    validate_current_binding(binding)
    backend_type = type(backend)
    require(backend_type is PREGRANT.V2JournalSessionModel
            and backend_type.__dict__.get("model_only_v2_journal") is True
            and callable(backend_type.__dict__.get("append_v2_model")),
            "model-only V2 journal session required")
    claim = _validate_controller_claim(
        binding.controller_claim, binding, backend, False)
    claim._mark_used(_CLAIM_KEY)
    binding.used = True
    binding.state = "V2_EVENT_CHAIN_ACTIVE"
    chain = V2EventChain(_CHAIN_KEY, binding, backend, claim)
    try:
        PREGRANT.claim_v2_event_chain_model(backend, chain, claim)
    except PREGRANT.Refusal as exc:
        binding.state = "UNKNOWN_V2_EVENT_CHAIN"
        chain.state = "UNKNOWN"
        raise Refusal(str(exc)) from exc
    return chain
