#!/usr/bin/python3
"""Source-model allocated status EOF evidence; no live read or release."""

import copy
import hashlib
import os
import threading
import types

import prelive_abort_fork_split as ABORT
import prelive_allocated_graph_enrollment as GRAPH
import prelive_capture_settlement as CAPTURE
import prelive_descendant_bridge as DBRIDGE
import prelive_owned_child as OWNED
import prelive_real_fd_adapter as FDS


_STATUS_KEY = object()
_WATERMARK_KEY = object()
_ATTEMPT_SET = ABORT.AbortForkAttempt.__dict__["_set"]
_VALIDATE_TERMINAL = DBRIDGE._validate_allocated_terminal_evidence
_RESERVE_TERMINAL = DBRIDGE._reserve_allocated_terminal_evidence
_STRICT_STREAM_SNAPSHOT = CAPTURE._strict_stream_snapshot
_VALIDATE_TRANSFERRED_NORMAL = CAPTURE.validate_transferred_normal_completion
_STRICT_BOUND_REQUEST = GRAPH._strict_bound_request


class Refusal(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise Refusal(message)


def _status_authority(evidence):
    authority = evidence._allocation_authority
    require(type(authority) is tuple
            and len(authority) == len(FDS._ALL_ROLES),
            "strict allocated status authority")
    selected = None
    for entry in authority:
        require(type(entry) is tuple and len(entry) == 2
                and type(entry[0]) is str
                and type(entry[1]) is tuple
                and len(entry[1]) == len(FDS._IDENTITY_KEYS)
                and all(type(value) is int for value in entry[1]),
                "strict allocated status authority entry")
        if entry[0] == "status_parent":
            selected = entry[1]
    require(selected is not None, "allocated status authority absent")
    return selected


class _StatusWatermark:
    def __init__(self, key, value):
        require(key is _WATERMARK_KEY and type(value) is int,
                "private allocated status watermark")
        self.value = value
        self._sealed = True

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise Refusal("allocated status watermark is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        raise Refusal("allocated status watermark is immutable")

    def advance(self, key, value, deadline_ns):
        require(key is _WATERMARK_KEY
                and type(value) is int and type(deadline_ns) is int
                and self.value <= value < deadline_ns,
                "allocated status watermark advance")
        object.__setattr__(self, "value", value)


_WATERMARK_ADVANCE = _StatusWatermark.__dict__["advance"]


class AllocatedStatusTailResult:
    """One-shot modeled status read; all outcomes remain nonqualified."""
    def __init__(self, key, evidence):
        require(key is _STATUS_KEY,
                "allocated status result construction is private")
        self.terminal_evidence = evidence
        self.result = evidence._result
        self.bridge = evidence._bridge
        self.attempt = evidence._attempt
        self.domain = evidence._domain
        self.owner_thread = threading.current_thread()
        self.deadline_ns = evidence._deadline_ns
        require(type(evidence._source.descendant_capability.finished_ns) is int
                and type(evidence._attempt.last_clock_ns) is int,
                "strict allocated status attempt watermark")
        self.initial_watermark_ns = max(
            evidence._watermark_ns,
            evidence._source.descendant_capability.finished_ns,
            evidence._attempt.last_clock_ns)
        self.watermark = _StatusWatermark(
            _WATERMARK_KEY, self.initial_watermark_ns)
        self.status_identity = _status_authority(evidence)
        self.status_fd = self.status_identity[
            FDS._IDENTITY_KEYS.index("fd")]
        self.state = "OBSERVING"
        self.read_attempted = False
        self.read_returned = False
        self.read_kind = None
        self.read_data = None
        self.read_before_ns = None
        self.read_after_ns = None
        self._read_before_frozen = None
        self._read_observation = None
        self.eof_evidence = None
        self.error = None
        self._active = True
        self._poisoned = False
        self._read_latched = False
        self._fixed = True

    def __setattr__(self, name, value):
        if (getattr(self, "_fixed", False)
                and name in {"terminal_evidence", "result", "bridge",
                             "attempt", "domain", "owner_thread",
                             "deadline_ns", "initial_watermark_ns",
                             "watermark", "status_identity", "status_fd",
                             "_read_before_frozen", "_read_observation",
                             "_active", "_poisoned", "_read_latched",
                             "_fixed"}):
            raise Refusal("allocated status authority is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        if name in {"terminal_evidence", "result", "bridge", "attempt",
                    "domain", "owner_thread", "deadline_ns",
                    "initial_watermark_ns", "watermark", "status_identity",
                    "status_fd", "_read_before_frozen",
                    "_read_observation", "_active", "_poisoned",
                    "_read_latched", "_fixed"}:
            raise Refusal("allocated status authority is immutable")
        object.__delattr__(self, name)

    def __copy__(self):
        raise Refusal("allocated status result is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("allocated status result is noncopyable")

    def _poison(self, reason):
        object.__setattr__(self, "_poisoned", True)
        object.__setattr__(self, "_active", False)
        if self._read_latched:
            object.__setattr__(self, "read_attempted", True)
        object.__setattr__(self, "error", reason)
        object.__setattr__(self, "state", "UNKNOWN")

    def snapshot(self):
        return {"schema": 1, "classification": self.state,
                "read_attempted": self.read_attempted,
                "read_returned": self.read_returned,
                "read_kind": self.read_kind,
                "read_bytes": (None if self.read_data is None
                               else len(self.read_data)),
                "status_eof_verified": (
                    self.state == "MODEL_ALLOCATED_STATUS_EOF_NONQUALIFIED"
                    and self.eof_evidence is not None),
                "abort_protocol_verified": False,
                "descendants_qualified": False,
                "resources_closed": False,
                "runtime_authorized": False,
                "storage_authorized": False,
                "postcondition_verified": False,
                "error": self.error}


class AllocatedStatusEOFEvidence:
    """Opaque exact modeled status EOF; not protocol/release authority."""
    __slots__ = ("_operation", "_terminal_evidence", "_attempt",
                 "_status_identity", "_before_ns", "_after_ns",
                 "_deadline_ns", "_consumed", "_consumer", "_sealed")

    def __init__(self, key, operation):
        require(key is _STATUS_KEY,
                "allocated status EOF construction is private")
        object.__setattr__(self, "_operation", operation)
        object.__setattr__(self, "_terminal_evidence",
                           operation.terminal_evidence)
        object.__setattr__(self, "_attempt", operation.attempt)
        object.__setattr__(self, "_status_identity",
                           operation.status_identity)
        object.__setattr__(self, "_before_ns", operation.read_before_ns)
        object.__setattr__(self, "_after_ns", operation.read_after_ns)
        object.__setattr__(self, "_deadline_ns", operation.deadline_ns)
        object.__setattr__(self, "_consumed", False)
        object.__setattr__(self, "_consumer", None)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise Refusal("allocated status EOF evidence is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        raise Refusal("allocated status EOF evidence is immutable")

    def __copy__(self):
        raise Refusal("allocated status EOF evidence is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("allocated status EOF evidence is noncopyable")

    def snapshot(self):
        return {"schema": 1,
                "classification": "MODEL_ALLOCATED_STATUS_EOF_NONQUALIFIED",
                "before_ns": self._before_ns,
                "after_ns": self._after_ns,
                "deadline_ns": self._deadline_ns,
                "status_eof_verified": True,
                "abort_protocol_verified": False,
                "descendants_qualified": False,
                "resources_closed": False,
                "runtime_authorized": False,
                "storage_authorized": False,
                "postcondition_verified": False}


def _validate_operation(operation, expected_state="OBSERVING"):
    require(type(operation) is AllocatedStatusTailResult
            and type(expected_state) is str
            and threading.current_thread() is operation.owner_thread
            and type(operation.state) is str
            and operation.state == expected_state
            and type(operation._active) is bool
            and type(operation._poisoned) is bool
            and type(operation._read_latched) is bool
            and operation._poisoned is False
            and operation._active is (expected_state == "OBSERVING")
            and operation.error is None,
            "current allocated status operation required")
    evidence = operation.terminal_evidence
    _VALIDATE_TERMINAL(evidence, operation)
    attempt = operation.attempt
    require(operation.result is evidence._result
            and operation.bridge is evidence._bridge
            and operation.domain is evidence._domain
            and attempt is evidence._attempt
            and attempt.allocated_status_started is True
            and attempt.allocated_status_result is operation
            and attempt.allocated_status_eof_evidence
                is operation.eof_evidence
            and type(operation.deadline_ns) is int
            and operation.deadline_ns == evidence._deadline_ns
            and type(operation.initial_watermark_ns) is int
            and type(operation.watermark) is _StatusWatermark
            and type(operation.watermark.__dict__) is dict
            and all(type(key) is str
                    for key in operation.watermark.__dict__)
            and set(operation.watermark.__dict__) == {"value", "_sealed"}
            and type(operation.watermark._sealed) is bool
            and operation.watermark._sealed is True
            and type(operation.watermark.value) is int
            and evidence._watermark_ns
                <= operation.initial_watermark_ns
                <= operation.watermark.value < operation.deadline_ns
            and type(operation.status_identity) is tuple
            and operation.status_identity is _status_authority(evidence)
            and type(operation.status_fd) is int
            and operation.status_fd == operation.status_identity[
                FDS._IDENTITY_KEYS.index("fd")]
            and operation.status_fd > 2,
            "allocated status authority changed")
    handle = attempt.handle
    require(type(handle.bundle) is dict
            and type(handle.bundle.get("status_parent")) is dict
            and all(type(key) is str
                    for key in handle.bundle["status_parent"])
            and set(handle.bundle["status_parent"])
                == set(FDS._IDENTITY_KEYS)
            and all(type(handle.bundle["status_parent"][key]) is int
                    for key in FDS._IDENTITY_KEYS)
            and handle.owned == {
                "status_parent", "stdout_parent", "stderr_parent"}
            and handle.lost == set()
            and handle.attempted_writes == set()
            and tuple(handle.bundle["status_parent"][key]
                      for key in FDS._IDENTITY_KEYS)
                == operation.status_identity,
            "allocated status FD ownership changed")
    require(type(operation.read_attempted) is bool
            and operation.read_attempted is operation._read_latched
            and type(operation.read_returned) is bool
            and (operation.read_kind is None
                 or type(operation.read_kind) is str)
            and (operation.read_data is None
                 or type(operation.read_data) is bytes)
            and (operation.read_before_ns is None
                 or type(operation.read_before_ns) is int)
            and (operation.read_after_ns is None
                 or type(operation.read_after_ns) is int)
            and (operation._read_before_frozen is None
                 or type(operation._read_before_frozen) is int)
            and (operation._read_observation is None
                 or (type(operation._read_observation) is tuple
                     and len(operation._read_observation) == 2
                     and type(operation._read_observation[0]) is str
                     and type(operation._read_observation[1]) is bytes)),
            "strict allocated status effect evidence")
    if operation._read_before_frozen is not None:
        require(operation.read_before_ns
                == operation._read_before_frozen,
                "allocated status read-before evidence changed")
    if operation._read_observation is not None:
        require(operation.read_returned is True
                and operation.read_kind
                    == operation._read_observation[0]
                and operation.read_data
                    == operation._read_observation[1],
                "allocated status read observation changed")
    return True


class _ModeledStatusCalls:
    def __init__(self, key, operation, delegate):
        delegate_type = type(delegate)
        require(key is _STATUS_KEY
                and type(delegate_type) is type
                and delegate_type.__getattribute__ is object.__getattribute__
                and type(delegate_type.__dict__.get("__dict__"))
                    is types.GetSetDescriptorType,
                "strict allocated status backend namespace")
        namespace_descriptor = delegate_type.__dict__["__dict__"]
        namespace = namespace_descriptor.__get__(delegate, delegate_type)
        require(type(namespace) is dict
                and all(type(name) is str for name in namespace)
                and namespace.get(
                    "allocated_status_model_backend") is True
                and all(type(delegate_type.__dict__.get(name))
                        is types.FunctionType
                        for name in ("monotonic_ns", "read_status")),
                "explicit allocated status model backend required")
        self.operation = operation
        self.delegate = delegate
        self.delegate_type = delegate_type
        self.namespace_descriptor = namespace_descriptor
        self.clock_method = delegate_type.__dict__["monotonic_ns"]
        self.read_method = delegate_type.__dict__["read_status"]

    def _surface(self):
        delegate_type = self.delegate_type
        require(type(self.delegate) is delegate_type
                and type(delegate_type) is type
                and delegate_type.__getattribute__
                    is object.__getattribute__
                and delegate_type.__dict__.get("__dict__")
                    is self.namespace_descriptor
                and delegate_type.__dict__.get("monotonic_ns")
                    is self.clock_method
                and delegate_type.__dict__.get("read_status")
                    is self.read_method,
                "allocated status backend surface changed")
        namespace = self.namespace_descriptor.__get__(
            self.delegate, delegate_type)
        require(type(namespace) is dict
                and all(type(name) is str for name in namespace)
                and namespace.get("allocated_status_model_backend") is True,
                "allocated status backend namespace changed")

    def clock(self):
        self._surface()
        _validate_operation(self.operation)
        previous = self.operation.watermark.value
        value = self.clock_method(self.delegate)
        self._surface()
        _validate_operation(self.operation)
        require(type(value) is int
                and self.operation.watermark.value == previous
                and value >= previous
                and value < self.operation.deadline_ns,
                "strict allocated status clock")
        _WATERMARK_ADVANCE(
            self.operation.watermark, _WATERMARK_KEY, value,
            self.operation.deadline_ns)
        return value

    def read(self):
        self._surface()
        _validate_operation(self.operation)
        require(self.operation.read_attempted is False
                and self.operation.read_returned is False,
                "fresh allocated status read required")
        require(type(self.operation.read_before_ns) is int,
                "allocated status read clock absent")
        object.__setattr__(
            self.operation, "_read_before_frozen",
            self.operation.read_before_ns)
        object.__setattr__(self.operation, "_read_latched", True)
        self.operation.read_attempted = True
        value = self.read_method(
            self.delegate, self.operation.status_fd, 1)
        require(type(value) is dict
                and all(type(key) is str for key in value)
                and set(value) == {"kind", "data"}
                and type(value["kind"]) is str
                and value["kind"] in ("EOF", "EAGAIN", "DATA")
                and type(value["data"]) is bytes
                and ((value["kind"] == "DATA"
                      and len(value["data"]) == 1)
                     or (value["kind"] != "DATA"
                         and value["data"] == b"")),
                "strict allocated status read result")
        operation = self.operation
        operation.read_kind = value["kind"]
        operation.read_data = bytes(value["data"])
        object.__setattr__(
            operation, "_read_observation",
            (operation.read_kind, operation.read_data))
        operation.read_returned = True
        self._surface()
        _validate_operation(operation)


def _validate_allocated_status_eof(evidence, consumer=None):
    require(type(evidence) is AllocatedStatusEOFEvidence
            and type(evidence._sealed) is bool and evidence._sealed is True
            and type(evidence._consumed) is bool
            and ((consumer is None and evidence._consumed is False
                  and evidence._consumer is None)
                 or (consumer is not None and evidence._consumed is True
                     and evidence._consumer is consumer)),
            "exact allocated status EOF evidence required")
    operation = evidence._operation
    _validate_operation(operation,
                        "MODEL_ALLOCATED_STATUS_EOF_NONQUALIFIED")
    require(type(operation) is AllocatedStatusTailResult
            and operation.eof_evidence is evidence
            and operation.attempt.allocated_status_eof_evidence is evidence
            and operation.state == "MODEL_ALLOCATED_STATUS_EOF_NONQUALIFIED"
            and evidence._terminal_evidence is operation.terminal_evidence
            and evidence._attempt is operation.attempt
            and evidence._status_identity is operation.status_identity
            and type(evidence._before_ns) is int
            and type(evidence._after_ns) is int
            and type(evidence._deadline_ns) is int
            and evidence._before_ns == operation.read_before_ns
            and evidence._after_ns == operation.read_after_ns
            and evidence._deadline_ns == operation.deadline_ns
            and operation.initial_watermark_ns
                <= evidence._before_ns <= evidence._after_ns
                < evidence._deadline_ns
            and operation.read_attempted is True
            and operation.read_returned is True
            and operation.read_kind == "EOF"
            and operation.read_data == b"",
            "allocated status EOF evidence changed")
    return True


def validate_allocated_status_eof(evidence):
    return _validate_allocated_status_eof(evidence)


def _reserve_allocated_status_eof(evidence, consumer):
    require(consumer is not None,
            "allocated status EOF consumer required")
    _validate_allocated_status_eof(evidence)
    object.__setattr__(evidence, "_consumed", True)
    object.__setattr__(evidence, "_consumer", consumer)
    _validate_allocated_status_eof(evidence, consumer)
    return True


class AllocatedProtocolEvaluation:
    """One-shot pure protocol evaluation; capability exists only on MATCH."""
    def __init__(self, key, status_eof):
        require(key is _STATUS_KEY,
                "allocated protocol evaluation construction is private")
        self.status_eof = status_eof
        self.status_operation = status_eof._operation
        self.terminal_evidence = status_eof._terminal_evidence
        self.attempt = status_eof._attempt
        self.owner_thread = threading.current_thread()
        self.state = "VERIFYING"
        self.reasons = None
        self.capability = None
        self.error = None
        self._active = True
        self._poisoned = False
        self._fixed = True

    def __setattr__(self, name, value):
        if (getattr(self, "_fixed", False)
                and name in {"status_eof", "status_operation",
                             "terminal_evidence", "attempt", "owner_thread",
                             "_active", "_poisoned", "_fixed"}):
            raise Refusal("allocated protocol authority is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        if name in {"status_eof", "status_operation", "terminal_evidence",
                    "attempt", "owner_thread", "_active", "_poisoned",
                    "_fixed"}:
            raise Refusal("allocated protocol authority is immutable")
        object.__delattr__(self, name)

    def __copy__(self):
        raise Refusal("allocated protocol evaluation is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("allocated protocol evaluation is noncopyable")

    def _poison(self, reason):
        object.__setattr__(self, "_poisoned", True)
        object.__setattr__(self, "_active", False)
        object.__setattr__(self, "error", reason)
        object.__setattr__(self, "state", "UNKNOWN")

    def snapshot(self):
        return {"schema": 1, "classification": self.state,
                "reasons": (None if self.reasons is None
                            else list(self.reasons)),
                "status_eof_verified": True,
                "abort_protocol_verified": self.capability is not None,
                "descendants_qualified": False,
                "resources_closed": False,
                "exec_proven": False,
                "runtime_authorized": False,
                "storage_authorized": False,
                "postcondition_verified": False,
                "error": self.error}


class AllocatedAbortProtocolCapability:
    """Opaque model-only protocol MATCH; never release authority."""
    __slots__ = ("_evaluation", "_status_eof", "_terminal_evidence",
                 "_attempt", "_owner_thread", "_receipt_bytes", "_consumed",
                 "_consumer", "_sealed")

    def __init__(self, key, evaluation, receipt):
        require(key is _STATUS_KEY,
                "allocated protocol capability construction is private")
        GRAPH.CONSUMER._builtin_tree(
            receipt, "allocated protocol MATCH receipt")
        object.__setattr__(self, "_evaluation", evaluation)
        object.__setattr__(self, "_status_eof", evaluation.status_eof)
        object.__setattr__(self, "_terminal_evidence",
                           evaluation.terminal_evidence)
        object.__setattr__(self, "_attempt", evaluation.attempt)
        object.__setattr__(self, "_owner_thread", threading.current_thread())
        object.__setattr__(self, "_receipt_bytes", ABORT.SUP.canonical(receipt))
        object.__setattr__(self, "_consumed", False)
        object.__setattr__(self, "_consumer", None)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise Refusal("allocated protocol capability is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        raise Refusal("allocated protocol capability is immutable")

    def __copy__(self):
        raise Refusal("allocated protocol capability is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("allocated protocol capability is noncopyable")

    def snapshot(self):
        return _protocol_match_receipt()


def _protocol_match_receipt():
    return {"schema": 1,
            "classification":
                "MODEL_ALLOCATED_ABORT_PROTOCOL_MATCH_NONQUALIFIED",
            "status_eof_verified": True,
            "abort_protocol_verified": True,
            "descendants_qualified": False,
            "resources_closed": False,
            "exec_proven": False,
            "runtime_authorized": False,
            "storage_authorized": False,
            "postcondition_verified": False}


def _validate_protocol_evaluation(evaluation, expected_state="VERIFYING"):
    require(type(evaluation) is AllocatedProtocolEvaluation
            and type(expected_state) is str
            and threading.current_thread() is evaluation.owner_thread
            and type(evaluation.state) is str
            and evaluation.state == expected_state
            and type(evaluation._active) is bool
            and type(evaluation._poisoned) is bool
            and evaluation._poisoned is False
            and evaluation._active is (expected_state == "VERIFYING")
            and evaluation.error is None,
            "current allocated protocol evaluation required")
    status_eof = evaluation.status_eof
    _validate_allocated_status_eof(status_eof, evaluation)
    attempt = evaluation.attempt
    require(evaluation.status_operation is status_eof._operation
            and evaluation.terminal_evidence is status_eof._terminal_evidence
            and attempt is status_eof._attempt
            and attempt.allocated_protocol_started is True
            and attempt.allocated_protocol_evaluation is evaluation
            and attempt.allocated_protocol_capability is evaluation.capability
            and (evaluation.reasons is None
                 or (type(evaluation.reasons) is tuple
                     and all(type(reason) is str
                             for reason in evaluation.reasons)))
            and (evaluation.capability is None
                 or type(evaluation.capability)
                    is AllocatedAbortProtocolCapability),
            "allocated protocol evaluation backlinks changed")
    return True


def _strict_protocol_match(evaluation, expected_state="VERIFYING"):
    """Pure exact matcher. Returns a built-in tuple of mismatch reasons."""
    _validate_protocol_evaluation(evaluation, expected_state)
    attempt = evaluation.attempt
    cap = attempt.monitor_capability
    capture = attempt.capture_handle
    adapter = attempt.capture_adapter
    settlement_handle = attempt.settlement_handle
    require(type(settlement_handle) is CAPTURE.SettlementHandle
            and settlement_handle
                is evaluation.terminal_evidence._bridge.settlement
            and type(settlement_handle.__dict__) is dict
            and all(type(key) is str
                    for key in settlement_handle.__dict__)
            and set(settlement_handle.__dict__) == {
                "receipt", "child_adapter", "deadline_ns", "leader_reaped",
                "owner", "probe_used", "owner_thread", "capture_handle",
                "normal_completion_capability", "descendant_capability"},
            "exact allocated settlement handle required")
    descendant = settlement_handle.descendant_capability
    require(type(descendant) is CAPTURE._DescendantSettlementCapability
            and type(descendant.__dict__) is dict
            and all(type(key) is str for key in descendant.__dict__)
            and set(descendant.__dict__) == {
                "capture_handle", "child_adapter", "primary_failure",
                "primary_completion", "settlement_kind",
                "normal_completion_capability", "finished_ns", "deadline_ns",
                "owner", "owner_thread", "used"}
            and descendant.capture_handle is capture
            and descendant.child_adapter is adapter
            and type(descendant.primary_completion) is dict
            and all(type(key) is str
                    for key in descendant.primary_completion)
            and type(descendant.owner) is dict
            and all(type(key) is str for key in descendant.owner)
            and type(descendant.primary_failure) is type(None)
            and type(descendant.settlement_kind) is str
            and descendant.settlement_kind == "NORMAL_COMPLETION"
            and type(descendant.finished_ns) is int
            and type(descendant.deadline_ns) is int
            and descendant.owner_thread is attempt.owner_thread
            and type(descendant.used) is bool
            and descendant.used is True,
            "exact allocated descendant settlement required")
    GRAPH.CONSUMER._builtin_tree(
        descendant.primary_completion,
        "allocated descendant primary completion")
    GRAPH.CONSUMER._builtin_tree(
        descendant.owner, "allocated descendant owner")
    require(type(cap) is CAPTURE._NormalCompletionCapability
            and type(cap.__dict__) is dict
            and all(type(key) is str for key in cap.__dict__)
            and set(cap.__dict__) == {
                "capture_handle", "child_adapter", "cached_receipt",
                "completion", "completion_digest", "execution_deadline_ns",
                "completed_at_ns", "owner", "owner_thread", "used"}
            and cap is capture.normal_completion_capability
            and cap is adapter.normal_completion_capability
            and cap is settlement_handle.normal_completion_capability
            and cap is descendant.normal_completion_capability
            and cap.capture_handle is capture
            and cap.child_adapter is adapter
            and type(cap.cached_receipt) is OWNED.ExitReceipt
            and cap.cached_receipt is adapter.cached
            and cap.owner_thread is attempt.owner_thread
            and type(cap.completion_digest) is str
            and type(adapter.normal_completion_digest) is str
            and len(adapter.normal_completion_digest) == 64
            and all(character in "0123456789abcdef"
                    for character in adapter.normal_completion_digest)
            and adapter.normal_completion_digest == cap.completion_digest
            and type(cap.execution_deadline_ns) is int
            and type(cap.completed_at_ns) is int
            and type(cap.owner) is dict
            and type(cap.used) is bool and cap.used is True,
            "exact allocated normal completion capability required")
    policy = attempt._protocol_policy
    completion = cap.completion
    settlement = attempt.settlement_receipt
    require(type(policy) is dict and all(type(key) is str for key in policy)
            and set(policy) == {"schema", "exit_code", "stdout_hex",
                                "stderr_hex"}
            and type(policy["schema"]) is int and policy["schema"] == 1
            and type(policy["exit_code"]) is int
            and type(policy["stdout_hex"]) is str
            and type(policy["stderr_hex"]) is str
            and type(attempt._protocol_policy_bytes) is bytes
            and type(completion) is dict
            and all(type(key) is str for key in completion)
            and type(settlement) is dict
            and all(type(key) is str for key in settlement),
            "strict allocated protocol containers")
    GRAPH.CONSUMER._builtin_tree(
        cap.completion, "allocated normal completion")
    GRAPH.CONSUMER._builtin_tree(cap.owner, "allocated completion owner")
    GRAPH.CONSUMER._builtin_tree(
        settlement_handle.receipt, "allocated settlement handle receipt")
    GRAPH.CONSUMER._builtin_tree(
        settlement_handle.owner, "allocated settlement handle owner")
    persisted = attempt.persisted
    require(type(persisted) is GRAPH.PersistedNoGrantIntentCapability
            and type(persisted.__dict__) is dict
            and all(type(key) is str for key in persisted.__dict__)
            and set(persisted.__dict__) == {
                "bound", "enrollment", "handle", "ticket", "bridge",
                "receipt", "persisted_now_ns", "owner_thread", "state",
                "_receipt_bytes", "_sealed"},
            "exact allocated persisted authority required")
    bound = persisted.bound
    require(type(bound) is GRAPH.BoundNoGrantIntentCapability
            and type(bound.__dict__) is dict
            and all(type(key) is str for key in bound.__dict__)
            and set(bound.__dict__) == {
                "enrollment", "handle", "ticket", "request",
                "intent_bytes", "bind_now_ns", "owner_thread", "used",
                "state", "consumer", "persistence_started",
                "persistence_bridge", "persisted_capability",
                "_request_bytes", "_sealed"}
            and type(bound.intent_bytes) is bytes
            and type(bound._request_bytes) is bytes
            and persisted.enrollment is attempt.enrollment
            and persisted.handle is attempt.handle
            and persisted.ticket is attempt.ticket
            and bound.enrollment is attempt.enrollment
            and bound.handle is attempt.handle
            and bound.ticket is attempt.ticket
            and bound.persisted_capability is persisted
            and bound.consumer is attempt,
            "allocated persisted authority backlinks changed")
    origin_owner = capture.origin.owner
    owned_lifecycle = adapter.handle.lifecycle
    require(type(origin_owner) is dict
            and all(type(key) is str for key in origin_owner)
            and type(owned_lifecycle) is dict
            and all(type(key) is str for key in owned_lifecycle)
            and set(owned_lifecycle) == {
                "schema", "state", "token", "supervisor", "owned_child",
                "origin"}
            and type(owned_lifecycle["supervisor"]) is dict
            and all(type(key) is str
                    for key in owned_lifecycle["supervisor"])
            and type(owned_lifecycle["owned_child"]) is dict
            and all(type(key) is str
                    for key in owned_lifecycle["owned_child"])
            and set(owned_lifecycle["owned_child"])
                == {"pid", "starttime"}
            and type(owned_lifecycle["owned_child"]["pid"]) is int
            and type(owned_lifecycle["owned_child"]["starttime"]) is int,
            "strict allocated transferred identity containers")
    GRAPH.CONSUMER._builtin_tree(
        origin_owner, "allocated capture origin owner")
    GRAPH.CONSUMER._builtin_tree(
        owned_lifecycle["supervisor"],
        "allocated owned lifecycle supervisor")
    GRAPH.CONSUMER._builtin_tree(
        owned_lifecycle["owned_child"],
        "allocated owned lifecycle child")
    _VALIDATE_TRANSFERRED_NORMAL(settlement_handle, capture, adapter)
    GRAPH.CONSUMER._builtin_tree(policy, "allocated protocol policy")
    GRAPH.CONSUMER._builtin_tree(completion,
                                 "allocated monitor completion")
    GRAPH.CONSUMER._builtin_tree(settlement,
                                 "allocated settlement receipt")
    require(ABORT.SUP.canonical(policy) == attempt._protocol_policy_bytes
            and ABORT.SUP.canonical(completion)
                == attempt._monitor_result_bytes
            and ABORT.SUP.canonical(settlement)
                == attempt._settlement_receipt_bytes,
            "allocated protocol frozen evidence changed")
    require(policy["exit_code"] == 73,
            "allocated protocol exit policy changed")
    expected_data = {
        "stdout": bytes.fromhex(policy["stdout_hex"]),
        "stderr": bytes.fromhex(policy["stderr_hex"])}
    terminal = completion.get("leader_observation")
    reap = settlement.get("leader_reap")
    streams = completion.get("streams")
    require(type(terminal) is dict
            and all(type(key) is str for key in terminal)
            and type(reap) is dict
            and all(type(key) is str for key in reap)
            and type(streams) is dict
            and all(type(key) is str for key in streams)
            and set(streams) == {"stdout", "stderr"},
            "strict allocated protocol evidence")
    GRAPH.CONSUMER._builtin_tree(terminal,
                                 "allocated terminal observation")
    GRAPH.CONSUMER._builtin_tree(reap, "allocated leader reap")
    GRAPH.CONSUMER._builtin_tree(streams, "allocated stream receipts")
    lifecycle = attempt.lifecycle
    owned = attempt.owned_pidfd
    require(type(lifecycle) is dict
            and type(lifecycle.get("token")) is str
            and type(attempt.returned_pid) is int
            and type(owned.child) is dict
            and type(owned.child.get("starttime")) is int,
            "strict allocated protocol lifecycle")
    expected_terminal = {
        "classification": "EXITED_NONZERO",
        "lifecycle_token": lifecycle["token"],
        "child_pid": attempt.returned_pid,
        "child_starttime": owned.child["starttime"],
        "code": os.CLD_EXITED, "status": 73}
    expected_reap = {
        "classification": "OWNED_LEADER_REAPED_ONLY",
        "lifecycle_token": lifecycle["token"],
        "child_pid": attempt.returned_pid,
        "child_starttime": owned.child["starttime"],
        "exit": "EXITED_NONZERO", "status": 73,
        "runtime_authorized": False,
        "storage_authorized": False,
        "postcondition_verified": False,
        "descendants_qualified": False,
        "capture_qualified": False}
    reasons = []
    if terminal != expected_terminal:
        reasons.append("TERMINAL_NOT_EXIT_73")
    if reap != expected_reap:
        reasons.append("REAP_NOT_EXIT_73")
    allocation = dict(evaluation.terminal_evidence._allocation_authority)
    request = _STRICT_BOUND_REQUEST(bound)
    require(type(request) is dict
            and type(request.get("capture_limit")) is int,
            "strict allocated protocol capture policy")
    require(type(capture.streams) is dict
            and all(type(key) is str for key in capture.streams)
            and set(capture.streams) == {"stdout", "stderr"}
            and type(capture.origin.identities) is dict
            and all(type(key) is str for key in capture.origin.identities)
            and set(capture.origin.identities) == {"stdout", "stderr"},
            "strict allocated protocol stream graph")
    for role in ("stdout", "stderr"):
        stream = capture.streams[role]
        require(type(stream) is CAPTURE._Stream
                and type(stream.role) is str and stream.role == role
                and type(stream.identity) is dict
                and all(type(key) is str for key in stream.identity)
                and type(stream.limit) is int
                and stream.limit == request["capture_limit"]
                and type(stream.stored) is bytearray
                and type(stream.observed_bytes) is int
                and type(stream.eof) is bool
                and type(stream.truncated) is bool,
                "strict allocated protocol stream")
        GRAPH.CONSUMER._builtin_tree(
            stream.identity, "allocated protocol stream identity")
        current = _STRICT_STREAM_SNAPSHOT(stream, role)
        GRAPH.CONSUMER._builtin_tree(
            current, "allocated current stream receipt")
        expected = expected_data[role]
        parent = allocation[role + "_parent"]
        require(type(parent) is tuple
                and len(parent) == len(FDS._IDENTITY_KEYS)
                and all(type(value) is int for value in parent),
                "strict allocated protocol parent identity")
        parent_map = dict(zip(FDS._IDENTITY_KEYS, parent))
        identity = capture.origin.identities[role]
        require(type(identity) is dict
                and all(type(key) is str for key in identity),
                "strict allocated protocol origin identity")
        GRAPH.CONSUMER._builtin_tree(
            identity, "allocated protocol origin identity")
        expected_receipt = {
            "role": role,
            "pipe_identity": copy.deepcopy(identity),
            "eof": True, "truncated": False,
            "stored_bytes": len(expected),
            "observed_bytes": len(expected),
            "prefix_sha256": hashlib.sha256(expected).hexdigest(),
            "digest_scope": "FULL_CAPTURE"}
        identity_matches = (all(identity.get(key) == parent_map[key]
                                for key in ("fd", "dev", "inode", "mode",
                                            "flags"))
                            and stream.identity == identity)
        if not (bytes(stream.stored) == expected
                and identity_matches
                and current == expected_receipt
                and streams[role] == expected_receipt):
            reasons.append(role.upper() + "_CANARY_MISMATCH")
    _validate_protocol_evaluation(evaluation, expected_state)
    return tuple(reasons)


def _validate_allocated_protocol_capability(capability, consumer=None):
    require(type(capability) is AllocatedAbortProtocolCapability
            and type(capability._sealed) is bool
            and capability._sealed is True
            and threading.current_thread() is capability._owner_thread
            and type(capability._receipt_bytes) is bytes
            and type(capability._consumed) is bool
            and ((consumer is None and capability._consumed is False
                  and capability._consumer is None)
                 or (consumer is not None and capability._consumed is True
                     and capability._consumer is consumer)),
            "exact allocated protocol capability required")
    evaluation = capability._evaluation
    _validate_protocol_evaluation(
        evaluation, "MODEL_ALLOCATED_ABORT_PROTOCOL_MATCH_NONQUALIFIED")
    require(evaluation.capability is capability
            and capability._status_eof is evaluation.status_eof
            and capability._terminal_evidence is evaluation.terminal_evidence
            and capability._attempt is evaluation.attempt
            and evaluation.reasons == (),
            "allocated protocol capability backlinks changed")
    require(_strict_protocol_match(
                evaluation,
                "MODEL_ALLOCATED_ABORT_PROTOCOL_MATCH_NONQUALIFIED") == (),
            "allocated protocol MATCH no longer current")
    receipt = _protocol_match_receipt()
    GRAPH.CONSUMER._builtin_tree(
        receipt, "allocated protocol capability snapshot")
    require(ABORT.SUP.canonical(receipt) == capability._receipt_bytes,
            "allocated protocol capability receipt changed")
    return True


def validate_allocated_protocol_capability(capability):
    return _validate_allocated_protocol_capability(capability)


def _reserve_allocated_protocol_capability(capability, consumer):
    require(consumer is not None,
            "private allocated protocol reservation")
    _validate_allocated_protocol_capability(capability)
    object.__setattr__(capability, "_consumed", True)
    object.__setattr__(capability, "_consumer", consumer)
    _validate_allocated_protocol_capability(capability, consumer)
    return True


def match_allocated_abort_protocol_once(status_eof):
    """Pure model evidence matcher; performs no clock, read or release."""
    _validate_allocated_status_eof(status_eof)
    attempt = status_eof._attempt
    evaluation = None
    try:
        require(attempt.allocated_protocol_started is False
                and attempt.allocated_protocol_evaluation is None
                and attempt.allocated_protocol_capability is None,
                "fresh allocated protocol evaluation required")
        evaluation = AllocatedProtocolEvaluation(_STATUS_KEY, status_eof)
        _ATTEMPT_SET(attempt, "allocated_protocol_started", True)
        _ATTEMPT_SET(attempt, "allocated_protocol_evaluation", evaluation)
        _reserve_allocated_status_eof(status_eof, evaluation)
        _validate_protocol_evaluation(evaluation)
        reasons = _strict_protocol_match(evaluation)
        evaluation.reasons = reasons
        if reasons:
            evaluation.state = "MODEL_ALLOCATED_ABORT_PROTOCOL_MISMATCH"
            object.__setattr__(evaluation, "_active", False)
            return evaluation
        receipt = _protocol_match_receipt()
        capability = AllocatedAbortProtocolCapability(
            _STATUS_KEY, evaluation, receipt)
        evaluation.capability = capability
        _ATTEMPT_SET(attempt, "allocated_protocol_capability", capability)
        evaluation.state = receipt["classification"]
        object.__setattr__(evaluation, "_active", False)
        validate_allocated_protocol_capability(capability)
        return evaluation
    except BaseException as exc:
        if type(evaluation) is AllocatedProtocolEvaluation:
            evaluation._poison(type(exc).__name__)
        if type(attempt) is ABORT.AbortForkAttempt:
            attempt._poison("ALLOCATED_PROTOCOL_MATCH")
        raise


def observe_allocated_status_tail_once(terminal_evidence, modeled_calls):
    """Model exactly one status read; no poll, retry, live read or release."""
    if (type(terminal_evidence) is DBRIDGE.AllocatedTerminalDomainEvidence
            and terminal_evidence._consumed is True
            and type(terminal_evidence._consumer)
                is AllocatedStatusTailResult):
        active = terminal_evidence._consumer
        if type(active._active) is not bool or active._active is True:
            active._poison("REENTRANT_ALLOCATED_STATUS_OBSERVATION")
            active.attempt._poison("ALLOCATED_STATUS_REENTRY")
            raise Refusal("allocated status observation reentry")
    _VALIDATE_TERMINAL(terminal_evidence)
    attempt = terminal_evidence._attempt
    operation = None
    try:
        require(attempt.allocated_status_started is False
                and attempt.allocated_status_result is None
                and attempt.allocated_status_eof_evidence is None,
                "fresh allocated status observation required")
        operation = AllocatedStatusTailResult(_STATUS_KEY, terminal_evidence)
        _ATTEMPT_SET(attempt, "allocated_status_started", True)
        _ATTEMPT_SET(attempt, "allocated_status_result", operation)
        _RESERVE_TERMINAL(terminal_evidence, operation)
        _validate_operation(operation)
        calls = _ModeledStatusCalls(_STATUS_KEY, operation, modeled_calls)
        before = calls.clock()
        operation.read_before_ns = before
        calls.read()
        after = calls.clock()
        operation.read_after_ns = after
        _validate_operation(operation)
        observed_kind = operation._read_observation[0]
        if observed_kind == "EOF":
            evidence = AllocatedStatusEOFEvidence(_STATUS_KEY, operation)
            operation.eof_evidence = evidence
            _ATTEMPT_SET(
                attempt, "allocated_status_eof_evidence", evidence)
            operation.state = "MODEL_ALLOCATED_STATUS_EOF_NONQUALIFIED"
            object.__setattr__(operation, "_active", False)
            validate_allocated_status_eof(evidence)
        elif observed_kind == "DATA":
            operation.state = "MODEL_ALLOCATED_STATUS_DATA_MISMATCH"
            object.__setattr__(operation, "_active", False)
        else:
            operation.state = "MODEL_ALLOCATED_STATUS_EAGAIN_NONQUALIFIED"
            object.__setattr__(operation, "_active", False)
        return operation
    except BaseException as exc:
        if type(operation) is AllocatedStatusTailResult:
            operation._poison(type(exc).__name__)
        if type(attempt) is ABORT.AbortForkAttempt:
            attempt._poison("ALLOCATED_STATUS_OBSERVATION")
        raise
