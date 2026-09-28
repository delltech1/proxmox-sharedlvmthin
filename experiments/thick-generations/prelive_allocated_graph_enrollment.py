#!/usr/bin/python3
"""Bind and exactly persist one close-only FD graph's V2 INTENT bytes.

Source/real-pipe/temp-file checkpoint: no fork, exec, grant or storage.
"""

import copy
import hashlib
import re
import threading

import prelive_launcher_identity_consumer as CONSUMER
import prelive_owned_child as OWNED
import prelive_real_fd_adapter as FDS
import prelive_descendant_accounting as DESC
import prelive_supervisor_model as SUP
import prelive_v2_intent_file_bridge as INTENT


HEX32 = re.compile(r"[0-9a-f]{32}")
FD_ROLES = ("grant_child", "grant_parent", "status_parent", "status_child",
            "stdout_parent", "stdout_child", "stderr_parent", "stderr_child")
_ENROLL_KEY = object()
_BOUND_KEY = object()
_PERSISTED_KEY = object()
_PERSISTED_CLAIM_KEY = object()
_DOMAIN_ENROLL_KEY = object()
_FORK_RECORD_KEY = object()


class Refusal(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise Refusal(message)


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _builtin_child_domain_namespace(domain):
    return (type(domain) is DESC.ChildDomain
            and type(domain.__dict__) is dict
            and all(type(key) is str for key in domain.__dict__))


def _strict_fresh_child_domain(domain, bridge_binding=None):
    require(_builtin_child_domain_namespace(domain)
            and set(domain.__dict__) == {
                "owner", "request_id", "domain_id", "deadline_ns",
                "owner_thread", "_seal", "state", "launcher_claimed",
                "leader", "expected_leader", "records", "pending",
                "terminal_echild", "sequence", "last_clock_ns",
                "bridge_enrollment"},
            "exact child domain required")
    CONSUMER._builtin_tree(domain.owner, "allocated descendant domain owner")
    require(type(domain.owner) is dict
            and type(domain.request_id) is str
            and 1 <= len(domain.request_id) <= 128
            and type(domain.domain_id) is str
            and HEX32.fullmatch(domain.domain_id)
            and type(domain.deadline_ns) is int and domain.deadline_ns > 0
            and type(domain.state) is str and domain.state == "PREPARED"
            and type(domain.launcher_claimed) is bool
            and domain.launcher_claimed is False
            and domain.leader is None and domain.expected_leader is None
            and type(domain.records) is list and len(domain.records) == 0
            and domain.pending is None and domain.terminal_echild is None
            and type(domain.sequence) is int and domain.sequence == 0
            and type(domain.last_clock_ns) is int
            and domain.last_clock_ns >= 0
            and domain.bridge_enrollment is bridge_binding
            and type(domain._seal) is object,
            "strict fresh child domain required")


def _strict_claimed_child_domain(domain, bridge_binding):
    require(_builtin_child_domain_namespace(domain)
            and set(domain.__dict__) == {
                "owner", "request_id", "domain_id", "deadline_ns",
                "owner_thread", "_seal", "state", "launcher_claimed",
                "leader", "expected_leader", "records", "pending",
                "terminal_echild", "sequence", "last_clock_ns",
                "bridge_enrollment"},
            "exact claimed child domain required")
    CONSUMER._builtin_tree(domain.owner,
                           "allocated claimed descendant owner")
    CONSUMER._builtin_tree(domain.expected_leader,
                           "allocated expected descendant leader")
    require(type(domain.owner) is dict
            and type(domain.request_id) is str
            and 1 <= len(domain.request_id) <= 128
            and type(domain.domain_id) is str
            and HEX32.fullmatch(domain.domain_id)
            and type(domain.deadline_ns) is int and domain.deadline_ns > 0
            and type(domain.state) is str
            and domain.state == "LAUNCHER_CLAIMED"
            and type(domain.launcher_claimed) is bool
            and domain.launcher_claimed is True
            and domain.leader is None
            and type(domain.expected_leader) is dict
            and set(domain.expected_leader) == {
                "pid", "starttime", "boot_id"}
            and type(domain.expected_leader["pid"]) is int
            and domain.expected_leader["pid"] > 0
            and type(domain.expected_leader["starttime"]) is int
            and domain.expected_leader["starttime"] > 0
            and type(domain.expected_leader["boot_id"]) is str
            and type(domain.records) is list and len(domain.records) == 0
            and domain.pending is None and domain.terminal_echild is None
            and type(domain.sequence) is int and domain.sequence == 0
            and type(domain.last_clock_ns) is int
            and domain.last_clock_ns >= 0
            and domain.bridge_enrollment is bridge_binding
            and type(domain._seal) is object,
            "strict claimed child domain required")


def _strict_allocated_fork_lifecycle(lifecycle, expected_state=None):
    require(type(lifecycle) is dict
            and all(type(key) is str for key in lifecycle)
            and set(lifecycle) == {
                "schema", "state", "token", "supervisor",
                "owned_child", "origin"},
            "strict allocated descendant lifecycle required")
    require((expected_state is None or type(expected_state) is str)
            and type(lifecycle["schema"]) is int
            and lifecycle["schema"] == 1
            and type(lifecycle["state"]) is str
            and lifecycle["state"] == (
                expected_state if expected_state is not None
                else lifecycle["state"])
            and lifecycle["state"] in (
                "SPAWNED_UNBOUND", "PIDFD_BOUND",
                "OWNED_LEADER_REAPED_ONLY")
            and type(lifecycle["token"]) is str
            and HEX32.fullmatch(lifecycle["token"]),
            "strict allocated descendant lifecycle scalars")
    CONSUMER._builtin_tree(lifecycle["supervisor"],
                           "allocated descendant fork supervisor")
    CONSUMER._builtin_tree(lifecycle["owned_child"],
                           "allocated descendant fork child")
    child = lifecycle["owned_child"]
    origin = lifecycle["origin"]
    require(type(child) is dict
            and set(child) == {"pid", "starttime"}
            and type(child["pid"]) is int and child["pid"] > 0
            and type(origin) is OWNED._ForkOrigin
            and type(origin.__dict__) is dict
            and all(type(key) is str for key in origin.__dict__)
            and set(origin.__dict__) == {
                "pid", "claimed", "pre_fork_enrollment"}
            and type(origin.pid) is int and origin.pid > 0
            and type(origin.claimed) is bool
            and type(origin.pre_fork_enrollment)
            is OWNED._PreForkDescendantEnrollment,
            "strict allocated descendant child/origin")
    if lifecycle["state"] == "SPAWNED_UNBOUND":
        require(child["starttime"] is None and origin.claimed is False,
                "strict unbound allocated descendant lifecycle")
    else:
        require(type(child["starttime"]) is int
                and child["starttime"] > 0 and origin.claimed is True,
                "strict pidfd-bound allocated descendant lifecycle")
    require(origin.pid == child["pid"],
            "allocated descendant origin/child mismatch")
    return origin


class AllocatedDescendantEnrollment:
    """One graph/domain composition using the graph's original fork ticket."""
    def __init__(self, key, graph, domain, cleanup_ms, prepared_ns):
        require(key is _DOMAIN_ENROLL_KEY,
                "private allocated descendant enrollment")
        self.graph = graph
        self.domain = domain
        self.binding = graph.binding
        self.pre_fork_enrollment = graph.ticket
        self.cleanup_ms = cleanup_ms
        self.execution_deadline_ns = graph.deadline_ns
        self.cleanup_hard_limit_ns = (
            graph.deadline_ns + cleanup_ms * 1000000)
        self.prepared_ns = prepared_ns
        self.owner_thread = threading.current_thread()
        self.domain_id = domain.domain_id
        self.domain_seal = domain._seal
        self.domain_prepared_watermark_ns = domain.last_clock_ns
        self.fork_attempt = None
        self.fork_lifecycle = None
        self.fork_origin = None
        self.fork_pid = None
        self.fork_token = None
        self._fork_supervisor_bytes = None
        self.used = False
        self.state = "READY"
        self._owner_bytes = SUP.canonical(graph.owner)
        self._sealed = True

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise Refusal("allocated descendant enrollment is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        raise Refusal("allocated descendant enrollment is immutable")

    def __copy__(self):
        raise Refusal("allocated descendant enrollment is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("allocated descendant enrollment is noncopyable")

    def _poison(self):
        object.__setattr__(self, "used", True)
        object.__setattr__(self, "state", "UNKNOWN")
        if _builtin_child_domain_namespace(self.domain):
            DESC.poison_bridge_domain(DESC._BRIDGE_KEY, self.domain)

    def _validate(self, graph_state):
        require(type(self.__dict__) is dict
                and all(type(key) is str for key in self.__dict__)
                and set(self.__dict__) == {
                    "graph", "domain", "binding", "pre_fork_enrollment",
                    "cleanup_ms", "execution_deadline_ns",
                    "cleanup_hard_limit_ns", "prepared_ns", "owner_thread",
                    "domain_id", "domain_seal",
                    "domain_prepared_watermark_ns", "fork_attempt",
                    "fork_lifecycle", "fork_origin", "fork_pid",
                    "fork_token", "_fork_supervisor_bytes", "used",
                    "state", "_owner_bytes", "_sealed"}
                and type(self.domain_id) is str
                and HEX32.fullmatch(self.domain_id)
                and type(self.domain_seal) is object
                and type(self.domain_prepared_watermark_ns) is int
                and self.domain_prepared_watermark_ns >= 0
                and type(self._owner_bytes) is bytes
                and type(self.used) is bool and self.used is False
                and type(self.state) is str and self.state == "READY"
                and self.fork_attempt is None
                and self.fork_lifecycle is None
                and self.fork_origin is None
                and self.fork_pid is None and self.fork_token is None
                and self._fork_supervisor_bytes is None
                and type(self._sealed) is bool and self._sealed is True,
                "strict allocated descendant enrollment required")
        _strict_fresh_child_domain(self.domain, self.binding)
        require(type(self.graph) is AllocatedNoGrantGraphEnrollment
                and type(self.domain) is DESC.ChildDomain
                and self.graph.domain_enrollment is self
                and self.graph.state == graph_state
                and self.graph.binding is self.binding
                and self.graph.ticket is self.pre_fork_enrollment
                and type(self.pre_fork_enrollment)
                is OWNED._PreForkDescendantEnrollment
                and self.pre_fork_enrollment.used is False
                and self.pre_fork_enrollment.binding is self.binding
                and threading.current_thread() is self.owner_thread
                and self.owner_thread is self.graph.owner_thread
                and type(self.cleanup_ms) is int
                and 1 <= self.cleanup_ms <= 10000
                and type(self.prepared_ns) is int
                and self.graph.started_ns <= self.prepared_ns
                and self.prepared_ns < self.execution_deadline_ns
                and self.domain_prepared_watermark_ns <= self.prepared_ns
                and self.execution_deadline_ns == self.graph.deadline_ns
                and self.cleanup_hard_limit_ns
                == self.execution_deadline_ns + self.cleanup_ms * 1000000,
                "allocated descendant enrollment changed")
        require(self.domain.owner_thread is self.owner_thread
                and self.domain.request_id == self.graph.request_id
                and self.domain.domain_id == self.domain_id
                and self.domain._seal is self.domain_seal
                and self.domain.last_clock_ns
                == self.domain_prepared_watermark_ns
                and self.domain.deadline_ns == self.cleanup_hard_limit_ns
                and SUP.canonical(self.domain.owner) == self._owner_bytes
                and SUP.canonical(self.graph.owner) == self._owner_bytes
                and SUP.canonical(self.pre_fork_enrollment.supervisor)
                == self._owner_bytes,
                "allocated descendant domain changed")

    def _validate_forked(self, attempt, expected_attempt_state,
                         expected_shared_state="FORKED",
                         expected_used=False,
                         expected_domain_state="PREPARED",
                         expected_lifecycle_state=None):
        import prelive_abort_fork_split as ABORT
        require(type(self.__dict__) is dict
                and all(type(key) is str for key in self.__dict__)
                and set(self.__dict__) == {
                    "graph", "domain", "binding", "pre_fork_enrollment",
                    "cleanup_ms", "execution_deadline_ns",
                    "cleanup_hard_limit_ns", "prepared_ns", "owner_thread",
                    "domain_id", "domain_seal",
                    "domain_prepared_watermark_ns", "fork_attempt",
                    "fork_lifecycle", "fork_origin", "fork_pid",
                    "fork_token", "_fork_supervisor_bytes", "used",
                    "state", "_owner_bytes", "_sealed"}
                and type(attempt) is ABORT.AbortForkAttempt
                and type(expected_attempt_state) is str
                and type(expected_shared_state) is str
                and type(expected_used) is bool
                and type(expected_domain_state) is str
                and (expected_lifecycle_state is None
                     or type(expected_lifecycle_state) is str)
                and expected_domain_state in (
                    "PREPARED", "LAUNCHER_CLAIMED")
                and type(attempt.state) is str
                and attempt.state == expected_attempt_state
                and attempt.poisoned is False
                and attempt.fork_attempted is True
                and attempt.child_may_exist is True
                and threading.current_thread() is self.owner_thread
                and attempt.owner_thread is self.owner_thread
                and self.fork_attempt is attempt
                and type(self.fork_lifecycle) is dict
                and self.fork_lifecycle is attempt.lifecycle
                and type(self.fork_origin) is OWNED._ForkOrigin
                and type(self.fork_pid) is int and self.fork_pid > 0
                and type(self.fork_token) is str
                and type(self._fork_supervisor_bytes) is bytes
                and type(self.state) is str
                and self.state == expected_shared_state
                and type(self.used) is bool and self.used is expected_used
                and type(self._sealed) is bool and self._sealed is True,
                "strict allocated descendant fork record required")
        if expected_domain_state == "PREPARED":
            _strict_fresh_child_domain(self.domain, self.binding)
        else:
            _strict_claimed_child_domain(self.domain, self.binding)
        lifecycle = self.fork_lifecycle
        origin = _strict_allocated_fork_lifecycle(
            lifecycle, expected_lifecycle_state)
        CONSUMER._builtin_tree(self.graph.owner,
                               "allocated descendant graph owner")
        require(self.fork_origin is origin
                and lifecycle["token"] == self.fork_token,
                "allocated descendant lifecycle changed")
        require(type(self.graph) is AllocatedNoGrantGraphEnrollment
                and self.graph.domain_enrollment is self
                and self.graph.state == "BOUND"
                and self.graph._poisoned is False
                and self.graph.binding is self.binding
                and self.graph.ticket is self.pre_fork_enrollment
                and self.pre_fork_enrollment.binding is self.binding
                and self.pre_fork_enrollment.used is True
                and self.fork_origin.pre_fork_enrollment
                is self.pre_fork_enrollment
                and self.fork_origin.pid == self.fork_pid
                and attempt.enrollment is self.graph
                and attempt.ticket is self.pre_fork_enrollment
                and attempt.returned_pid == self.fork_pid
                and lifecycle["token"] == attempt._lifecycle_token
                and lifecycle["owned_child"]["pid"] == self.fork_pid
                and SUP.canonical(lifecycle["supervisor"])
                == self._fork_supervisor_bytes == self._owner_bytes
                and SUP.canonical(self.graph.owner) == self._owner_bytes
                and SUP.canonical(self.domain.owner) == self._owner_bytes
                and self.domain.owner_thread is self.owner_thread
                and self.domain.request_id == self.graph.request_id
                and self.domain.domain_id == self.domain_id
                and self.domain._seal is self.domain_seal
                and self.domain.last_clock_ns
                == self.domain_prepared_watermark_ns
                and self.domain.deadline_ns == self.cleanup_hard_limit_ns,
                "allocated descendant fork chain changed")
        owned = attempt.owned_pidfd
        if owned is None:
            require(lifecycle["state"] == "SPAWNED_UNBOUND"
                    and expected_attempt_state in {
                        "FORKING", "LINUX_FORKING",
                        "CHILD_EXIT_OBSERVED_UNREAPED"},
                    "unbound descendant fork phase mismatch")
        else:
            require(type(owned) is OWNED._OwnedPidfdHandle
                    and lifecycle["state"] == (
                        expected_lifecycle_state
                        if expected_lifecycle_state is not None
                        else "PIDFD_BOUND")
                    and owned.lifecycle is lifecycle
                    and type(owned.child) is dict,
                    "pidfd-bound descendant fork authority required")
            CONSUMER._builtin_tree(
                owned.child, "allocated descendant owned child")
            require(type(attempt._owned_child_bytes) is bytes
                    and lifecycle["owned_child"] == {
                        "pid": owned.child["pid"],
                        "starttime": owned.child["starttime"]}
                    and type(owned.child["pid"]) is int
                    and owned.child["pid"] == self.fork_pid
                    and type(owned.child["starttime"]) is int
                    and owned.child["starttime"] > 0
                    and type(owned.child["boot_id"]) is str
                    and owned.child["boot_id"] == self.graph.owner["boot_id"]
                    and SUP.canonical(owned.child)
                    == attempt._owned_child_bytes,
                    "pidfd-bound descendant child changed")
            if expected_domain_state == "LAUNCHER_CLAIMED":
                require(self.domain.expected_leader == owned.child,
                        "claimed descendant leader changed")


def _strict_bound_request(bound):
    CONSUMER._builtin_tree(bound.request, "stored bound no-grant request")
    current = CONSUMER.validate_v2_request(bound.request)
    require(SUP.canonical(current) == bound._request_bytes
            and CONSUMER.encode_v2_intent(
                current, bound.enrollment.owner) == bound.intent_bytes,
            "bound no-grant request changed")
    return current


def _strict_persistence_receipt(receipt, bound, request):
    CONSUMER._builtin_tree(receipt, "exact persisted INTENT receipt")
    record = receipt.get("record_identity") if type(receipt) is dict else None
    require(type(receipt) is dict and set(receipt) == {
                "schema", "classification", "request_id", "bytes",
                "sha256", "name", "record_identity", "child_started",
                "grant_attempted", "runtime_authorized",
                "storage_authorized", "exec_proven",
                "power_loss_durability_proven"}
            and type(receipt["schema"]) is int and receipt["schema"] == 1
            and receipt["classification"]
            == "V2_INTENT_EXACT_BYTES_VERIFIED"
            and receipt["request_id"] == request["request_id"]
            and type(receipt["bytes"]) is int
            and receipt["bytes"] == len(bound.intent_bytes)
            and type(receipt["sha256"]) is str
            and receipt["sha256"]
            == hashlib.sha256(bound.intent_bytes).hexdigest()
            and receipt["name"] == "exact-event-000001.json"
            and type(record) is dict and set(record) == {
                "dev", "inode", "uid", "mode", "nlink"}
            and all(type(record[key]) is int and record[key] >= 0
                    for key in ("dev", "inode", "uid", "mode", "nlink"))
            and record["inode"] > 0 and record["mode"] == 0o600
            and record["nlink"] == 1
            and receipt["child_started"] is False
            and receipt["grant_attempted"] is False
            and receipt["runtime_authorized"] is False
            and receipt["storage_authorized"] is False
            and receipt["exec_proven"] is False
            and receipt["power_loss_durability_proven"] is False,
            "exact persisted INTENT receipt required")
    return copy.deepcopy(receipt)


class BoundNoGrantIntentCapability:
    def __init__(self, key, enrollment, request, intent_bytes, bind_now_ns):
        require(key is _BOUND_KEY, "private bound no-grant capability")
        CONSUMER._builtin_tree(request, "bound no-grant request")
        require(type(intent_bytes) is bytes and _integer(bind_now_ns),
                "strict bound no-grant inputs")
        self.enrollment = enrollment
        self.handle = enrollment.handle
        self.ticket = enrollment.ticket
        self.request = copy.deepcopy(request)
        self.intent_bytes = intent_bytes
        self.bind_now_ns = bind_now_ns
        self.owner_thread = threading.current_thread()
        self.used = False
        self.state = "BOUND"
        self.consumer = None
        self.persistence_started = False
        self.persistence_bridge = None
        self.persisted_capability = None
        self._request_bytes = SUP.canonical(request)
        self._sealed = True

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise Refusal("bound no-grant capability is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        raise Refusal("bound no-grant capability is immutable")

    def __copy__(self):
        raise Refusal("bound no-grant capability is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("bound no-grant capability is noncopyable")

    def _claim_after_persistence(self, key, persisted, consumer, now_ns):
        enrollment = self.enrollment
        if (key is not _PERSISTED_CLAIM_KEY
                or type(persisted) is not PersistedNoGrantIntentCapability
                or persisted.bound is not self
                or self.persisted_capability is not persisted
                or self.persistence_bridge is not persisted.bridge
                or self.persistence_started is not True
                or self.used or self.state != "PERSISTED" or consumer is None
                or persisted.state != "CLAIMING"
                or threading.current_thread() is not self.owner_thread):
            object.__setattr__(self, "used", True)
            object.__setattr__(self, "state", "UNKNOWN")
            raise Refusal("fresh bound no-grant capability required")
        object.__setattr__(self, "used", True)
        object.__setattr__(self, "state", "CLAIMING")
        object.__setattr__(self, "consumer", consumer)
        try:
            enrollment._validate("BOUND")
            require(_integer(now_ns)
                    and _integer(persisted.persisted_now_ns)
                    and persisted.persisted_now_ns >= self.bind_now_ns
                    and now_ns >= persisted.persisted_now_ns
                    and now_ns < enrollment.deadline_ns,
                    "no-grant capability deadline reached")
            enrollment.handle.validate()
            enrollment._validate("BOUND", validate_handle=False)
            persisted._validate_frozen("CLAIMING")
            CONSUMER._builtin_tree(self.request,
                                   "post-callback bound no-grant request")
            current_request = CONSUMER.validate_v2_request(self.request)
            current_request_bytes = SUP.canonical(current_request)
            expected_intent = CONSUMER.encode_v2_intent(
                current_request, enrollment.owner)
            require(self.used is True and self.state == "CLAIMING"
                    and self.consumer is consumer
                    and enrollment.bound_capability is self
                    and self.persisted_capability is persisted
                    and persisted.state == "CLAIMING"
                    and enrollment.ticket.used is False
                    and current_request_bytes == self._request_bytes
                    and expected_intent == self.intent_bytes,
                    "bound no-grant authority changed")
            require(self.used is True and self.state == "CLAIMING"
                    and self.consumer is consumer
                    and enrollment.bound_capability is self
                    and self.persisted_capability is persisted
                    and persisted.state == "CLAIMING",
                    "bound no-grant claim changed before completion")
            object.__setattr__(self, "state", "CLAIMED")
            return {"schema": 1,
                    "classification": "NO_GRANT_GRAPH_INTENT_CLAIMED",
                    "ticket_used": False, "grant_attempted": False,
                    "runtime_authorized": False,
                    "storage_authorized": False, "exec_proven": False}
        except BaseException:
            object.__setattr__(self, "state", "UNKNOWN")
            enrollment._poison()
            raise

    def claim_for_abort_once(self, consumer, now_ns):
        raise Refusal("exact persisted no-grant capability required")


class PersistedNoGrantIntentCapability:
    def __init__(self, key, bound, bridge, receipt, persisted_now_ns):
        require(key is _PERSISTED_KEY,
                "private persisted no-grant capability")
        CONSUMER._builtin_tree(receipt, "persisted no-grant receipt")
        self.bound = bound
        self.enrollment = bound.enrollment
        self.handle = bound.handle
        self.ticket = bound.ticket
        self.bridge = bridge
        self.receipt = copy.deepcopy(receipt)
        self.persisted_now_ns = persisted_now_ns
        self.owner_thread = threading.current_thread()
        self.state = "READY"
        self._receipt_bytes = SUP.canonical(receipt)
        self._sealed = True

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise Refusal("persisted no-grant capability is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        raise Refusal("persisted no-grant capability is immutable")

    def __copy__(self):
        raise Refusal("persisted no-grant capability is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("persisted no-grant capability is noncopyable")

    def claim_for_abort_once(self, consumer, now_ns):
        if (self.state != "READY"
                or threading.current_thread() is not self.owner_thread):
            object.__setattr__(self, "state", "UNKNOWN")
            raise Refusal("fresh persisted no-grant capability required")
        object.__setattr__(self, "state", "CLAIMING")
        try:
            self._validate_frozen("CLAIMING")
            result = self.bound._claim_after_persistence(
                _PERSISTED_CLAIM_KEY, self, consumer, now_ns)
            require(self.state == "CLAIMING",
                    "persisted no-grant claim changed reentrantly")
            object.__setattr__(self, "state", "CLAIMED")
            return result
        except BaseException:
            object.__setattr__(self, "state", "UNKNOWN")
            self.enrollment._poison()
            raise

    def _validate_frozen(self, expected):
        request = _strict_bound_request(self.bound)
        current = _strict_persistence_receipt(
            self.receipt, self.bound, request)
        CONSUMER._builtin_tree(self.bridge.receipt,
                               "stored bridge INTENT receipt")
        bridge_receipt = _strict_persistence_receipt(
            self.bridge.receipt, self.bound, request)
        bound_states = {"READY": ("PERSISTED",),
                        "CLAIMING": ("PERSISTED", "CLAIMING"),
                        "CLAIMED": ("CLAIMED",)}.get(expected)
        require(bound_states is not None
                and self.state == expected
                and threading.current_thread() is self.owner_thread
                and self.bound.persisted_capability is self
                and self.bound.persistence_bridge is self.bridge
                and self.bound.state in bound_states
                and self.bridge.state == "SEALED_INTENT"
                and SUP.canonical(current) == self._receipt_bytes
                and SUP.canonical(bridge_receipt) == self._receipt_bytes
                and _integer(self.persisted_now_ns)
                and self.persisted_now_ns >= self.bound.bind_now_ns
                and self.persisted_now_ns < self.enrollment.deadline_ns,
                "persisted no-grant authority changed")


class AllocatedNoGrantGraphEnrollment:
    def __init__(self, key, handle, owner, request_id, graph_id, ticket_id,
                 started_ns, deadline_ns):
        require(key is _ENROLL_KEY, "private allocated graph enrollment")
        self.owner_thread = threading.current_thread()
        self.state = "ENROLLING"
        self._poisoned = False
        self._bind_started = False
        self._bind_now_ns = None
        self.handle = handle
        require(type(handle) is FDS.RealFDHandle,
                "exact real FD handle required")
        CONSUMER._builtin_tree(owner, "allocated graph owner")
        require(type(owner) is dict and type(owner.get("boot_id")) is str,
                "strict allocated graph owner")
        self.owner = SUP.validate_owner(
            copy.deepcopy(owner), owner["boot_id"])
        CONSUMER._builtin_tree(handle.owner, "real FD handle owner")
        require(SUP.canonical(handle.owner) == SUP.canonical(self.owner),
                "real FD handle owner mismatch")
        self.request_id = request_id
        self.graph_id = graph_id
        self.ticket_id = ticket_id
        self.started_ns = started_ns
        self.deadline_ns = deadline_ns
        self.binding = object()
        self.ticket = OWNED.prepare_descendant_fork_enrollment(
            OWNED._PREFORK_KEY, self.binding, self.owner)
        self._domain_enrollment_started = False
        self.domain_enrollment = None
        self.bound_capability = None
        self._sealed = True
        require(type(request_id) is str and HEX32.fullmatch(request_id)
                and type(graph_id) is str and HEX32.fullmatch(graph_id)
                and type(ticket_id) is str and HEX32.fullmatch(ticket_id)
                and _integer(started_ns)
                and _integer(deadline_ns, 1) and deadline_ns > started_ns,
                "strict allocated graph enrollment input")
        snapshot = handle.claim_close_only_once(self)
        CONSUMER._builtin_tree(snapshot, "initial allocated graph snapshot")
        CONSUMER._builtin_tree(self.owner,
                               "post-callback allocated graph owner")
        require(snapshot["grant_policy"] == FDS.CLOSE_ONLY_NO_WRITE
                and snapshot["consumer_claimed"] is True
                and snapshot["classification"] == "FD_GRAPH_READY"
                and SUP.canonical(snapshot["owner"])
                == SUP.canonical(self.owner)
                and snapshot["owned_roles"] == sorted(FD_ROLES)
                and snapshot["lost_roles"] == []
                and snapshot["poisoned"] is False
                and self.state == "ENROLLING"
                and self._poisoned is False
                and handle._consumer_claim is self,
                "fresh close-only graph claim required")
        endpoints = []
        for role in FD_ROLES:
            identity = snapshot["bundle"][role]
            endpoints.append({"role": role, **copy.deepcopy(identity)})
        descriptor = {
            "schema": 1, "kind": "NO_GRANT_ALLOCATED_FD_GRAPH_V1",
            "request_id": request_id, "graph_id": graph_id,
            "supervisor": copy.deepcopy(self.owner),
            "pre_fork_ticket_id": ticket_id,
            "phase": "PARENT_PRE_FORK_ALL_ENDPOINTS_OWNED",
            "grant_policy": "CLOSE_ONLY_NO_WRITE",
            "endpoints": endpoints, "endpoint_count": 8,
            "all_cloexec": True, "parent_nonblocking": True}
        CONSUMER._builtin_tree(descriptor, "allocated FD descriptor")
        object.__setattr__(self, "descriptor", descriptor)
        object.__setattr__(self, "graph_digest", SUP.digest(descriptor))
        object.__setattr__(self, "_descriptor_bytes",
                           SUP.canonical(descriptor))
        object.__setattr__(self, "_owner_bytes", SUP.canonical(self.owner))
        require(self.state == "ENROLLING" and self._poisoned is False,
                "allocated graph enrollment changed during construction")
        object.__setattr__(self, "state", "GRAPH_ENROLLED")

    def __setattr__(self, name, value):
        if (getattr(self, "_sealed", False)
                and name in {"_sealed", "state", "_poisoned",
                             "owner_thread",
                             "_bind_started", "_bind_now_ns",
                             "_domain_enrollment_started",
                             "domain_enrollment",
                             "bound_capability", "handle", "owner",
                             "request_id", "graph_id",
                             "ticket_id", "started_ns", "deadline_ns",
                             "binding", "ticket", "descriptor",
                             "graph_digest", "_descriptor_bytes",
                             "_owner_bytes"}):
            raise Refusal("allocated graph authority is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        if getattr(self, "_sealed", False):
            raise Refusal("allocated graph authority is immutable")
        object.__delattr__(self, name)

    def __copy__(self):
        raise Refusal("allocated graph enrollment is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("allocated graph enrollment is noncopyable")

    def _poison(self):
        object.__setattr__(self, "_poisoned", True)
        object.__setattr__(self, "state", "UNKNOWN")
        enrollment = self.domain_enrollment
        if type(enrollment) is AllocatedDescendantEnrollment:
            enrollment._poison()

    def _validate(self, expected, validate_handle=True):
        self._validate_frozen(expected)
        if validate_handle:
            snapshot = self.handle.validate()
            CONSUMER._builtin_tree(snapshot, "allocated graph snapshot")
            require(snapshot["grant_policy"] == FDS.CLOSE_ONLY_NO_WRITE
                    and snapshot["consumer_claimed"] is True
                    and SUP.canonical(snapshot["owner"])
                    == self._owner_bytes
                    and snapshot["owned_roles"] == sorted(FD_ROLES)
                    and snapshot["lost_roles"] == []
                    and snapshot["poisoned"] is False,
                    "allocated graph no longer fresh")
            self._validate_frozen(expected)

    def _validate_frozen(self, expected):
        CONSUMER._builtin_tree(self.owner, "stored allocated graph owner")
        require(type(self.ticket) is OWNED._PreForkDescendantEnrollment,
                "exact allocated graph ticket required")
        CONSUMER._builtin_tree(
            self.ticket.supervisor, "stored allocated graph ticket owner")
        CONSUMER._builtin_tree(self.descriptor,
                               "stored allocated graph descriptor")
        require(threading.current_thread() is self.owner_thread
                and self.state == expected
                and self._poisoned is False
                and type(self.handle) is FDS.RealFDHandle
                and self.handle._consumer_claim is self
                and self.handle._consumer_claim_started is True
                and self.handle._grant_policy == FDS.CLOSE_ONLY_NO_WRITE
                and self.ticket.binding is self.binding
                and self.ticket.used is False
                and SUP.canonical(self.ticket.supervisor) == self._owner_bytes
                and SUP.canonical(self.owner) == self._owner_bytes
                and self.descriptor["request_id"] == self.request_id
                and self.descriptor["graph_id"] == self.graph_id
                and self.descriptor["pre_fork_ticket_id"] == self.ticket_id
                and SUP.canonical(self.descriptor["supervisor"])
                == self._owner_bytes
                and SUP.canonical(self.descriptor) == self._descriptor_bytes
                and SUP.digest(self.descriptor) == self.graph_digest
                and ((expected == "GRAPH_ENROLLED"
                      and self._bind_started is False
                      and self._bind_now_ns is None
                      and self.bound_capability is None)
                     or (expected in ("BINDING", "BOUND")
                         and self._bind_started is True
                         and _integer(self._bind_now_ns)
                         and (expected != "BOUND"
                              or (type(self.bound_capability)
                                  is BoundNoGrantIntentCapability
                                  and self.bound_capability.enrollment is self
                                  and self.bound_capability.handle
                                  is self.handle
                                  and self.bound_capability.ticket
                                  is self.ticket)))),
                "allocated graph enrollment changed")
        if self.domain_enrollment is None:
            require(self._domain_enrollment_started is False,
                    "allocated descendant enrollment disappeared")
        else:
            require(self._domain_enrollment_started is True
                    and type(self.domain_enrollment)
                    is AllocatedDescendantEnrollment,
                    "allocated descendant enrollment backlink changed")
            self.domain_enrollment._validate(expected)

    def bind_once(self, request, intent_bytes, now_ns):
        if (self.state != "GRAPH_ENROLLED" or self.bound_capability is not None
                or self._bind_started or self._poisoned):
            self._poison()
            raise Refusal("fresh allocated graph enrollment required")
        object.__setattr__(self, "_bind_started", True)
        object.__setattr__(self, "_bind_now_ns", now_ns)
        object.__setattr__(self, "state", "BINDING")
        try:
            self._validate("BINDING")
            require(_integer(now_ns) and now_ns >= self.started_ns
                    and now_ns < self.deadline_ns,
                    "allocated graph binding deadline reached")
            current = CONSUMER.validate_v2_request(request)
            require(current["request_id"] == self.request_id
                    and current["boot_id"] == self.owner["boot_id"]
                    and current["launcher_identity"]["run_binding"]
                    ["fd_graph_digest"] == self.graph_digest
                    and current["launcher_identity"]["run_binding"]
                    ["unarmed_deadline_ns"] == self.deadline_ns
                    and SUP.canonical(current["launcher_identity"]
                                      ["run_binding"]["supervisor"])
                    == self._owner_bytes,
                    "V2 request does not bind allocated graph")
            if self.domain_enrollment is not None:
                require(now_ns >= self.domain_enrollment.prepared_ns
                        and current["cleanup_ms"]
                        == self.domain_enrollment.cleanup_ms,
                        "V2 bind does not follow descendant enrollment")
            INTENT._strict_event(intent_bytes)
            expected = CONSUMER.encode_v2_intent(current, self.owner)
            require(type(intent_bytes) is bytes and intent_bytes == expected,
                    "exact allocated-graph V2 INTENT required")
            capability = BoundNoGrantIntentCapability(
                _BOUND_KEY, self, current, intent_bytes, now_ns)
            object.__setattr__(self, "bound_capability", capability)
            object.__setattr__(self, "state", "BOUND")
            self._validate("BOUND")
            return capability
        except BaseException:
            self._poison()
            raise


def enroll_allocated_no_grant_graph(handle, owner, request_id, graph_id,
                                    ticket_id, started_ns, deadline_ns):
    return AllocatedNoGrantGraphEnrollment(
        _ENROLL_KEY, handle, owner, request_id, graph_id, ticket_id,
        started_ns, deadline_ns)


def enroll_allocated_descendant_domain(graph, domain, cleanup_ms, now_ns):
    """Compose one prepared ChildDomain with the graph's original ticket.

    ``domain.deadline_ns`` is a frozen cleanup hard limit, not the eventual
    settlement deadline.  A later checkpoint must narrow it exactly once
    before descendant draining; this function never extends either deadline.
    """
    require(type(graph) is AllocatedNoGrantGraphEnrollment
            and type(domain) is DESC.ChildDomain,
            "exact allocated graph and child domain required")
    try:
        require(type(cleanup_ms) is int and 1 <= cleanup_ms <= 10000
                and _integer(now_ns),
                "strict descendant cleanup policy")
        CONSUMER._builtin_tree(graph.owner,
                               "allocated descendant graph owner")
        _strict_fresh_child_domain(domain)
        require(type(graph.ticket) is OWNED._PreForkDescendantEnrollment,
                "exact allocated graph ticket required")
        CONSUMER._builtin_tree(
            graph.ticket.supervisor, "allocated descendant ticket owner")
        graph._validate_frozen("GRAPH_ENROLLED")
        hard_limit = graph.deadline_ns + cleanup_ms * 1000000
        require(threading.current_thread() is graph.owner_thread
                and threading.current_thread() is domain.owner_thread
                and graph._domain_enrollment_started is False
                and graph.domain_enrollment is None
                and graph.started_ns <= now_ns < graph.deadline_ns
                and domain.last_clock_ns <= now_ns
                and domain.request_id == graph.request_id
                and domain.deadline_ns == hard_limit
                and SUP.canonical(domain.owner)
                == SUP.canonical(graph.owner)
                == SUP.canonical(graph.ticket.supervisor),
                "fresh matching allocated descendant domain required")
        object.__setattr__(graph, "_domain_enrollment_started", True)
        enrollment = AllocatedDescendantEnrollment(
            _DOMAIN_ENROLL_KEY, graph, domain, cleanup_ms, now_ns)
        object.__setattr__(graph, "domain_enrollment", enrollment)
        DESC.reserve_bridge_enrollment(
            DESC._BRIDGE_KEY, domain, graph.binding)
        graph._validate("GRAPH_ENROLLED")
        require(graph.domain_enrollment is enrollment
                and domain.bridge_enrollment is graph.binding
                and enrollment.pre_fork_enrollment is graph.ticket,
                "allocated descendant composition changed")
        return enrollment
    except BaseException:
        graph._poison()
        if _builtin_child_domain_namespace(domain):
            DESC.poison_bridge_domain(DESC._BRIDGE_KEY, domain)
        raise


def record_allocated_descendant_fork_once(shared, attempt, lifecycle,
                                          expected_attempt_state):
    """Bind the consumed original ticket to its one exact fork origin."""
    import prelive_abort_fork_split as ABORT
    require(type(shared) is AllocatedDescendantEnrollment
            and type(attempt) is ABORT.AbortForkAttempt
            and type(lifecycle) is dict
            and type(expected_attempt_state) is str,
            "exact allocated descendant fork inputs required")
    if shared.state != "READY":
        shared._poison()
        shared.graph._poison()
        attempt._poison("DESCENDANT_FORK_RECORD_REPLAY")
        raise Refusal("fresh allocated descendant enrollment required")
    object.__setattr__(shared, "state", "FORK_RECORDING")
    try:
        require(type(shared.__dict__) is dict
                and all(type(key) is str for key in shared.__dict__),
                "strict allocated descendant fork containers")
        origin = _strict_allocated_fork_lifecycle(lifecycle)
        graph = shared.graph
        require(threading.current_thread() is shared.owner_thread
                and shared.owner_thread is graph.owner_thread
                and attempt.owner_thread is shared.owner_thread
                and graph.domain_enrollment is shared
                and graph.state == "BOUND" and graph._poisoned is False
                and graph.ticket is shared.pre_fork_enrollment
                and type(shared.pre_fork_enrollment)
                is OWNED._PreForkDescendantEnrollment
                and shared.pre_fork_enrollment.used is True
                and type(origin) is OWNED._ForkOrigin
                and origin.pre_fork_enrollment
                is shared.pre_fork_enrollment
                and lifecycle is attempt.lifecycle
                and lifecycle["state"] == "SPAWNED_UNBOUND"
                and lifecycle["token"] == attempt._lifecycle_token
                and lifecycle["origin"] is attempt._lifecycle_origin
                and lifecycle["owned_child"] == {
                    "pid": attempt.returned_pid, "starttime": None}
                and origin.pid == attempt.returned_pid
                and attempt.enrollment is graph
                and attempt.persisted.enrollment is graph
                and attempt.persisted.state == "CLAIMED"
                and attempt.persisted.bound.consumer is attempt
                and attempt.ticket is shared.pre_fork_enrollment
                and SUP.canonical(lifecycle["supervisor"])
                == shared._owner_bytes,
                "allocated descendant fork origin mismatch")
        _strict_fresh_child_domain(shared.domain, shared.binding)
        object.__setattr__(shared, "fork_attempt", attempt)
        object.__setattr__(shared, "fork_lifecycle", lifecycle)
        object.__setattr__(shared, "fork_origin", origin)
        object.__setattr__(shared, "fork_pid", origin.pid)
        object.__setattr__(shared, "fork_token", lifecycle["token"])
        object.__setattr__(shared, "_fork_supervisor_bytes",
                           SUP.canonical(lifecycle["supervisor"]))
        object.__setattr__(shared, "state", "FORKED")
        shared._validate_forked(attempt, expected_attempt_state)
        return shared
    except BaseException:
        shared._poison()
        shared.graph._poison()
        attempt._poison("DESCENDANT_FORK_RECORD")
        raise


def validate_allocated_descendant_fork(shared, attempt,
                                       expected_attempt_state, reaped=False):
    import prelive_abort_fork_split as ABORT
    require(type(shared) is AllocatedDescendantEnrollment
            and type(reaped) is bool,
            "exact allocated descendant enrollment required")
    try:
        shared._validate_forked(
            attempt, expected_attempt_state,
            expected_lifecycle_state=(
                "OWNED_LEADER_REAPED_ONLY" if reaped else None))
        return shared
    except BaseException:
        shared._poison()
        shared.graph._poison()
        if type(attempt) is ABORT.AbortForkAttempt:
            attempt._poison("DESCENDANT_FORK_VALIDATION")
        raise


def validate_allocated_descendant_attach_phase(
        shared, attempt, expected_domain_state):
    import prelive_abort_fork_split as ABORT
    require(type(shared) is AllocatedDescendantEnrollment
            and type(attempt) is ABORT.AbortForkAttempt
            and type(expected_domain_state) is str,
            "exact allocated attach validation inputs required")
    try:
        shared._validate_forked(
            attempt, "LINUX_CAPTURE_BOUND_UNARMED",
            expected_shared_state="ATTACHING", expected_used=True,
            expected_domain_state=expected_domain_state)
        return shared
    except BaseException:
        shared._poison()
        shared.graph._poison()
        attempt._poison("DESCENDANT_ATTACHMENT_VALIDATION")
        raise


def persist_bound_intent_once(bound, bridge, now_ns):
    if (type(bound) is BoundNoGrantIntentCapability
            and (bound.state != "BOUND" or bound.persistence_started
                 or bound.persistence_bridge is not None
                 or bound.persisted_capability is not None)):
        object.__setattr__(bound, "state", "UNKNOWN")
        bound.enrollment._poison()
        raise Refusal("bound INTENT persistence replay or reentrancy")
    require(type(bound) is BoundNoGrantIntentCapability
            and type(bridge) is INTENT.V2IntentFileBridge
            and threading.current_thread() is bound.owner_thread
            and bound.state == "BOUND" and bound.used is False
            and bound.persistence_started is False
            and bound.persistence_bridge is None
            and bound.persisted_capability is None
            and bridge.state == "READY" and bridge.receipt is None
            and _integer(now_ns) and now_ns >= bound.bind_now_ns
            and now_ns < bound.enrollment.deadline_ns,
            "fresh bound INTENT persistence authority required")
    object.__setattr__(bound, "persistence_started", True)
    object.__setattr__(bound, "persistence_bridge", bridge)
    object.__setattr__(bound, "state", "PERSISTING")
    enrollment = bound.enrollment
    try:
        enrollment._validate("BOUND")
        request_before = _strict_bound_request(bound)
        require(bound.state == "PERSISTING"
                and bound.persistence_started is True
                and bound.persistence_bridge is bridge
                and bound.persisted_capability is None,
                "bound persistence authority changed before write")
        receipt = bridge.persist_intent(bound.intent_bytes)
        request_after = _strict_bound_request(bound)
        frozen_receipt = _strict_persistence_receipt(
            receipt, bound, request_after)
        frozen_receipt_bytes = SUP.canonical(frozen_receipt)
        require(SUP.canonical(request_after) == SUP.canonical(request_before)
                and bridge.state == "SEALED_INTENT"
                and bound.state == "PERSISTING"
                and bound.persistence_started is True
                and bound.persistence_bridge is bridge
                and bound.persisted_capability is None,
                "exact persisted INTENT receipt required")
        enrollment._validate("BOUND")
        final_request = _strict_bound_request(bound)
        final_receipt = _strict_persistence_receipt(
            receipt, bound, final_request)
        CONSUMER._builtin_tree(bridge.receipt,
                               "post-callback bridge INTENT receipt")
        final_bridge_receipt = _strict_persistence_receipt(
            bridge.receipt, bound, final_request)
        require(SUP.canonical(final_request) == SUP.canonical(request_after)
                and SUP.canonical(final_receipt) == frozen_receipt_bytes
                and SUP.canonical(final_bridge_receipt)
                == frozen_receipt_bytes
                and bound.state == "PERSISTING"
                and bound.persistence_bridge is bridge
                and bridge.state == "SEALED_INTENT"
                and enrollment.ticket.used is False,
                "bound persistence authority changed after verification")
        persisted = PersistedNoGrantIntentCapability(
            _PERSISTED_KEY, bound, bridge, frozen_receipt, now_ns)
        object.__setattr__(bound, "persisted_capability", persisted)
        object.__setattr__(bound, "state", "PERSISTED")
        return persisted
    except BaseException:
        object.__setattr__(bound, "state", "UNKNOWN")
        enrollment._poison()
        raise
