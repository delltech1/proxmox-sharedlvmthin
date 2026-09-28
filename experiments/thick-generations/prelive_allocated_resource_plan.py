#!/usr/bin/python3
"""Opaque source-model resource plan; performs no syscall or close."""

import threading

import prelive_abort_fork_split as ABORT
import prelive_allocated_graph_enrollment as GRAPH
import prelive_allocated_release_deadline as DEADLINE
import prelive_real_fd_adapter as FDS


_KEY = object()
_VALIDATE_DEADLINE = DEADLINE._validate_allocated_release_deadline_capability
_RESERVE_DEADLINE = DEADLINE._reserve_allocated_release_deadline_capability
_FD_INDEX = FDS._IDENTITY_KEYS.index("fd")
_PIPE_ROLES = ("status_parent", "stdout_parent", "stderr_parent")
_RELEASE_ORDER = ("epoll", *_PIPE_ROLES, "pidfd")


class Refusal(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise Refusal(message)


def _receipt(plan):
    return {
        "schema": 1,
        "classification": "MODEL_ALLOCATED_RESOURCE_PLAN_BOUND_ONLY",
        "release_order": list(_RELEASE_ORDER),
        "epoll_fd": plan._epoll_fd,
        "pipe_fds": {role: fd for role, fd in plan._pipe_fds},
        "pidfd": plan._pidfd,
        "deadline_ns": plan._deadline_ns,
        "acquisition_provenance_frozen": True,
        "ownership_binding_frozen": True,
        "ownership_transfer_modeled": False,
        "acquisition_time_ofd_proven": False,
        "live_identity_verified": False,
        "live_close_authorized": False,
        "anchors_acquired": False,
        "resources_closed": False,
        "runtime_authorized": False,
        "storage_authorized": False,
        "postcondition_verified": False}


class AllocatedResourcePlan:
    """One-consumer model binding to original allocated resource owners."""

    __slots__ = ("_deadline", "_operation", "_attempt", "_owner_thread",
                 "_allocation_authority", "_capture", "_poller",
                 "_stream_refs", "_owned_pidfd", "_epoll_fd",
                 "_pipe_fds", "_pidfd", "_deadline_ns", "_receipt_bytes",
                 "_state", "_active", "_poisoned", "_consumed",
                 "_consumer", "_sealed")

    def __init__(self, key, deadline):
        require(key is _KEY, "private allocated resource plan")
        operation = deadline._operation
        attempt = deadline._attempt
        terminal = operation.inputs._terminal
        authority = terminal._result.authority
        capture = attempt.capture_handle
        poller = capture.poller
        allocation = terminal._allocation_authority
        allocation_map = dict(allocation)
        require(type(allocation) is tuple
                and len(allocation) == len(FDS._ALL_ROLES)
                and set(allocation_map) == set(FDS._ALL_ROLES)
                and all(type(role) is str for role in allocation_map)
                and all(type(values) is tuple
                        and len(values) == len(FDS._IDENTITY_KEYS)
                        and all(type(value) is int for value in values)
                        for values in allocation_map.values()),
                "strict allocated resource-plan authority")
        pipe_fds = tuple(
            (role, allocation_map[role][_FD_INDEX]) for role in _PIPE_ROLES)
        stream_refs = tuple(
            (role, capture.streams[role]) for role in ("stdout", "stderr"))
        values = (authority.epoll_fd, terminal._owned_pidfd,
                  operation.deadline_ns)
        require(all(type(value) is int and value >= 3 for value in values[:2])
                and type(values[2]) is int and values[2] > 0
                and type(authority.epoll_fd) is int
                and authority.capture_poller is poller
                and attempt._capture_poller is poller
                and attempt.owned_pidfd is not None
                and attempt.owned_pidfd.pidfd == terminal._owned_pidfd
                and all(type(fd) is int and fd >= 3
                        for _, fd in pipe_fds)
                and len({authority.epoll_fd, terminal._owned_pidfd,
                         *(fd for _, fd in pipe_fds)}) == 5,
                "strict distinct allocated resource-plan descriptors")
        object.__setattr__(self, "_deadline", deadline)
        object.__setattr__(self, "_operation", operation)
        object.__setattr__(self, "_attempt", attempt)
        object.__setattr__(self, "_owner_thread", threading.current_thread())
        object.__setattr__(self, "_allocation_authority", allocation)
        object.__setattr__(self, "_capture", capture)
        object.__setattr__(self, "_poller", poller)
        object.__setattr__(self, "_stream_refs", stream_refs)
        object.__setattr__(self, "_owned_pidfd", attempt.owned_pidfd)
        object.__setattr__(self, "_epoll_fd", authority.epoll_fd)
        object.__setattr__(self, "_pipe_fds", pipe_fds)
        object.__setattr__(self, "_pidfd", terminal._owned_pidfd)
        object.__setattr__(self, "_deadline_ns", operation.deadline_ns)
        object.__setattr__(self, "_receipt_bytes", b"")
        object.__setattr__(self, "_state", "BINDING")
        object.__setattr__(self, "_active", True)
        object.__setattr__(self, "_poisoned", False)
        object.__setattr__(self, "_consumed", False)
        object.__setattr__(self, "_consumer", None)
        object.__setattr__(self, "_sealed", True)
        receipt = _receipt(self)
        GRAPH.CONSUMER._builtin_tree(receipt, "allocated resource plan")
        object.__setattr__(self, "_receipt_bytes", ABORT.SUP.canonical(receipt))

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise Refusal("allocated resource plan is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        raise Refusal("allocated resource plan is immutable")

    def __copy__(self):
        raise Refusal("allocated resource plan is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("allocated resource plan is noncopyable")

    def snapshot(self):
        validate_allocated_resource_plan(self)
        return _receipt(self)


def _validate_allocated_resource_plan(plan, consumer=None):
    require(type(plan) is AllocatedResourcePlan
            and threading.current_thread() is plan._owner_thread
            and type(plan._sealed) is bool and plan._sealed is True
            and type(plan._receipt_bytes) is bytes
            and type(plan._state) is str
            and plan._state == "MODEL_ALLOCATED_RESOURCE_PLAN_BOUND_ONLY"
            and type(plan._active) is bool and plan._active is False
            and type(plan._poisoned) is bool and plan._poisoned is False
            and type(plan._consumed) is bool
            and ((consumer is None and plan._consumed is False
                  and plan._consumer is None)
                 or (consumer is not None and plan._consumed is True
                     and plan._consumer is consumer)),
            "exact allocated resource plan required")
    deadline = plan._deadline
    _VALIDATE_DEADLINE(deadline, plan)
    operation = deadline._operation
    attempt = deadline._attempt
    terminal = operation.inputs._terminal
    authority = terminal._result.authority
    capture = attempt.capture_handle
    allocation = terminal._allocation_authority
    allocation_map = dict(allocation)
    require(type(plan._allocation_authority) is tuple
            and type(plan._stream_refs) is tuple
            and len(plan._stream_refs) == 2
            and all(type(item) is tuple and len(item) == 2
                    and type(item[0]) is str for item in plan._stream_refs)
            and tuple(item[0] for item in plan._stream_refs)
                == ("stdout", "stderr")
            and type(plan._pipe_fds) is tuple
            and len(plan._pipe_fds) == 3
            and all(type(item) is tuple and len(item) == 2
                    and type(item[0]) is str and type(item[1]) is int
                    for item in plan._pipe_fds)
            and tuple(item[0] for item in plan._pipe_fds) == _PIPE_ROLES
            and type(plan._epoll_fd) is int
            and type(plan._pidfd) is int
            and type(plan._deadline_ns) is int,
            "strict allocated resource-plan stored schema")
    current_streams = tuple(
        (role, capture.streams[role]) for role in ("stdout", "stderr"))
    require(all(plan._stream_refs[index][1]
                is current_streams[index][1]
                for index in range(len(current_streams))),
            "allocated resource-plan stream ownership changed")
    require(plan._operation is operation
            and plan._attempt is attempt
            and plan._allocation_authority is allocation
            and attempt.handle._allocation_authority is allocation
            and plan._capture is capture
            and plan._poller is capture.poller
            and plan._poller is attempt._capture_poller
            and plan._poller is authority.capture_poller
            and plan._owned_pidfd is attempt.owned_pidfd
            and plan._pidfd == terminal._owned_pidfd
            and plan._owned_pidfd.pidfd == plan._pidfd
            and plan._epoll_fd == authority.epoll_fd
            and plan._pipe_fds == tuple(
                (role, allocation_map[role][_FD_INDEX])
                for role in _PIPE_ROLES)
            and plan._deadline_ns == operation.deadline_ns
            and plan._deadline_ns == terminal._deadline_ns,
            "allocated resource-plan provenance changed")
    receipt = _receipt(plan)
    GRAPH.CONSUMER._builtin_tree(receipt, "allocated resource-plan receipt")
    require(ABORT.SUP.canonical(receipt) == plan._receipt_bytes,
            "allocated resource-plan receipt changed")
    return True


def validate_allocated_resource_plan(plan):
    return _validate_allocated_resource_plan(plan)


def bind_allocated_resource_plan_once(deadline):
    """Consume a modeled deadline leaf and freeze ownership; no live effect."""
    if (type(deadline) is DEADLINE.AllocatedReleaseDeadlineCapability
            and type(deadline._consumed) is bool
            and deadline._consumed is True
            and type(deadline._consumer) is AllocatedResourcePlan):
        active = deadline._consumer
        if (type(active._active) is not bool or active._active is True
                or type(active._state) is not str
                or active._state == "BINDING"):
            object.__setattr__(active, "_poisoned", True)
            object.__setattr__(active, "_active", False)
            object.__setattr__(active, "_state", "UNKNOWN")
            raise Refusal("allocated resource-plan bind reentry")
    _VALIDATE_DEADLINE(deadline)
    plan = None
    try:
        plan = AllocatedResourcePlan(_KEY, deadline)
        _RESERVE_DEADLINE(deadline, plan)
        require(type(plan._state) is str and plan._state == "BINDING"
                and type(plan._active) is bool and plan._active is True
                and type(plan._poisoned) is bool and plan._poisoned is False,
                "allocated resource-plan commit changed")
        object.__setattr__(
            plan, "_state", "MODEL_ALLOCATED_RESOURCE_PLAN_BOUND_ONLY")
        object.__setattr__(plan, "_active", False)
        _validate_allocated_resource_plan(plan)
        return plan
    except BaseException:
        if type(plan) is AllocatedResourcePlan:
            object.__setattr__(plan, "_poisoned", True)
            object.__setattr__(plan, "_active", False)
            object.__setattr__(plan, "_state", "UNKNOWN")
        raise
