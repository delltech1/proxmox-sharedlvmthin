#!/usr/bin/python3
"""One-shot modeled deadline observation; never a live release grant."""

import threading
import types

import prelive_abort_fork_split as ABORT
import prelive_allocated_abort_protocol as APROTO
import prelive_allocated_graph_enrollment as GRAPH
import prelive_allocated_release_inputs as INPUTS
import prelive_capture_settlement as CAPTURE
import prelive_descendant_bridge as DBRIDGE


_KEY = object()
_ATTEMPT_SET = ABORT.AbortForkAttempt.__dict__["_set"]
_VALIDATE_INPUTS = INPUTS._validate_allocated_release_inputs_capability
_RESERVE_INPUTS = INPUTS._reserve_allocated_release_inputs_capability


class Refusal(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise Refusal(message)


def _poison_attempt(attempt, phase):
    require(type(attempt) is ABORT.AbortForkAttempt
            and type(phase) is str,
            "exact allocated deadline poison target")
    survivor = (attempt.fork_attempted
                if type(attempt.fork_attempted) is bool else True)
    _ATTEMPT_SET(attempt, "poisoned", True)
    _ATTEMPT_SET(attempt, "failure_phase", phase)
    _ATTEMPT_SET(
        attempt, "state", "UNKNOWN_SURVIVOR" if survivor else "UNKNOWN")


def _receipt(observed, lower, deadline):
    require(type(observed) is int and type(lower) is int
            and type(deadline) is int,
            "strict allocated deadline receipt times")
    return {
        "schema": 1,
        "classification": "MODEL_ALLOCATED_RELEASE_DEADLINE_ADMITTED_ONLY",
        "observed_ns": observed,
        "lower_bound_ns": lower,
        "deadline_ns": deadline,
        "model_deadline_observation_verified": True,
        "live_clock_verified": False,
        "live_release_authorized": False,
        "descendants_qualified": False,
        "resources_closed": False,
        "runtime_authorized": False,
        "storage_authorized": False,
        "postcondition_verified": False}


def _validator_surface(_modules=(
        (INPUTS, INPUTS.__name__), (APROTO, APROTO.__name__),
        (DBRIDGE, DBRIDGE.__name__), (CAPTURE, CAPTURE.__name__),
        (GRAPH, GRAPH.__name__),
        (GRAPH.CONSUMER, GRAPH.CONSUMER.__name__),
        (ABORT.SUP, ABORT.SUP.__name__))):
    """Freeze every Python function in the scoped validation modules."""
    result = []
    queue = list(_modules)
    visited = set()
    index = 0
    while index < len(queue):
        module_entry = queue[index]
        index += 1
        require(type(module_entry) is tuple and len(module_entry) == 2
                and type(module_entry[0]) is types.ModuleType
                and type(module_entry[1]) is str,
                "strict allocated deadline validation module identity")
        module, frozen_name = module_entry
        if id(module) in visited:
            continue
        visited.add(id(module))
        require(type(module) is types.ModuleType
                and type(module.__dict__) is dict,
                "strict allocated deadline validation module")
        namespace = module.__dict__
        require(all(type(name) is str for name in namespace),
                "strict allocated deadline module namespace")
        current_name = namespace.get("__name__")
        require(type(current_name) is str
                and current_name == frozen_name,
                "allocated deadline validation module name changed")
        functions = tuple(
            (name, namespace[name])
            for name in sorted(namespace)
            if type(namespace[name]) is types.FunctionType)
        classes = []
        for name in sorted(namespace):
            value = namespace[name]
            if (type(value) is type
                    and type(value.__module__) is str
                    and value.__module__ == frozen_name):
                class_namespace = value.__dict__
                require(type(class_namespace) is types.MappingProxyType
                        and all(type(key) is str
                                for key in class_namespace),
                        "strict allocated deadline class namespace")
                entries = tuple(
                    (key, class_namespace[key])
                    for key in sorted(class_namespace))
                classes.append((name, value, entries))
        aliases = []
        for name in sorted(namespace):
            value = namespace[name]
            if (type(value) is types.ModuleType
                    and type(value.__dict__) is dict):
                alias_namespace = value.__dict__
                require(all(type(key) is str for key in alias_namespace),
                        "strict allocated deadline alias namespace")
                alias_name = alias_namespace.get("__name__")
                require(type(alias_name) is str,
                        "strict allocated deadline module alias name")
                if alias_name.startswith("prelive_"):
                    aliases.append((name, value, alias_name))
                    queue.append((value, alias_name))
        result.append((frozen_name, functions, tuple(classes),
                       tuple(aliases)))
    return tuple(result)


def _validate_surface(frozen, _get_surface=_validator_surface):
    current = _get_surface()
    require(type(frozen) is tuple
            and type(current) is tuple
            and len(current) == len(frozen),
            "allocated deadline validator surface changed")
    for index in range(len(frozen)):
        frozen_module = frozen[index]
        current_module = current[index]
        require(type(frozen_module) is tuple and len(frozen_module) == 4
                and type(current_module) is tuple
                and len(current_module) == 4
                and type(frozen_module[0]) is str
                and type(current_module[0]) is str
                and current_module[0] == frozen_module[0]
                and type(frozen_module[1]) is tuple
                and type(current_module[1]) is tuple
                and len(current_module[1]) == len(frozen_module[1])
                and type(frozen_module[2]) is tuple
                and type(current_module[2]) is tuple
                and len(current_module[2]) == len(frozen_module[2])
                and type(frozen_module[3]) is tuple
                and type(current_module[3]) is tuple
                and len(current_module[3]) == len(frozen_module[3]),
                "allocated deadline validation module changed")
        for function_index in range(len(frozen_module[1])):
            frozen_entry = frozen_module[1][function_index]
            current_entry = current_module[1][function_index]
            require(type(frozen_entry) is tuple and len(frozen_entry) == 2
                    and type(current_entry) is tuple
                    and len(current_entry) == 2
                    and type(frozen_entry[0]) is str
                    and type(current_entry[0]) is str
                    and current_entry[0] == frozen_entry[0]
                    and type(frozen_entry[1]) is types.FunctionType
                    and type(current_entry[1]) is types.FunctionType
                    and current_entry[1] is frozen_entry[1],
                    "allocated deadline validation function changed")
        for class_index in range(len(frozen_module[2])):
            frozen_class = frozen_module[2][class_index]
            current_class = current_module[2][class_index]
            require(type(frozen_class) is tuple and len(frozen_class) == 3
                    and type(current_class) is tuple
                    and len(current_class) == 3
                    and type(frozen_class[0]) is str
                    and type(current_class[0]) is str
                    and current_class[0] == frozen_class[0]
                    and type(frozen_class[1]) is type
                    and type(current_class[1]) is type
                    and current_class[1] is frozen_class[1]
                    and type(frozen_class[2]) is tuple
                    and type(current_class[2]) is tuple
                    and len(current_class[2]) == len(frozen_class[2]),
                    "allocated deadline validation class changed")
            for entry_index in range(len(frozen_class[2])):
                frozen_entry = frozen_class[2][entry_index]
                current_entry = current_class[2][entry_index]
                require(type(frozen_entry) is tuple
                        and len(frozen_entry) == 2
                        and type(current_entry) is tuple
                        and len(current_entry) == 2
                        and type(frozen_entry[0]) is str
                        and type(current_entry[0]) is str
                        and current_entry[0] == frozen_entry[0]
                        and current_entry[1] is frozen_entry[1],
                        "allocated deadline class attribute changed")
        for alias_index in range(len(frozen_module[3])):
            frozen_alias = frozen_module[3][alias_index]
            current_alias = current_module[3][alias_index]
            require(type(frozen_alias) is tuple and len(frozen_alias) == 3
                    and type(current_alias) is tuple
                    and len(current_alias) == 3
                    and type(frozen_alias[0]) is str
                    and type(current_alias[0]) is str
                    and current_alias[0] == frozen_alias[0]
                    and type(frozen_alias[1]) is types.ModuleType
                    and type(current_alias[1]) is types.ModuleType
                    and current_alias[1] is frozen_alias[1]
                    and type(frozen_alias[2]) is str
                    and type(current_alias[2]) is str
                    and current_alias[2] == frozen_alias[2],
                    "allocated deadline module alias changed")
    return True


_BASE_VALIDATOR_SURFACE = _validator_surface()


class AllocatedReleaseDeadlineOperation:
    __slots__ = ("inputs", "attempt", "owner_thread", "state", "active",
                 "poisoned", "error", "clock_attempted", "clock_returned",
                 "observed_ns", "deadline_ns", "lower_bound_ns",
                 "time_preimage", "resource_preimage", "validator_surface",
                 "backend", "capability", "_sealed")

    def __init__(self, key, inputs):
        require(key is _KEY, "private allocated deadline operation")
        attempt = inputs._attempt
        terminal = inputs._terminal
        status = inputs._status_eof
        status_operation = status._operation
        deadline = terminal._deadline_ns
        values = (terminal._watermark_ns, status._before_ns,
                  status._after_ns, status_operation.watermark.value,
                  attempt.last_clock_ns)
        require(type(deadline) is int
                and all(type(value) is int for value in values),
                "strict allocated deadline preimage")
        lower = max(values)
        handle = attempt.handle
        require(type(handle.owned) is set and type(handle.lost) is set
                and all(type(role) is str for role in handle.owned)
                and all(type(role) is str for role in handle.lost),
                "strict allocated deadline resource sets")
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "attempt", attempt)
        object.__setattr__(self, "owner_thread", threading.current_thread())
        object.__setattr__(self, "state", "ADMITTING")
        object.__setattr__(self, "active", True)
        object.__setattr__(self, "poisoned", False)
        object.__setattr__(self, "error", None)
        object.__setattr__(self, "clock_attempted", False)
        object.__setattr__(self, "clock_returned", False)
        object.__setattr__(self, "observed_ns", None)
        object.__setattr__(self, "deadline_ns", deadline)
        object.__setattr__(self, "lower_bound_ns", lower)
        object.__setattr__(self, "time_preimage", values)
        object.__setattr__(self, "resource_preimage", (
            terminal._allocation_authority, terminal._owned_pidfd,
            tuple(sorted(handle.owned)), tuple(sorted(handle.lost))))
        object.__setattr__(self, "validator_surface", _validator_surface())
        object.__setattr__(self, "backend", None)
        object.__setattr__(self, "capability", None)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise Refusal("allocated deadline operation is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        raise Refusal("allocated deadline operation is immutable")

    def __copy__(self):
        raise Refusal("allocated deadline operation is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("allocated deadline operation is noncopyable")

    def _poison(self, reason):
        object.__setattr__(self, "poisoned", True)
        object.__setattr__(self, "active", False)
        object.__setattr__(self, "error", reason)
        object.__setattr__(self, "state", "UNKNOWN")


_POISON_OPERATION = AllocatedReleaseDeadlineOperation.__dict__["_poison"]


class _ModeledDeadlineClock:
    __slots__ = ("operation", "delegate", "delegate_type",
                 "namespace_descriptor", "clock_method", "_sealed")

    def __init__(self, key, operation, delegate):
        delegate_type = type(delegate)
        require(key is _KEY
                and type(delegate_type) is type
                and delegate_type.__getattribute__ is object.__getattribute__
                and type(delegate_type.__dict__.get("__dict__"))
                    is types.GetSetDescriptorType,
                "strict allocated deadline backend namespace")
        descriptor = delegate_type.__dict__["__dict__"]
        namespace = descriptor.__get__(delegate, delegate_type)
        require(type(namespace) is dict
                and all(type(name) is str for name in namespace)
                and namespace.get("allocated_release_deadline_model_backend")
                    is True
                and type(delegate_type.__dict__.get("monotonic_ns"))
                    is types.FunctionType,
                "explicit allocated deadline model backend required")
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "delegate", delegate)
        object.__setattr__(self, "delegate_type", delegate_type)
        object.__setattr__(self, "namespace_descriptor", descriptor)
        object.__setattr__(self, "clock_method",
                           delegate_type.__dict__["monotonic_ns"])
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise Refusal("allocated deadline backend is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        raise Refusal("allocated deadline backend is immutable")

    def surface(self):
        delegate_type = self.delegate_type
        require(type(self.delegate) is delegate_type
                and delegate_type.__getattribute__ is object.__getattribute__
                and delegate_type.__dict__.get("__dict__")
                    is self.namespace_descriptor
                and delegate_type.__dict__.get("monotonic_ns")
                    is self.clock_method,
                "allocated deadline backend surface changed")
        namespace = self.namespace_descriptor.__get__(
            self.delegate, delegate_type)
        require(type(namespace) is dict
                and all(type(name) is str for name in namespace)
                and namespace.get("allocated_release_deadline_model_backend")
                    is True,
                "allocated deadline backend namespace changed")

    def clock_once(self):
        surface_guard = _CLOCK_SURFACE
        surface_validator = _validate_surface
        operation_validator = _validate_operation
        surface_guard(self)
        operation_validator(self.operation, "ADMITTING")
        require(self.operation.clock_attempted is False
                and self.operation.clock_returned is False,
                "fresh allocated deadline clock required")
        object.__setattr__(self.operation, "clock_attempted", True)
        value = self.clock_method(self.delegate)
        object.__setattr__(self.operation, "clock_returned", True)
        require(type(value) is int,
                "strict allocated deadline clock result")
        object.__setattr__(self.operation, "observed_ns", value)
        surface_guard(self)
        surface_validator(self.operation.validator_surface)
        operation_validator(self.operation, "ADMITTING")
        return value


_CLOCK_SURFACE = _ModeledDeadlineClock.__dict__["surface"]
_CLOCK_ONCE = _ModeledDeadlineClock.__dict__["clock_once"]


class AllocatedReleaseDeadlineCapability:
    __slots__ = ("_operation", "_inputs", "_attempt", "_owner_thread",
                 "_receipt_bytes", "_reservation_started", "_consumed",
                 "_consumer", "_sealed")

    def __init__(self, key, operation):
        require(key is _KEY, "private allocated deadline capability")
        receipt = _receipt(operation.observed_ns, operation.lower_bound_ns,
                           operation.deadline_ns)
        GRAPH.CONSUMER._builtin_tree(receipt, "allocated deadline receipt")
        object.__setattr__(self, "_operation", operation)
        object.__setattr__(self, "_inputs", operation.inputs)
        object.__setattr__(self, "_attempt", operation.attempt)
        object.__setattr__(self, "_owner_thread", threading.current_thread())
        object.__setattr__(self, "_receipt_bytes", ABORT.SUP.canonical(receipt))
        object.__setattr__(self, "_reservation_started", False)
        object.__setattr__(self, "_consumed", False)
        object.__setattr__(self, "_consumer", None)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise Refusal("allocated deadline capability is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        raise Refusal("allocated deadline capability is immutable")

    def __copy__(self):
        raise Refusal("allocated deadline capability is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("allocated deadline capability is noncopyable")

    def snapshot(self):
        return _receipt(self._operation.observed_ns,
                        self._operation.lower_bound_ns,
                        self._operation.deadline_ns)


def _current_preimages(operation):
    inputs = operation.inputs
    attempt = operation.attempt
    terminal = inputs._terminal
    status = inputs._status_eof
    status_operation = status._operation
    times = (terminal._watermark_ns, status._before_ns, status._after_ns,
             status_operation.watermark.value, attempt.last_clock_ns)
    handle = attempt.handle
    require(all(type(value) is int for value in times)
            and type(handle.owned) is set
            and type(handle.lost) is set
            and all(type(role) is str for role in handle.owned)
            and all(type(role) is str for role in handle.lost),
            "strict allocated deadline current preimage")
    resources = (terminal._allocation_authority, terminal._owned_pidfd,
                 tuple(sorted(handle.owned)), tuple(sorted(handle.lost)))
    return times, resources


def _validate_operation(operation, expected_state, capability=None,
                        _surface_validator=_validate_surface,
                        _inputs_validator=_VALIDATE_INPUTS,
                        _preimage_reader=_current_preimages):
    require(type(operation) is AllocatedReleaseDeadlineOperation
            and type(expected_state) is str
            and threading.current_thread() is operation.owner_thread
            and type(operation.state) is str
            and operation.state == expected_state
            and type(operation.active) is bool
            and operation.active is (expected_state == "ADMITTING")
            and type(operation.poisoned) is bool
            and operation.poisoned is False
            and operation.error is None
            and operation.capability is capability
            and type(operation.clock_attempted) is bool
            and type(operation.clock_returned) is bool
            and (operation.observed_ns is None
                 or type(operation.observed_ns) is int)
            and type(operation.deadline_ns) is int
            and type(operation.lower_bound_ns) is int
            and type(operation.time_preimage) is tuple
            and all(type(value) is int
                    for value in operation.time_preimage)
            and type(operation.resource_preimage) is tuple
            and type(operation.validator_surface) is tuple,
            "current allocated deadline operation required")
    require(operation.backend is None
            or (type(operation.backend) is _ModeledDeadlineClock
                and operation.backend.operation is operation),
            "allocated deadline backend backlink changed")
    _surface_validator(operation.validator_surface)
    inputs = operation.inputs
    attempt = operation.attempt
    _inputs_validator(inputs, operation)
    require(inputs._attempt is attempt
            and inputs._consumed is True
            and inputs._consumer is operation
            and attempt.poisoned is False
            and attempt.allocated_release_deadline_started is True
            and attempt.allocated_release_deadline_operation is operation
            and attempt.allocated_release_deadline_capability is capability,
            "allocated deadline ownership chain changed")
    times, resources = _preimage_reader(operation)
    require(times == operation.time_preimage
            and resources == operation.resource_preimage
            and operation.deadline_ns == inputs._terminal._deadline_ns
            and operation.lower_bound_ns == max(operation.time_preimage)
            and operation.lower_bound_ns < operation.deadline_ns,
            "allocated deadline frozen preimage changed")
    if operation.clock_returned:
        require(operation.clock_attempted is True
                and type(operation.observed_ns) is int,
                "allocated deadline returned evidence changed")
    else:
        require(operation.observed_ns is None,
                "allocated deadline absent return changed")
    return True


def _validate_allocated_release_deadline_capability(capability,
                                                    consumer=None):
    require(type(capability) is AllocatedReleaseDeadlineCapability,
            "exact allocated deadline capability type required")
    require(type(capability._sealed) is bool
            and capability._sealed is True,
            "sealed allocated deadline capability required")
    require(threading.current_thread() is capability._owner_thread,
            "allocated deadline capability owner changed")
    require(type(capability._receipt_bytes) is bytes,
            "allocated deadline capability receipt changed")
    require(type(capability._consumed) is bool,
            "allocated deadline consumption scalar changed")
    require(type(capability._reservation_started) is bool
            and capability._reservation_started
                is capability._consumed,
            "allocated deadline reservation scalar changed")
    require((consumer is None and capability._consumed is False
             and capability._consumer is None)
            or (consumer is not None and capability._consumed is True
                and capability._consumer is consumer),
            "allocated deadline capability consumption changed")
    operation = capability._operation
    require(type(operation) is AllocatedReleaseDeadlineOperation,
            "exact allocated deadline operation required")
    require(capability._inputs is operation.inputs
            and capability._attempt is operation.attempt,
            "allocated deadline capability backlinks changed")
    _validate_operation(
        operation, "MODEL_ALLOCATED_RELEASE_DEADLINE_ADMITTED_ONLY",
        capability)
    require(operation.clock_attempted is True
            and operation.clock_returned is True
            and operation.lower_bound_ns <= operation.observed_ns
            and operation.observed_ns < operation.deadline_ns,
            "allocated deadline observation no longer admitted")
    receipt = _receipt(operation.observed_ns, operation.lower_bound_ns,
                       operation.deadline_ns)
    GRAPH.CONSUMER._builtin_tree(receipt, "allocated deadline snapshot")
    require(ABORT.SUP.canonical(receipt) == capability._receipt_bytes,
            "allocated deadline receipt changed")
    return True


def validate_allocated_release_deadline_capability(capability):
    return _validate_allocated_release_deadline_capability(capability)


def _reserve_allocated_release_deadline_capability(capability, consumer):
    require(consumer is not None,
            "private allocated deadline reservation")
    _validate_allocated_release_deadline_capability(capability)
    object.__setattr__(capability, "_reservation_started", True)
    object.__setattr__(capability, "_consumed", True)
    object.__setattr__(capability, "_consumer", consumer)
    _validate_allocated_release_deadline_capability(capability, consumer)
    return True


def admit_allocated_release_deadline_once(inputs, modeled_clock):
    """Observe one modeled clock; performs no live close or authorization."""
    _validate_surface(_BASE_VALIDATOR_SURFACE)
    if (type(inputs) is INPUTS.AllocatedReleaseInputsCapability
            and type(inputs._consumed) is bool
            and inputs._consumed is True
            and type(inputs._consumer) is AllocatedReleaseDeadlineOperation):
        active = inputs._consumer
        if (type(active.active) is not bool or active.active is True
                or type(active.state) is not str
                or active.state == "ADMITTING"):
            _POISON_OPERATION(active, "REENTRANT_ALLOCATED_DEADLINE")
            if type(active.attempt) is ABORT.AbortForkAttempt:
                _poison_attempt(
                    active.attempt, "ALLOCATED_RELEASE_DEADLINE_REENTRY")
            raise Refusal("allocated deadline admission reentry")
    _VALIDATE_INPUTS(inputs)
    attempt = inputs._attempt
    operation = None
    try:
        require(attempt.poisoned is False
                and attempt.allocated_release_deadline_started is False
                and attempt.allocated_release_deadline_operation is None
                and attempt.allocated_release_deadline_capability is None,
                "fresh allocated deadline admission required")
        operation = AllocatedReleaseDeadlineOperation(_KEY, inputs)
        _ATTEMPT_SET(attempt, "allocated_release_deadline_started", True)
        _ATTEMPT_SET(attempt, "allocated_release_deadline_operation", operation)
        _RESERVE_INPUTS(inputs, operation)
        _validate_operation(operation, "ADMITTING")
        backend = _ModeledDeadlineClock(_KEY, operation, modeled_clock)
        object.__setattr__(operation, "backend", backend)
        now = _CLOCK_ONCE(backend)
        _validate_operation(operation, "ADMITTING")
        if now < operation.lower_bound_ns:
            object.__setattr__(
                operation, "state",
                "MODEL_ALLOCATED_RELEASE_DEADLINE_ROLLBACK_REFUSED")
            object.__setattr__(operation, "active", False)
            return operation
        if now >= operation.deadline_ns:
            object.__setattr__(
                operation, "state",
                "MODEL_ALLOCATED_RELEASE_DEADLINE_EXPIRED")
            object.__setattr__(operation, "active", False)
            return operation
        capability = AllocatedReleaseDeadlineCapability(_KEY, operation)
        _ATTEMPT_SET(
            attempt, "allocated_release_deadline_capability", capability)
        require(type(operation.state) is str
                and operation.state == "ADMITTING"
                and type(operation.active) is bool
                and operation.active is True
                and operation.poisoned is False
                and operation.error is None
                and operation.capability is None,
                "allocated deadline commit changed")
        object.__setattr__(operation, "capability", capability)
        object.__setattr__(
            operation, "state",
            "MODEL_ALLOCATED_RELEASE_DEADLINE_ADMITTED_ONLY")
        object.__setattr__(operation, "active", False)
        validate_allocated_release_deadline_capability(capability)
        return operation
    except BaseException as exc:
        if type(operation) is AllocatedReleaseDeadlineOperation:
            _POISON_OPERATION(operation, type(exc).__name__)
        if type(attempt) is ABORT.AbortForkAttempt:
            _poison_attempt(attempt, "ALLOCATED_RELEASE_DEADLINE")
        raise
