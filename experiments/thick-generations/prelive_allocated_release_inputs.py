#!/usr/bin/python3
"""One-shot source-model qualification of allocated release inputs."""

import threading

import prelive_abort_fork_split as ABORT
import prelive_allocated_abort_protocol as APROTO
import prelive_allocated_graph_enrollment as GRAPH
import prelive_descendant_bridge as DBRIDGE


_KEY = object()
_ATTEMPT_SET = ABORT.AbortForkAttempt.__dict__["_set"]
_VALIDATE_PROTOCOL = APROTO._validate_allocated_protocol_capability
_RESERVE_PROTOCOL = APROTO._reserve_allocated_protocol_capability
_VALIDATE_STATUS = APROTO._validate_allocated_status_eof
_VALIDATE_TERMINAL = DBRIDGE._validate_allocated_terminal_evidence


class Refusal(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise Refusal(message)


def _poison_attempt(attempt, phase):
    """Pinned direct fail-closed transition; never dispatch attempt methods."""
    require(type(attempt) is ABORT.AbortForkAttempt
            and type(phase) is str,
            "exact allocated release-input poison target")
    survivor = (attempt.fork_attempted
                if type(attempt.fork_attempted) is bool else True)
    _ATTEMPT_SET(attempt, "poisoned", True)
    _ATTEMPT_SET(attempt, "failure_phase", phase)
    _ATTEMPT_SET(
        attempt, "state", "UNKNOWN_SURVIVOR" if survivor else "UNKNOWN")


def _receipt():
    return {
        "schema": 1,
        "classification": "MODEL_ALLOCATED_RELEASE_INPUTS_QUALIFIED_ONLY",
        "model_evidence_complete": True,
        "status_eof_verified": True,
        "abort_protocol_verified": True,
        "current_deadline_checked": False,
        "live_release_authorized": False,
        "descendants_qualified": False,
        "resources_closed": False,
        "exec_proven": False,
        "runtime_authorized": False,
        "storage_authorized": False,
        "postcondition_verified": False}


class AllocatedReleaseInputsOperation:
    __slots__ = ("protocol", "evaluation", "status_eof", "status_operation",
                 "terminal", "attempt", "owner_thread", "state", "active",
                 "poisoned", "error", "capability", "_sealed")

    def __init__(self, key, protocol):
        require(key is _KEY, "private allocated release-input operation")
        evaluation = protocol._evaluation
        status_eof = protocol._status_eof
        terminal = protocol._terminal_evidence
        object.__setattr__(self, "protocol", protocol)
        object.__setattr__(self, "evaluation", evaluation)
        object.__setattr__(self, "status_eof", status_eof)
        object.__setattr__(self, "status_operation", status_eof._operation)
        object.__setattr__(self, "terminal", terminal)
        object.__setattr__(self, "attempt", protocol._attempt)
        object.__setattr__(self, "owner_thread", threading.current_thread())
        object.__setattr__(self, "state", "QUALIFYING")
        object.__setattr__(self, "active", True)
        object.__setattr__(self, "poisoned", False)
        object.__setattr__(self, "error", None)
        object.__setattr__(self, "capability", None)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise Refusal("allocated release-input operation is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        raise Refusal("allocated release-input operation is immutable")

    def _poison(self, reason):
        object.__setattr__(self, "poisoned", True)
        object.__setattr__(self, "active", False)
        object.__setattr__(self, "error", reason)
        object.__setattr__(self, "state", "UNKNOWN")

    def __copy__(self):
        raise Refusal("allocated release-input operation is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("allocated release-input operation is noncopyable")


class AllocatedReleaseInputsCapability:
    __slots__ = ("_operation", "_protocol", "_evaluation", "_status_eof",
                 "_terminal", "_attempt", "_owner_thread", "_receipt_bytes",
                 "_consumed", "_consumer", "_sealed")

    def __init__(self, key, operation):
        require(key is _KEY, "private allocated release-input capability")
        receipt = _receipt()
        GRAPH.CONSUMER._builtin_tree(
            receipt, "allocated release-input receipt")
        object.__setattr__(self, "_operation", operation)
        object.__setattr__(self, "_protocol", operation.protocol)
        object.__setattr__(self, "_evaluation", operation.evaluation)
        object.__setattr__(self, "_status_eof", operation.status_eof)
        object.__setattr__(self, "_terminal", operation.terminal)
        object.__setattr__(self, "_attempt", operation.attempt)
        object.__setattr__(self, "_owner_thread", threading.current_thread())
        object.__setattr__(self, "_receipt_bytes", ABORT.SUP.canonical(receipt))
        object.__setattr__(self, "_consumed", False)
        object.__setattr__(self, "_consumer", None)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise Refusal("allocated release-input capability is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        raise Refusal("allocated release-input capability is immutable")

    def __copy__(self):
        raise Refusal("allocated release-input capability is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("allocated release-input capability is noncopyable")

    def snapshot(self):
        return _receipt()


_POISON_OPERATION = AllocatedReleaseInputsOperation.__dict__["_poison"]


def _validate_operation(operation, expected_state, capability=None):
    require(type(operation) is AllocatedReleaseInputsOperation
            and type(expected_state) is str
            and threading.current_thread() is operation.owner_thread
            and type(operation.state) is str
            and operation.state == expected_state
            and type(operation.active) is bool
            and operation.active is (expected_state == "QUALIFYING")
            and type(operation.poisoned) is bool
            and operation.poisoned is False
            and operation.error is None
            and operation.capability is capability,
            "current allocated release-input operation required")
    protocol = operation.protocol
    evaluation = operation.evaluation
    status_eof = operation.status_eof
    status_operation = operation.status_operation
    terminal = operation.terminal
    attempt = operation.attempt
    _VALIDATE_PROTOCOL(protocol, operation)
    _VALIDATE_STATUS(status_eof, evaluation)
    _VALIDATE_TERMINAL(terminal, status_operation)
    require(protocol._evaluation is evaluation
            and protocol._status_eof is status_eof
            and protocol._terminal_evidence is terminal
            and protocol._attempt is attempt
            and evaluation.status_eof is status_eof
            and evaluation.terminal_evidence is terminal
            and evaluation.attempt is attempt
            and status_eof._operation is status_operation
            and status_eof._terminal_evidence is terminal
            and status_eof._attempt is attempt
            and terminal._attempt is attempt
            and terminal._consumed is True
            and terminal._consumer is status_operation
            and status_eof._consumed is True
            and status_eof._consumer is evaluation
            and protocol._consumed is True
            and protocol._consumer is operation
            and attempt.poisoned is False
            and attempt.allocated_release_inputs_started is True
            and attempt.allocated_release_inputs_operation is operation
            and attempt.allocated_release_inputs_capability is capability
            and attempt.terminal_release_claim_attempted is False
            and attempt.terminal_release_claim is None,
            "allocated release-input ownership chain changed")
    return True


def _validate_allocated_release_inputs_capability(capability, consumer=None):
    require(type(capability) is AllocatedReleaseInputsCapability
            and type(capability._sealed) is bool
            and capability._sealed is True
            and threading.current_thread() is capability._owner_thread
            and type(capability._receipt_bytes) is bytes
            and type(capability._consumed) is bool
            and ((consumer is None and capability._consumed is False
                  and capability._consumer is None)
                 or (consumer is not None and capability._consumed is True
                     and capability._consumer is consumer)),
            "exact allocated release-input capability required")
    operation = capability._operation
    require(type(operation) is AllocatedReleaseInputsOperation,
            "exact allocated release-input operation required")
    require(capability._protocol is operation.protocol
            and capability._evaluation is operation.evaluation
            and capability._status_eof is operation.status_eof
            and capability._terminal is operation.terminal
            and capability._attempt is operation.attempt,
            "allocated release-input capability backlinks changed")
    _validate_operation(
        operation, "MODEL_ALLOCATED_RELEASE_INPUTS_QUALIFIED_ONLY",
        capability)
    receipt = _receipt()
    GRAPH.CONSUMER._builtin_tree(
        receipt, "allocated release-input capability receipt")
    require(ABORT.SUP.canonical(receipt) == capability._receipt_bytes,
            "allocated release-input receipt changed")
    return True


def validate_allocated_release_inputs_capability(capability):
    return _validate_allocated_release_inputs_capability(capability)


def _reserve_allocated_release_inputs_capability(capability, consumer):
    require(consumer is not None,
            "private allocated release-input reservation")
    _validate_allocated_release_inputs_capability(capability)
    object.__setattr__(capability, "_consumed", True)
    object.__setattr__(capability, "_consumer", consumer)
    _validate_allocated_release_inputs_capability(capability, consumer)
    return True


def qualify_allocated_release_inputs_once(protocol):
    """Consume one exact MATCH leaf; no clock, syscall, close or release."""
    if (type(protocol) is APROTO.AllocatedAbortProtocolCapability
            and type(protocol._consumed) is bool
            and protocol._consumed is True
            and type(protocol._consumer)
                is AllocatedReleaseInputsOperation):
        active = protocol._consumer
        if (type(active.active) is not bool or active.active is True
                or type(active.state) is not str
                or active.state == "QUALIFYING"):
            _POISON_OPERATION(active, "REENTRANT_ALLOCATED_RELEASE_INPUTS")
            if type(active.attempt) is ABORT.AbortForkAttempt:
                _poison_attempt(
                    active.attempt, "ALLOCATED_RELEASE_INPUTS_REENTRY")
            raise Refusal("allocated release-input qualification reentry")
    _VALIDATE_PROTOCOL(protocol)
    attempt = protocol._attempt
    operation = None
    try:
        require(attempt.poisoned is False
                and attempt.allocated_release_inputs_started is False
                and attempt.allocated_release_inputs_operation is None
                and attempt.allocated_release_inputs_capability is None
                and attempt.terminal_release_claim_attempted is False
                and attempt.terminal_release_claim is None,
                "fresh allocated release-input qualification required")
        operation = AllocatedReleaseInputsOperation(_KEY, protocol)
        _ATTEMPT_SET(attempt, "allocated_release_inputs_started", True)
        _ATTEMPT_SET(attempt, "allocated_release_inputs_operation", operation)
        _RESERVE_PROTOCOL(protocol, operation)
        _validate_operation(operation, "QUALIFYING")
        capability = AllocatedReleaseInputsCapability(_KEY, operation)
        _ATTEMPT_SET(
            attempt, "allocated_release_inputs_capability", capability)
        require(type(operation.state) is str
                and operation.state == "QUALIFYING"
                and type(operation.active) is bool
                and operation.active is True
                and type(operation.poisoned) is bool
                and operation.poisoned is False
                and operation.error is None
                and operation.capability is None,
                "allocated release-input commit changed")
        object.__setattr__(operation, "capability", capability)
        object.__setattr__(
            operation, "state",
            "MODEL_ALLOCATED_RELEASE_INPUTS_QUALIFIED_ONLY")
        object.__setattr__(operation, "active", False)
        validate_allocated_release_inputs_capability(capability)
        return capability
    except BaseException as exc:
        if type(operation) is AllocatedReleaseInputsOperation:
            _POISON_OPERATION(operation, type(exc).__name__)
        if type(attempt) is ABORT.AbortForkAttempt:
            _poison_attempt(attempt, "ALLOCATED_RELEASE_INPUTS")
        raise
