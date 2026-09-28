#!/usr/bin/python3
"""Source-only opaque leader-reap to descendant-domain bridge.

This module composes existing injected models.  It has no CLI, real syscall
backend, fork, wait, signal, close, journal or storage operation of its own.
"""

import copy
import errno
import select
import threading
import types

import prelive_owned_child as OWNED
import prelive_capture_settlement as CAPTURE
import prelive_descendant_accounting as DESC
import prelive_allocated_graph_enrollment as GRAPH
import prelive_real_fd_adapter as FDS
import prelive_abort_fork_split as ABORT


_ENROLL_KEY = object()
_BRIDGE_KEY = object()
_ALLOCATED_ATTACH_KEY = object()
_STEP_WATERMARK_KEY = object()
_TERMINAL_EVIDENCE_KEY = object()
_ADAPTER_ENROLL_METHOD = OWNED._CaptureChildAdapter.__dict__["enroll_descendant"]
_ADAPTER_ASSERT_METHOD = (
    OWNED._CaptureChildAdapter.__dict__["assert_current_no_callback"])
_SHARED_FORK_VALIDATE_METHOD = (
    GRAPH.AllocatedDescendantEnrollment.__dict__["_validate_forked"])
_REAPED_CHAIN_VALIDATE_METHOD = (
    ABORT.AbortForkAttempt.__dict__["_validate_linux_reaped_chain"])
_ATTEMPT_SET_METHOD = ABORT.AbortForkAttempt.__dict__["_set"]
_TAKE_SETTLEMENT_CAPABILITY = CAPTURE.take_descendant_settlement_capability
_ADAPTER_TAKE_REAP_METHOD = (
    OWNED._CaptureChildAdapter.__dict__["take_exact_leader_reap"])
_LEADER_REAP_CLAIM_METHOD = DESC._leader_reap_claim
_DOMAIN_ACCEPT_REAP_METHOD = DESC.ChildDomain.__dict__["accept_leader_reap"]
_DOMAIN_ADVANCE_CLOCK_METHOD = DESC.advance_bridge_clock_watermark
_DOMAIN_STEP_METHOD = DESC.ChildDomain.__dict__["step"]
_DOMAIN_CONTINUITY_METHOD = DESC.ChildDomain.__dict__["_continuity"]
_DOMAIN_CLOCK_METHOD = DESC.ChildDomain.__dict__["_clock"]
_DOMAIN_RECEIPT_METHOD = DESC.ChildDomain.__dict__["_receipt"]
_DOMAIN_UNKNOWN_METHOD = DESC.ChildDomain.__dict__["_unknown"]
_DOMAIN_LOCAL_OWNER_METHOD = DESC.ChildDomain.__dict__["_local_owner"]
_PERSISTED_VALIDATE_METHOD = (
    GRAPH.PersistedNoGrantIntentCapability.__dict__["_validate_frozen"])
_FD_GRAPH_VALIDATE_METHOD = FDS.RealFDHandle.__dict__["_validate_stored_graph"]
_FD_ROLE_VALIDATE_METHOD = FDS.RealFDHandle.__dict__["_validate_stored_role"]
_FD_AUTHORITY_METHOD = FDS.RealFDHandle.__dict__["_authority_record"]


class Refusal(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise Refusal(message)


def _adapter_surface_is_pinned(adapter, name, pinned):
    namespace = adapter.__dict__
    class_namespace = type(adapter).__dict__
    return (type(namespace) is dict
            and all(type(key) is str for key in namespace)
            and name not in namespace
            and class_namespace.get(name) is pinned)


def _strict_allocation_authority(handle):
    authority = handle._allocation_authority
    require(type(authority) is tuple
            and len(authority) == len(FDS._ALL_ROLES),
            "allocated FD authority container changed")
    roles = []
    for entry in authority:
        require(type(entry) is tuple and len(entry) == 2
                and type(entry[0]) is str
                and type(entry[1]) is tuple
                and len(entry[1]) == len(FDS._IDENTITY_KEYS)
                and all(type(value) is int for value in entry[1]),
                "allocated FD authority entry changed")
        roles.append(entry[0])
    require(len(set(roles)) == len(roles)
            and set(roles) == set(FDS._ALL_ROLES),
            "allocated FD authority roles changed")
    return authority


def _strict_leader_identity(value, label):
    require(type(value) is dict
            and all(type(key) is str for key in value)
            and set(value) == {"pid", "starttime", "boot_id"}
            and type(value["pid"]) is int and value["pid"] > 0
            and type(value["starttime"]) is int
            and value["starttime"] > 0
            and type(value["boot_id"]) is str,
            "strict " + label + " identity")
    GRAPH.CONSUMER._builtin_tree(value, label + " identity")
    return value


class Enrollment:
    def __init__(self, key, domain):
        require(key is _ENROLL_KEY, "enrollment construction is private")
        self.domain = domain
        self.binding = object()
        self.pre_fork_enrollment = OWNED.prepare_descendant_fork_enrollment(
            OWNED._PREFORK_KEY, self.binding, domain.owner)
        self.owner_thread = threading.current_thread()
        self.used = False

    def __copy__(self):
        raise Refusal("domain enrollment is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("domain enrollment is noncopyable")


class DescendantBridge:
    def __init__(self, key, enrollment, owned_handle, adapter, capture,
                 domain_origin):
        require(key is _BRIDGE_KEY, "bridge construction is private")
        self.enrollment = enrollment
        self.domain = enrollment.domain
        self.owned_handle = owned_handle
        self.adapter = adapter
        self.capture = capture
        self.domain_origin = domain_origin
        self.owner_thread = threading.current_thread()
        self.state = "ATTACHED"
        self.settlement = None
        self.settlement_capability = None
        self.leader_capability = None
        self.progress = None
        self.error = None
        self.primary_failure = None
        self.primary_completion = None
        self.settlement_evidence = None

    def __copy__(self):
        raise Refusal("descendant bridge is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("descendant bridge is noncopyable")

    def _poison(self, reason=None):
        self.error = reason
        self.state = "UNKNOWN"
        DESC.poison_bridge_domain(DESC._BRIDGE_KEY, self.domain)

    def _enter(self, allowed):
        if (threading.current_thread() is not self.owner_thread
                or self.state not in allowed):
            self._poison()
            raise Refusal("foreign, reentrant or out-of-phase bridge mutation")
        self.state = "ADVANCING"

    def _validate_transferred_chain(self):
        cap = self.leader_capability
        settled = self.settlement_capability
        valid = (cap is not None and settled is not None
                 and cap.used is True and settled.used is True
                 and cap.binding is self.enrollment.binding
                 and cap.fork_origin
                 is self.owned_handle.lifecycle["origin"]
                 and self.owned_handle.lifecycle["origin"].pre_fork_enrollment
                 is self.enrollment.pre_fork_enrollment
                 and self.enrollment.pre_fork_enrollment.binding
                 is self.enrollment.binding
                 and self.domain.bridge_enrollment is self.enrollment.binding
                 and settled.capture_handle is self.capture
                 and settled.child_adapter is self.adapter
                 and self.settlement.capture_handle is self.capture
                 and self.settlement.child_adapter is self.adapter
                 and self.settlement.probe_used is True
                 and self.settlement.descendant_capability is settled
                 and settled.primary_failure == self.primary_failure
                 and settled.primary_completion == self.primary_completion
                 and self.settlement.receipt["primary_failure"]
                 == self.primary_failure
                 and self.settlement.receipt["primary_completion"]
                 == self.primary_completion
                 and self.settlement.receipt["settlement_kind"]
                 == settled.settlement_kind)
        if not valid:
            self._poison("transferred capability chain changed")
            raise Refusal("transferred capability chain changed")
        if settled.settlement_kind == "NORMAL_COMPLETION":
            CAPTURE.validate_transferred_normal_completion(
                self.settlement, self.capture, self.adapter)
        OWNED.validate_transferred_leader_reap(
            self.adapter, cap, self.enrollment.binding, self.capture)

    def snapshot(self):
        outcome = {"ATTACHED": "LEADER_SETTLEMENT_PENDING",
                   "DRAINING": "CHILDREN_PENDING",
                   "DOMAIN_DRAINED": "MODEL_LEADER_REAP_AND_CHILD_DOMAIN_DRAINED",
                   "UNKNOWN": "UNKNOWN", "ADVANCING": "UNKNOWN_BUSY"}.get(
                       self.state, "UNKNOWN")
        return {"schema": 1, "classification": outcome,
                 "primary_failure": copy.deepcopy(self.primary_failure),
                 "primary_completion": copy.deepcopy(self.primary_completion),
                "settlement_evidence": copy.deepcopy(self.settlement_evidence),
                "leader_reap_confirmed": self.leader_capability is not None,
                "descendant_progress": copy.deepcopy(self.progress),
                "error": self.error,
                "cleanup_deadline_ns": self.domain.deadline_ns,
                "runtime_authorized": False, "storage_authorized": False,
                "postcondition_verified": False}


class AllocatedOwnedDomainAttachment:
    """Opaque hard-limit-only attachment; deliberately not drainable."""
    def __init__(self, key, shared, attempt, owned, adapter, capture,
                 domain_origin, allocation_authority):
        require(key is _ALLOCATED_ATTACH_KEY,
                "allocated attachment construction is private")
        self.shared = shared
        self.attempt = attempt
        self.domain = shared.domain
        self.owned_handle = owned
        self.adapter = adapter
        self.capture = capture
        self.domain_origin = domain_origin
        self.allocation_authority = allocation_authority
        self.binding = shared.binding
        self.pre_fork_enrollment = shared.pre_fork_enrollment
        self.owner_thread = threading.current_thread()
        self.state = "ATTACHED_HARD_LIMIT_ONLY"
        self.cleanup_deadline_bound = False
        self.used = False
        self._sealed = True

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise Refusal("allocated attachment is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        raise Refusal("allocated attachment is immutable")

    def __copy__(self):
        raise Refusal("allocated attachment is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("allocated attachment is noncopyable")

    def snapshot(self):
        return {"schema": 1,
                "classification": "ATTACHED_HARD_LIMIT_ONLY",
                "cleanup_hard_limit_ns": self.shared.cleanup_hard_limit_ns,
                "cleanup_deadline_bound": False,
                "descendant_drain_authorized": False,
                "runtime_authorized": False,
                "storage_authorized": False,
                "postcondition_verified": False}


class AllocatedDeadlineBoundAttachment:
    """Opaque deadline-bound receipt; still grants no drain authority."""
    def __init__(self, key, source, settlement, capability, deadline_ns,
                 hard_limit_ns, bind_now_ns, receipt_bytes):
        require(key is _ALLOCATED_ATTACH_KEY,
                "deadline binding construction is private")
        self.source_attachment = source
        self.shared = source.shared
        self.attempt = source.attempt
        self.domain = source.domain
        self.settlement = settlement
        self.descendant_capability = capability
        self.deadline_ns = deadline_ns
        self.hard_limit_ns = hard_limit_ns
        self.bind_now_ns = bind_now_ns
        self.settlement_receipt_bytes = receipt_bytes
        self.owner_thread = threading.current_thread()
        self.state = "DEADLINE_BOUND_NON_DRAINABLE"
        self.used = False
        self._sealed = True

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise Refusal("deadline-bound attachment is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        raise Refusal("deadline-bound attachment is immutable")

    def __copy__(self):
        raise Refusal("deadline-bound attachment is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("deadline-bound attachment is noncopyable")

    def snapshot(self):
        return {"schema": 1,
                "classification": "DEADLINE_BOUND_NON_DRAINABLE",
                "cleanup_deadline_ns": self.deadline_ns,
                "cleanup_hard_limit_ns": self.hard_limit_ns,
                "descendant_drain_authorized": False,
                "runtime_authorized": False,
                "storage_authorized": False,
                "postcondition_verified": False}


class AllocatedDescendantDrainBridge:
    """Mutable transfer record; usable only after exact drain-ready publish."""
    def __init__(self, key, source):
        require(key is _ALLOCATED_ATTACH_KEY,
                "allocated drain bridge construction is private")
        self.source = source
        self.attachment = source.source_attachment
        self.shared = source.shared
        self.attempt = source.attempt
        self.domain = source.domain
        self.settlement = source.settlement
        self.settlement_capability = None
        self.leader_capability = None
        self.leader_claim = None
        self.owner_thread = threading.current_thread()
        self.state = "TRANSFERRING"
        self.settlement_transfer_attempted = False
        self.settlement_transfer_confirmed = False
        self.leader_transfer_attempted = False
        self.leader_transfer_confirmed = False
        self.domain_accept_attempted = False
        self.domain_accept_confirmed = False
        self.ready_watermark_ns = None
        self.advance_attempted = False
        self.advance_result = None
        self.error = None

    def __copy__(self):
        raise Refusal("allocated drain bridge is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("allocated drain bridge is noncopyable")

    def _poison(self, reason):
        if self.state != "UNKNOWN":
            self.error = reason
            self.state = "UNKNOWN"

    def snapshot(self):
        return {"schema": 1, "classification": self.state,
                "settlement_transfer_attempted":
                    self.settlement_transfer_attempted,
                "settlement_transfer_confirmed":
                    self.settlement_transfer_confirmed,
                "leader_transfer_attempted": self.leader_transfer_attempted,
                "leader_transfer_confirmed": self.leader_transfer_confirmed,
                "domain_accept_attempted": self.domain_accept_attempted,
                "domain_accept_confirmed": self.domain_accept_confirmed,
                "ready_watermark_ns": self.ready_watermark_ns,
                "advance_attempted": self.advance_attempted,
                "cleanup_deadline_ns": self.source.deadline_ns,
                "descendant_drain_authorized":
                    self.state == "ALLOCATED_DRAIN_READY",
                "runtime_authorized": False,
                "storage_authorized": False,
                "postcondition_verified": False,
                "error": self.error}


def _validate_allocated_drain_chain(source, bridge=None, transferred=False,
                                    accepted=False, ready=False, now_ns=None,
                                    _nested_proof=True):
    """Callback-free validation for the one-way allocated drain handoff."""
    require(type(source) is AllocatedDeadlineBoundAttachment
            and type(transferred) is bool and type(accepted) is bool
            and type(ready) is bool and (not ready or accepted)
            and type(_nested_proof) is bool
            and (now_ns is None or type(now_ns) is int),
            "exact allocated drain validation inputs required")
    attachment = source.source_attachment
    shared = source.shared
    attempt = source.attempt
    domain = source.domain
    settlement = source.settlement
    descendant = source.descendant_capability
    adapter = attachment.adapter
    owned = attachment.owned_handle
    require(type(source.__dict__) is dict
            and all(type(key) is str for key in source.__dict__)
            and set(source.__dict__) == {
                "source_attachment", "shared", "attempt", "domain",
                "settlement", "descendant_capability", "deadline_ns",
                "hard_limit_ns", "bind_now_ns",
                "settlement_receipt_bytes", "owner_thread", "state",
                "used", "_sealed"}
            and type(source.deadline_ns) is int
            and type(source.hard_limit_ns) is int
            and type(source.bind_now_ns) is int
            and type(source.settlement_receipt_bytes) is bytes
            and source.deadline_ns <= source.hard_limit_ns
            and source.bind_now_ns < source.deadline_ns
            and source.owner_thread is threading.current_thread()
            and source.owner_thread is attempt.owner_thread
            and type(source.state) is str
            and source.state == "DEADLINE_BOUND_NON_DRAINABLE"
            and type(source.used) is bool
            and source.used is (bridge is not None)
            and type(source._sealed) is bool and source._sealed is True,
            "allocated drain successor changed")
    require(type(settlement.__dict__) is dict
            and all(type(key) is str for key in settlement.__dict__)
            and type(settlement.receipt) is dict
            and all(type(key) is str for key in settlement.receipt)
            and type(settlement.deadline_ns) is int
            and type(settlement.leader_reaped) is bool
            and settlement.leader_reaped is True
            and type(settlement.probe_used) is bool
            and type(settlement.owner) is dict
            and all(type(key) is str for key in settlement.owner)
            and type(descendant.__dict__) is dict
            and all(type(key) is str for key in descendant.__dict__)
            and type(descendant.primary_completion) is dict
            and all(type(key) is str
                    for key in descendant.primary_completion)
            and type(descendant.finished_ns) is int
            and type(descendant.deadline_ns) is int
            and type(domain.deadline_ns) is int
            and type(domain.last_clock_ns) is int
            and type(domain.request_id) is str
            and type(domain.domain_id) is str
            and type(domain.state) is str
            and type(domain.launcher_claimed) is bool
            and domain.launcher_claimed is True
            and type(domain.records) is list
            and domain.records == []
            and domain.pending is None
            and domain.terminal_echild is None
            and domain.leader is (None if not accepted else domain.leader)
            and type(domain.expected_leader) is dict
            and all(type(key) is str for key in domain.expected_leader)
            and set(domain.expected_leader) == {
                "pid", "starttime", "boot_id"}
            and type(owned.child) is dict
            and all(type(key) is str for key in owned.child)
            and set(owned.child) == {"pid", "starttime", "boot_id"},
            "strict allocated drain containers")
    GRAPH.CONSUMER._builtin_tree(
        settlement.receipt, "allocated drain settlement receipt")
    GRAPH.CONSUMER._builtin_tree(
        settlement.owner, "allocated drain settlement owner")
    GRAPH.CONSUMER._builtin_tree(
        descendant.owner, "allocated drain descendant owner")
    GRAPH.CONSUMER._builtin_tree(
        domain.owner, "allocated drain domain owner")
    GRAPH.CONSUMER._builtin_tree(
        descendant.primary_completion,
        "allocated drain descendant completion")
    GRAPH.CONSUMER._builtin_tree(
        domain.expected_leader, "allocated drain expected leader")
    GRAPH.CONSUMER._builtin_tree(
        owned.child, "allocated drain owned child")
    require(type(attachment) is AllocatedOwnedDomainAttachment
            and source.shared is attachment.shared
            and source.attempt is attachment.attempt
            and source.domain is attachment.domain
            and source.deadline_ns == attempt.cleanup_deadline_ns
            and source.hard_limit_ns == shared.cleanup_hard_limit_ns
            and attempt.descendant_deadline_binding is source
            and attempt.descendant_deadline_binding_started is True
            and attempt.state == "LINUX_LEADER_REAPED_UNQUALIFIED"
            and attempt.poisoned is False
            and attempt.settlement_handle is settlement
            and attempt._settlement_receipt_bytes
            == source.settlement_receipt_bytes
            and settlement is source.settlement
            and descendant is settlement.descendant_capability
            and descendant is source.descendant_capability
            and ABORT.SUP.canonical(settlement.receipt)
            == source.settlement_receipt_bytes,
            "allocated drain settlement chain changed")
    require(type(attachment.__dict__) is dict
            and all(type(key) is str for key in attachment.__dict__)
            and attachment.shared is shared
            and attachment.attempt is attempt
            and attachment.domain is domain
            and attachment.owned_handle is owned
            and attachment.capture is attempt.capture_handle
            and attachment.binding is shared.binding
            and attachment.pre_fork_enrollment
            is shared.pre_fork_enrollment
            and attachment.owner_thread is source.owner_thread
            and attachment.state == "ATTACHED_HARD_LIMIT_ONLY"
            and attachment.used is True
            and attachment._sealed is True
            and _strict_allocation_authority(attempt.handle)
            is attachment.allocation_authority,
            "allocated drain attachment chain changed")
    require(type(attempt.persisted)
            is GRAPH.PersistedNoGrantIntentCapability
            and type(attempt.persisted.__dict__) is dict
            and all(type(key) is str
                    for key in attempt.persisted.__dict__)
            and type(attempt.persisted).__dict__.get("_validate_frozen")
            is _PERSISTED_VALIDATE_METHOD
            and "_validate_frozen" not in attempt.persisted.__dict__
            and type(attempt.handle) is FDS.RealFDHandle
            and type(attempt.handle.__dict__) is dict
            and all(type(key) is str for key in attempt.handle.__dict__)
            and type(attempt.handle).__dict__.get("_validate_stored_graph")
            is _FD_GRAPH_VALIDATE_METHOD
            and "_validate_stored_graph" not in attempt.handle.__dict__,
            "allocated drain nested validator surfaces changed")
    require(type(attempt.handle).__dict__.get("_validate_stored_role")
            is _FD_ROLE_VALIDATE_METHOD
            and "_validate_stored_role" not in attempt.handle.__dict__
            and type(attempt.handle).__dict__.get("_authority_record")
            is _FD_AUTHORITY_METHOD
            and "_authority_record" not in attempt.handle.__dict__,
            "allocated drain FD validator chain changed")
    require(ABORT.SUP.canonical(settlement.owner)
            == attempt._owner_bytes
            and ABORT.SUP.canonical(descendant.owner)
            == attempt._owner_bytes
            and ABORT.SUP.canonical(domain.owner)
            == attempt._owner_bytes
            and domain.request_id == shared.graph.request_id
            and domain.domain_id == shared.domain_id
            and domain._seal is shared.domain_seal
            and settlement.deadline_ns == source.deadline_ns
            and descendant.finished_ns == settlement.receipt["finished_ns"]
            and descendant.deadline_ns
            == settlement.receipt["cleanup_deadline_ns"],
            "allocated drain frozen provenance changed")
    if transferred and _nested_proof:
        require(type(attempt).__dict__.get("_validate_linux_reaped_chain")
                is _REAPED_CHAIN_VALIDATE_METHOD
                and "_validate_linux_reaped_chain" not in attempt.__dict__,
                "allocated drain reaped validator surface changed")
        _REAPED_CHAIN_VALIDATE_METHOD(
            attempt, "LINUX_LEADER_REAPED_UNQUALIFIED",
            terminal_claimed=True)
        return _validate_allocated_drain_chain(
            source, bridge, transferred=transferred, accepted=accepted,
            ready=ready, now_ns=now_ns, _nested_proof=False)
    require(type(domain) is DESC.ChildDomain
            and domain is shared.domain
            and domain.owner_thread is source.owner_thread
            and domain._seal is shared.domain_seal
            and domain.bridge_enrollment is shared.binding
            and domain.expected_leader == owned.child
            and domain.deadline_ns == source.deadline_ns
            and type(domain.last_clock_ns) is int
            and domain.last_clock_ns == (
                now_ns if transferred and now_ns is not None
                else source.bind_now_ns)
            and domain.state == ("DRAINING" if accepted
                                 else "LAUNCHER_CLAIMED")
            and domain.records == [] and domain.pending is None
            and domain.terminal_echild is None,
            "allocated drain domain changed")
    require(type(adapter) is OWNED._CaptureChildAdapter
            and type(adapter.__dict__) is dict
            and all(type(key) is str for key in adapter.__dict__)
            and type(adapter.state) is str
            and adapter is attempt.capture_adapter
            and adapter.handle is owned
            and adapter.owner_thread is source.owner_thread
            and adapter.descendant_binding is shared.binding
            and adapter.descendant_consumer is attachment.capture
            and adapter.capture_consumer is attachment.capture
            and adapter.cached is owned.observation
            and type(adapter.reap_capability)
            is OWNED._ExactLeaderReapCapability,
            "allocated drain adapter changed")
    require(type(owned.__dict__) is dict
            and all(type(key) is str for key in owned.__dict__)
            and type(owned.state) is str
            and type(owned.lifecycle) is dict
            and all(type(key) is str for key in owned.lifecycle)
            and set(owned.lifecycle) == {
                "schema", "state", "token", "supervisor",
                "owned_child", "origin"}
            and type(owned.lifecycle["schema"]) is int
            and type(owned.lifecycle["state"]) is str
            and type(owned.lifecycle["token"]) is str
            and type(owned.lifecycle["supervisor"]) is dict
            and all(type(key) is str
                    for key in owned.lifecycle["supervisor"])
            and type(owned.lifecycle["owned_child"]) is dict
            and all(type(key) is str
                    for key in owned.lifecycle["owned_child"])
            and type(owned.lifecycle["origin"]) is OWNED._ForkOrigin,
            "strict allocated drain owned lifecycle")
    leader_capability = adapter.reap_capability
    origin = attachment.domain_origin
    require(type(leader_capability.__dict__) is dict
            and all(type(key) is str for key in leader_capability.__dict__)
            and set(leader_capability.__dict__) == {
                "binding", "adapter", "handle", "fork_origin", "receipt",
                "owner_thread", "leader", "used"}
            and type(origin) is DESC._LauncherOrigin
            and type(origin.__dict__) is dict
            and all(type(key) is str for key in origin.__dict__)
            and set(origin.__dict__) == {"seal", "leader", "used"},
            "strict allocated drain leader containers")
    _strict_leader_identity(
        leader_capability.leader, "allocated drain capability leader")
    _strict_leader_identity(origin.leader,
                            "allocated drain launcher origin leader")
    require(leader_capability.binding is shared.binding
            and leader_capability.adapter is adapter
            and leader_capability.handle is owned
            and leader_capability.fork_origin is owned.lifecycle["origin"]
            and leader_capability.receipt is adapter.cached
            and leader_capability.owner_thread is source.owner_thread
            and leader_capability.leader == domain.expected_leader
            and leader_capability.leader == owned.child
            and origin.seal is domain._seal
            and type(origin.used) is bool and origin.used is True
            and origin.leader == owned.child
            and type(leader_capability.used) is bool
            and leader_capability.used is transferred
            and adapter.state == ("DESCENDANT_HANDOFF" if transferred
                                  else "REAPED")
            and owned.state == "REAPED"
            and owned.lifecycle["state"] == "OWNED_LEADER_REAPED_ONLY",
            "allocated drain leader capability changed")
    require(type(descendant) is CAPTURE._DescendantSettlementCapability
            and descendant.capture_handle is attachment.capture
            and descendant.child_adapter is adapter
            and descendant.owner_thread is source.owner_thread
            and descendant.deadline_ns == source.deadline_ns
            and type(descendant.finished_ns) is int
            and descendant.finished_ns <= source.bind_now_ns
            and type(descendant.used) is bool
            and descendant.used is transferred
            and type(settlement.probe_used) is bool
            and settlement.probe_used is transferred,
            "allocated drain settlement capability changed")
    if bridge is not None:
        require(type(bridge) is AllocatedDescendantDrainBridge
                and bridge.source is source
                and bridge.attachment is attachment
                and bridge.shared is shared and bridge.attempt is attempt
                and bridge.domain is domain and bridge.settlement is settlement
                and bridge.owner_thread is source.owner_thread
                and attempt.descendant_drain_transfer_started is True
                and attempt.descendant_drain_bridge is bridge
                and type(bridge.state) is str
                and bridge.state == (
                    "ALLOCATED_DRAIN_READY" if ready else "TRANSFERRING")
                and bridge.error is None
                and type(bridge.advance_attempted) is bool
                and bridge.advance_attempted is False
                and bridge.advance_result is None
                and bridge.ready_watermark_ns == (
                    now_ns if ready else None)
                and shared.state == (
                    "ALLOCATED_DRAIN_READY" if ready
                    else "DRAIN_TRANSFERRING")
                and bridge.settlement_transfer_confirmed is transferred
                and bridge.leader_transfer_confirmed is transferred
                and bridge.domain_accept_confirmed is accepted,
                "allocated drain bridge publication changed")
        if transferred:
            require(bridge.settlement_capability is descendant
                    and bridge.leader_capability is leader_capability,
                    "allocated drain transferred references changed")
        if accepted:
            require(type(bridge.leader_claim) is DESC.LeaderReapClaim
                    and bridge.leader_claim.used is True
                    and bridge.leader_claim.leader == domain.leader
                    and domain.leader == domain.expected_leader,
                    "allocated drain accepted leader changed")
    return True


def transfer_allocated_descendant_drain(source, settlement, clock):
    """Consume exact post-reap authorities; perform no descendant step."""
    require(type(source) is AllocatedDeadlineBoundAttachment
            and type(settlement) is CAPTURE.SettlementHandle,
            "exact allocated drain transfer inputs required")
    shared = source.shared
    attempt = source.attempt
    domain = source.domain
    adapter = source.source_attachment.adapter
    bridge = None
    try:
        require(settlement is source.settlement
                and type(shared.state) is str
                and attempt.descendant_drain_transfer_started is False
                and type(attempt.descendant_drain_transfer_started) is bool
                and attempt.descendant_drain_bridge is None
                and shared.state == "DEADLINE_BOUND_NON_DRAINABLE",
                "fresh allocated drain transfer required")
        _validate_allocated_drain_chain(source)
        frozen_finished_ns = source.descendant_capability.finished_ns
        frozen_bind_now_ns = source.bind_now_ns
        frozen_deadline_ns = source.deadline_ns
        frozen_receipt_bytes = source.settlement_receipt_bytes
        require(type(frozen_finished_ns) is int
                and type(frozen_bind_now_ns) is int
                and type(frozen_deadline_ns) is int
                and type(frozen_receipt_bytes) is bytes,
                "strict allocated drain frozen scalars")
        require(_adapter_surface_is_pinned(
                    adapter, "take_exact_leader_reap",
                    _ADAPTER_TAKE_REAP_METHOD),
                "allocated leader handoff surface changed")
        bridge = AllocatedDescendantDrainBridge(
            _ALLOCATED_ATTACH_KEY, source)
        object.__setattr__(source, "used", True)
        object.__setattr__(shared, "state", "DRAIN_TRANSFERRING")
        _ATTEMPT_SET_METHOD(
            attempt, "descendant_drain_transfer_started", True)
        _ATTEMPT_SET_METHOD(attempt, "descendant_drain_bridge", bridge)
        _validate_allocated_drain_chain(source, bridge)

        bridge.settlement_transfer_attempted = True
        claimed_settlement = _TAKE_SETTLEMENT_CAPABILITY(
            settlement, source.source_attachment.capture, adapter)
        bridge.settlement_capability = claimed_settlement
        bridge.settlement_transfer_confirmed = True
        require(claimed_settlement is source.descendant_capability,
                "allocated settlement capability changed during transfer")

        bridge.leader_transfer_attempted = True
        leader_capability = _ADAPTER_TAKE_REAP_METHOD(
            adapter, OWNED._DESCENDANT_KEY, shared.binding,
            source.source_attachment.capture)
        bridge.leader_capability = leader_capability
        require(leader_capability is adapter.reap_capability
                and type(leader_capability)
                is OWNED._ExactLeaderReapCapability
                and leader_capability.used is False,
                "allocated leader capability changed during transfer")
        leader_capability.used = True
        bridge.leader_transfer_confirmed = True
        _validate_allocated_drain_chain(
            source, bridge, transferred=True)

        now = clock.monotonic_ns()
        require(type(now) is int,
                "strict allocated drain transfer clock")
        _validate_allocated_drain_chain(
            source, bridge, transferred=True)
        require(source.descendant_capability.finished_ns
                == frozen_finished_ns
                and source.bind_now_ns == frozen_bind_now_ns
                and source.deadline_ns == frozen_deadline_ns
                and source.settlement_receipt_bytes
                == frozen_receipt_bytes
                and now >= frozen_bind_now_ns
                and now >= frozen_finished_ns
                and now >= domain.last_clock_ns
                and now < frozen_deadline_ns,
                "allocated drain frozen evidence changed after clock")
        _DOMAIN_ADVANCE_CLOCK_METHOD(DESC._BRIDGE_KEY, domain, now)
        _validate_allocated_drain_chain(
            source, bridge, transferred=True, now_ns=now)

        claim = _LEADER_REAP_CLAIM_METHOD(
            DESC._LEADER_KEY, domain,
            source.source_attachment.domain_origin)
        require(type(claim) is DESC.LeaderReapClaim
                and claim.used is False
                and claim.leader == leader_capability.leader,
                "allocated drain leader claim changed")
        bridge.leader_claim = claim
        bridge.domain_accept_attempted = True
        _DOMAIN_ACCEPT_REAP_METHOD(domain, claim)
        bridge.domain_accept_confirmed = True
        _validate_allocated_drain_chain(
            source, bridge, transferred=True, accepted=True, now_ns=now)
        bridge.state = "ALLOCATED_DRAIN_READY"
        bridge.ready_watermark_ns = now
        object.__setattr__(shared, "state", "ALLOCATED_DRAIN_READY")
        _validate_allocated_drain_chain(
            source, bridge, transferred=True, accepted=True, ready=True,
            now_ns=now)
        return bridge
    except BaseException as exc:
        if type(bridge) is AllocatedDescendantDrainBridge:
            bridge._poison(type(exc).__name__)
        if type(shared) is GRAPH.AllocatedDescendantEnrollment:
            shared._poison()
            shared.graph._poison()
        if type(adapter) is OWNED._CaptureChildAdapter:
            adapter._poison("UNKNOWN_ALLOCATED_DESCENDANT_DRAIN_TRANSFER")
        if type(attempt) is ABORT.AbortForkAttempt:
            attempt._poison("ALLOCATED_DESCENDANT_DRAIN_TRANSFER")
        raise


class _AllocatedStepWatermark:
    def __init__(self, key, value):
        require(key is _STEP_WATERMARK_KEY and type(value) is int,
                "private allocated step watermark")
        self.value = value
        self._sealed = True

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise Refusal("allocated step watermark is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        raise Refusal("allocated step watermark is immutable")

    def __copy__(self):
        raise Refusal("allocated step watermark is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("allocated step watermark is noncopyable")

    def advance(self, key, value, deadline_ns):
        require(key is _STEP_WATERMARK_KEY
                and type(value) is int and type(deadline_ns) is int
                and self.value <= value < deadline_ns,
                "allocated step watermark advance")
        object.__setattr__(self, "value", value)


_STEP_WATERMARK_ADVANCE_METHOD = _AllocatedStepWatermark.__dict__["advance"]


class _AllocatedStepAuthority:
    """Immutable pre-step authority; effect evidence lives elsewhere."""
    def __init__(self, key, bridge):
        require(key is _ALLOCATED_ATTACH_KEY,
                "allocated step authority construction is private")
        self.deadline_ns = bridge.source.deadline_ns
        self.initial_watermark_ns = bridge.ready_watermark_ns
        self.initial_sequence = bridge.domain.sequence
        self.request_id = bridge.domain.request_id
        self.domain_id = bridge.domain.domain_id
        self.owned_pidfd = bridge.attachment.owned_handle.pidfd
        self.owner_bytes = bridge.attempt._owner_bytes
        self.settlement_receipt_bytes = bridge.source.settlement_receipt_bytes
        self.allocation_authority = bridge.attachment.allocation_authority
        self.fd_owned = frozenset(bridge.attempt.handle.owned)
        self.fd_lost = frozenset(bridge.attempt.handle.lost)
        self.fd_attempted_closes = frozenset(
            bridge.attempt.handle.attempted_closes)
        self.fd_attempted_writes = frozenset(
            bridge.attempt.handle.attempted_writes)
        self.capture_origin = bridge.attempt._capture_origin
        self.capture_poller = bridge.attempt._capture_poller
        self.capture_stream_refs = tuple(
            (role, bridge.attempt._capture_stream_refs[role])
            for role in ("stdout", "stderr"))
        self.lifecycle = bridge.attachment.owned_handle.lifecycle
        self.lifecycle_origin = self.lifecycle["origin"]
        self.pre_fork_enrollment = bridge.shared.pre_fork_enrollment
        self.monitor_result_bytes = bridge.attempt._monitor_result_bytes
        poller = bridge.attempt._capture_poller
        require(type(poller) is select.epoll and poller.closed is False,
                "exact allocated step epoll")
        poller_fd = poller.fileno()
        require(type(poller_fd) is int and poller_fd > 2,
                "strict allocated step epoll fd")
        self.epoll_fd = poller_fd
        self.preserved_fds = frozenset({poller_fd} | {
            values[FDS._IDENTITY_KEYS.index("fd")]
            for _role, values in self.allocation_authority})
        self._sealed = True

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise Refusal("allocated step authority is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        raise Refusal("allocated step authority is immutable")

    def __copy__(self):
        raise Refusal("allocated step authority is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("allocated step authority is noncopyable")


class AllocatedDescendantStepResult:
    """One-shot, nonqualified evidence from one modeled domain step."""
    def __init__(self, key, bridge):
        require(key is _ALLOCATED_ATTACH_KEY,
                "allocated step result construction is private")
        self.bridge = bridge
        self.source = bridge.source
        self.shared = bridge.shared
        self.attempt = bridge.attempt
        self.domain = bridge.domain
        self.owner_thread = threading.current_thread()
        self.authority = _AllocatedStepAuthority(
            _ALLOCATED_ATTACH_KEY, bridge)
        self.watermark = _AllocatedStepWatermark(
            _STEP_WATERMARK_KEY, self.authority.initial_watermark_ns)
        self.state = "ADVANCING"
        self.receipt = None
        self.receipt_bytes = None
        self.discovery_attempted = False
        self.echild_observed = False
        self.identity_attempted = False
        self.pidfd_open_attempted = False
        self.adopted_pidfd = None
        self.pidfd_open_returned = False
        self.pidfd_observe_attempted = False
        self.pidfd_observe_result = None
        self.pidfd_reap_attempted = False
        self.pidfd_reap_result = None
        self.pidfd_close_attempted = False
        self.pidfd_close_returned = False
        self.pidfd_close_confirmed = False
        self.pidfd_close_unknown = False
        self.terminal_evidence = None
        self.error = None
        self._fixed = True

    def __setattr__(self, name, value):
        if (getattr(self, "_fixed", False)
                and name in {"bridge", "source", "shared", "attempt",
                             "domain", "owner_thread", "authority",
                             "watermark", "terminal_evidence", "_fixed"}):
            raise Refusal("allocated step authority backlink is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        if name in {"bridge", "source", "shared", "attempt", "domain",
                    "owner_thread", "authority", "watermark",
                    "terminal_evidence", "_fixed"}:
            raise Refusal("allocated step authority backlink is immutable")
        object.__delattr__(self, name)

    def __copy__(self):
        raise Refusal("allocated step result is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("allocated step result is noncopyable")

    def _poison(self, reason):
        if self.state != "UNKNOWN":
            self.error = reason
            self.state = "UNKNOWN"

    def snapshot(self):
        return {"schema": 1, "classification": self.state,
                "domain_outcome": (None if self.receipt is None
                                   else self.receipt.get("outcome")),
                "discovery_attempted": self.discovery_attempted,
                "echild_observed": self.echild_observed,
                "identity_attempted": self.identity_attempted,
                "pidfd_open_attempted": self.pidfd_open_attempted,
                "adopted_pidfd": self.adopted_pidfd,
                "pidfd_open_returned": self.pidfd_open_returned,
                "pidfd_observe_attempted": self.pidfd_observe_attempted,
                "pidfd_observe_result": copy.deepcopy(
                    self.pidfd_observe_result),
                "pidfd_reap_attempted": self.pidfd_reap_attempted,
                "pidfd_reap_result": copy.deepcopy(self.pidfd_reap_result),
                "pidfd_close_attempted": self.pidfd_close_attempted,
                "pidfd_close_returned": self.pidfd_close_returned,
                "pidfd_close_confirmed": self.pidfd_close_confirmed,
                "pidfd_close_unknown": self.pidfd_close_unknown,
                "descendants_qualified": False,
                "resources_closed": False,
                "runtime_authorized": False,
                "storage_authorized": False,
                "postcondition_verified": False,
                "error": self.error}


class AllocatedTerminalDomainEvidence:
    """Opaque original ECHILD seal; not release or live-cleanup authority."""
    __slots__ = ("_result", "_bridge", "_source", "_shared", "_attempt",
                 "_domain", "_owner_thread", "_receipt_bytes",
                 "_terminal_bytes", "_deadline_ns", "_watermark_ns",
                 "_allocation_authority", "_owned_pidfd", "_consumed",
                 "_consumer", "_sealed")

    def __init__(self, key, result):
        require(key is _TERMINAL_EVIDENCE_KEY,
                "terminal evidence construction is private")
        terminal = result.domain.terminal_echild
        GRAPH.CONSUMER._builtin_tree(
            terminal, "allocated terminal ECHILD evidence")
        object.__setattr__(self, "_result", result)
        object.__setattr__(self, "_bridge", result.bridge)
        object.__setattr__(self, "_source", result.source)
        object.__setattr__(self, "_shared", result.shared)
        object.__setattr__(self, "_attempt", result.attempt)
        object.__setattr__(self, "_domain", result.domain)
        object.__setattr__(self, "_owner_thread", threading.current_thread())
        object.__setattr__(self, "_receipt_bytes", result.receipt_bytes)
        object.__setattr__(self, "_terminal_bytes", ABORT.SUP.canonical(terminal))
        object.__setattr__(self, "_deadline_ns", result.authority.deadline_ns)
        object.__setattr__(self, "_watermark_ns", result.watermark.value)
        object.__setattr__(self, "_allocation_authority",
                           result.authority.allocation_authority)
        object.__setattr__(self, "_owned_pidfd",
                           result.authority.owned_pidfd)
        object.__setattr__(self, "_consumed", False)
        object.__setattr__(self, "_consumer", None)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise Refusal("allocated terminal evidence is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        raise Refusal("allocated terminal evidence is immutable")

    def __copy__(self):
        raise Refusal("allocated terminal evidence is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("allocated terminal evidence is noncopyable")

    def snapshot(self):
        return {"schema": 1,
                "classification": "ORIGINAL_MODEL_ECHILD_SEALED",
                "deadline_ns": self._deadline_ns,
                "watermark_ns": self._watermark_ns,
                "descendants_qualified": False,
                "resources_closed": False,
                "runtime_authorized": False,
                "storage_authorized": False,
                "postcondition_verified": False}


def _validate_allocated_terminal_evidence(evidence, consumer=None):
    """Callback-free validation of the exact originally minted ECHILD seal."""
    require(type(evidence) is AllocatedTerminalDomainEvidence
            and threading.current_thread() is evidence._owner_thread
            and type(evidence._consumed) is bool
            and ((consumer is None and evidence._consumed is False
                  and evidence._consumer is None)
                 or (consumer is not None and evidence._consumed is True
                     and evidence._consumer is consumer))
            and type(evidence._receipt_bytes) is bytes
            and type(evidence._terminal_bytes) is bytes
            and type(evidence._deadline_ns) is int
            and type(evidence._watermark_ns) is int
            and type(evidence._allocation_authority) is tuple
            and type(evidence._owned_pidfd) is int
            and type(evidence._sealed) is bool
            and evidence._sealed is True,
            "fresh exact allocated terminal evidence required")
    result = evidence._result
    bridge = evidence._bridge
    source = evidence._source
    shared = evidence._shared
    attempt = evidence._attempt
    domain = evidence._domain
    require(type(result) is AllocatedDescendantStepResult
            and result.terminal_evidence is evidence
            and attempt.descendant_terminal_evidence is evidence
            and result.bridge is bridge and result.source is source
            and result.shared is shared and result.attempt is attempt
            and result.domain is domain
            and bridge.advance_result is result
            and attempt.descendant_step_result is result
            and type(result.state) is str
            and result.state == "ALLOCATED_MODEL_DOMAIN_DRAINED_NONQUALIFIED"
            and type(bridge.state) is str and bridge.state == result.state
            and type(shared.state) is str and shared.state == result.state,
            "allocated terminal evidence backlinks changed")
    _validate_allocated_step_outer(
        result, ("DRAINED",), receipt_stored=True,
        terminal_sealed=True)
    require(type(result.receipt) is dict
            and all(type(key) is str for key in result.receipt)
            and type(result.receipt_bytes) is bytes
            and result.receipt_bytes is evidence._receipt_bytes,
            "allocated terminal receipt changed")
    GRAPH.CONSUMER._builtin_tree(
        result.receipt, "sealed allocated terminal receipt")
    require(ABORT.SUP.canonical(result.receipt) == evidence._receipt_bytes
            and _validate_allocated_step_postcondition(
                result, result.receipt)
                == "ALLOCATED_MODEL_DOMAIN_DRAINED_NONQUALIFIED",
            "allocated terminal receipt changed")
    terminal = domain.terminal_echild
    require(type(terminal) is dict
            and all(type(key) is str for key in terminal)
            and set(terminal) == {"errno", "observed_ns", "options"}
            and all(type(terminal[key]) is int for key in terminal)
            and terminal["errno"] == errno.ECHILD
            and terminal["options"] == DESC.WAIT_DISCOVERY_OPTIONS
            and ABORT.SUP.canonical(terminal) == evidence._terminal_bytes,
            "allocated terminal ECHILD changed")
    require(result.authority.deadline_ns == evidence._deadline_ns
            and result.watermark.value == evidence._watermark_ns
            and domain.deadline_ns == evidence._deadline_ns
            and domain.last_clock_ns == evidence._watermark_ns
            and terminal["observed_ns"] == evidence._watermark_ns
            and result.authority.initial_watermark_ns
                <= evidence._watermark_ns < evidence._deadline_ns
            and result.authority.allocation_authority
                is evidence._allocation_authority
            and attempt.handle._allocation_authority
                is evidence._allocation_authority
            and result.authority.owned_pidfd == evidence._owned_pidfd
            and bridge.attachment.owned_handle.pidfd
                == evidence._owned_pidfd,
            "allocated terminal frozen authority changed")
    require(domain.state == "DRAINED" and domain.sequence == 0
            and domain.records == [] and domain.pending is None
            and result.discovery_attempted is True
            and result.echild_observed is True
            and result.identity_attempted is False
            and result.pidfd_open_attempted is False
            and result.adopted_pidfd is None
            and result.pidfd_open_returned is False
            and result.pidfd_observe_attempted is False
            and result.pidfd_observe_result is None
            and result.pidfd_reap_attempted is False
            and result.pidfd_reap_result is None
            and result.pidfd_close_attempted is False
            and result.pidfd_close_returned is False
            and result.pidfd_close_confirmed is False
            and result.pidfd_close_unknown is False
            and result.error is None,
            "allocated terminal direct ECHILD path changed")
    _validate_step_fd_graph(result, attempt.handle)
    return True


def validate_allocated_terminal_evidence(evidence):
    return _validate_allocated_terminal_evidence(evidence)


def _reserve_allocated_terminal_evidence(evidence, consumer):
    """Consume the terminal seal once for its exact allocated-status owner."""
    require(consumer is not None,
            "private allocated terminal reservation")
    _validate_allocated_terminal_evidence(evidence)
    object.__setattr__(evidence, "_consumed", True)
    object.__setattr__(evidence, "_consumer", consumer)
    _validate_allocated_terminal_evidence(evidence, consumer)
    return True


def _validate_step_fd_graph(result, handle):
    authority = result.authority
    require(type(handle.bundle) is dict
            and type(handle.frozen_bundle) is dict
            and all(type(key) is str for key in handle.bundle)
            and all(type(key) is str for key in handle.frozen_bundle)
            and set(handle.bundle) == set(FDS._ALL_ROLES)
            and set(handle.frozen_bundle) == set(FDS._ALL_ROLES),
            "strict allocated step FD containers")
    expected = dict(authority.allocation_authority)
    require(set(expected) == set(FDS._ALL_ROLES)
            and all(type(role) is str for role in expected)
            and all(type(values) is tuple
                    and len(values) == len(FDS._IDENTITY_KEYS)
                    and all(type(value) is int for value in values)
                    for values in expected.values()),
            "allocated step FD authority roles changed")
    for role in FDS._ALL_ROLES:
        live = handle.bundle[role]
        frozen = handle.frozen_bundle[role]
        require(type(live) is dict and type(frozen) is dict
                and all(type(key) is str for key in live)
                and all(type(key) is str for key in frozen)
                and set(live) == set(FDS._IDENTITY_KEYS)
                and set(frozen) == set(FDS._IDENTITY_KEYS)
                and all(type(live[key]) is int
                        and type(frozen[key]) is int
                        for key in FDS._IDENTITY_KEYS)
                and tuple(live.get(key) for key in FDS._IDENTITY_KEYS)
                == expected[role]
                and tuple(frozen.get(key) for key in FDS._IDENTITY_KEYS)
                == expected[role],
                "allocated step FD endpoint changed")


def _validate_allocated_step_outer(result, allowed_domain_states,
                                   receipt_stored=False,
                                   terminal_sealed=False):
    """Callback-free outer guard used around every modeled syscall."""
    require(type(result) is AllocatedDescendantStepResult
            and type(allowed_domain_states) is tuple
            and allowed_domain_states
            and all(type(value) is str for value in allowed_domain_states),
            "exact allocated step guard inputs")
    require(type(receipt_stored) is bool and type(terminal_sealed) is bool,
            "strict allocated step receipt phase")
    record_state = ("ALLOCATED_MODEL_DOMAIN_DRAINED_NONQUALIFIED"
                    if terminal_sealed else "ADVANCING")
    shared_state = (record_state if terminal_sealed
                    else "DESCENDANT_STEP_ADVANCING")
    bridge = result.bridge
    source = result.source
    shared = result.shared
    attempt = result.attempt
    domain = result.domain
    attachment = bridge.attachment
    settlement = bridge.settlement
    adapter = attachment.adapter
    owned = attachment.owned_handle
    authority = result.authority
    descendant_capability = source.descendant_capability
    leader_capability = bridge.leader_capability
    leader_claim = bridge.leader_claim
    require(type(bridge) is AllocatedDescendantDrainBridge
            and type(source) is AllocatedDeadlineBoundAttachment
            and type(shared) is GRAPH.AllocatedDescendantEnrollment
            and type(attempt) is ABORT.AbortForkAttempt
            and type(domain) is DESC.ChildDomain
            and bridge.source is source
            and bridge.attachment is attachment
            and bridge.shared is shared and bridge.attempt is attempt
            and bridge.domain is domain and bridge.settlement is settlement
            and source.source_attachment is attachment
            and source.shared is shared and source.attempt is attempt
            and source.domain is domain and source.settlement is settlement
            and attachment.shared is shared
            and attachment.attempt is attempt
            and attachment.domain is domain
            and attachment.adapter is adapter
            and attachment.owned_handle is owned
            and adapter.handle is owned
            and owned.capture_adapter is adapter
            and attachment.capture is attempt.capture_handle
            and attempt.capture_adapter is adapter
            and attempt.owned_pidfd is owned,
            "allocated step exact authority backlinks changed")
    require(type(descendant_capability)
            is CAPTURE._DescendantSettlementCapability
            and type(descendant_capability.__dict__) is dict
            and all(type(key) is str
                    for key in descendant_capability.__dict__)
            and type(descendant_capability.used) is bool
            and type(settlement.probe_used) is bool
            and type(leader_capability) is OWNED._ExactLeaderReapCapability
            and type(leader_capability.__dict__) is dict
            and all(type(key) is str for key in leader_capability.__dict__)
            and set(leader_capability.__dict__) == {
                "binding", "adapter", "handle", "fork_origin", "receipt",
                "owner_thread", "leader", "used"}
            and type(leader_capability.used) is bool
            and type(leader_claim) is DESC.LeaderReapClaim
            and type(leader_claim.__dict__) is dict
            and all(type(key) is str for key in leader_claim.__dict__)
            and set(leader_claim.__dict__) == {"seal", "leader", "used"}
            and type(leader_claim.used) is bool,
            "strict allocated step consumed capabilities")
    _strict_leader_identity(
        leader_capability.leader, "allocated step capability leader")
    _strict_leader_identity(
        leader_claim.leader, "allocated step leader claim")
    require(threading.current_thread() is result.owner_thread
            and result.owner_thread is bridge.owner_thread
            and result.owner_thread is attempt.owner_thread
            and type(authority) is _AllocatedStepAuthority
            and type(authority.deadline_ns) is int
            and type(authority.initial_watermark_ns) is int
            and type(result.watermark) is _AllocatedStepWatermark
            and type(result.watermark.__dict__) is dict
            and all(type(key) is str for key in result.watermark.__dict__)
            and set(result.watermark.__dict__) == {"value", "_sealed"}
            and type(result.watermark).__dict__.get("advance")
            is _STEP_WATERMARK_ADVANCE_METHOD
            and "advance" not in result.watermark.__dict__
            and type(result.watermark.value) is int
            and authority.initial_watermark_ns <= result.watermark.value
            and result.watermark.value < authority.deadline_ns
            and type(authority.initial_sequence) is int
            and authority.initial_sequence == 0
            and type(authority.request_id) is str
            and type(authority.domain_id) is str
            and type(authority.owned_pidfd) is int
            and type(authority.owner_bytes) is bytes
            and type(authority.settlement_receipt_bytes) is bytes
            and type(authority.allocation_authority) is tuple
            and type(authority.fd_owned) is frozenset
            and type(authority.fd_lost) is frozenset
            and type(authority.fd_attempted_closes) is frozenset
            and type(authority.fd_attempted_writes) is frozenset
            and type(authority.capture_stream_refs) is tuple
            and len(authority.capture_stream_refs) == 2
            and all(type(item) is tuple and len(item) == 2
                    and type(item[0]) is str
                    for item in authority.capture_stream_refs)
            and type(authority.lifecycle) is dict
            and type(authority.lifecycle_origin) is OWNED._ForkOrigin
            and type(authority.pre_fork_enrollment)
            is OWNED._PreForkDescendantEnrollment
            and type(authority.monitor_result_bytes) is bytes
            and type(authority.preserved_fds) is frozenset
            and all(type(fd) is int for fd in authority.preserved_fds)
            and type(result.state) is str and result.state == record_state
            and ((receipt_stored
                  and type(result.receipt) is dict
                  and type(result.receipt_bytes) is bytes)
                 or (not receipt_stored and result.receipt is None
                     and result.receipt_bytes is None))
            and result.error is None,
            "allocated step record changed")
    if receipt_stored:
        GRAPH.CONSUMER._builtin_tree(
            result.receipt, "allocated step stored receipt")
    require(type(bridge.state) is str and bridge.state == record_state
            and type(bridge.advance_attempted) is bool
            and bridge.advance_attempted is True
            and bridge.advance_result is result
            and bridge.error is None
            and bridge.ready_watermark_ns == authority.initial_watermark_ns
            and source.used is True
            and type(source.deadline_ns) is int
            and source.deadline_ns == authority.deadline_ns
            and descendant_capability is bridge.settlement_capability
            and descendant_capability.used is True
            and settlement.probe_used is True
            and leader_capability is adapter.reap_capability
            and leader_capability.used is True
            and leader_claim.used is True
            and type(shared.state) is str
            and shared.state == shared_state
            and attempt.descendant_drain_bridge is bridge
            and attempt.descendant_step_started is True
            and attempt.descendant_step_result is result
            and type(attempt.poisoned) is bool
            and attempt.poisoned is False,
            "allocated step authority changed")
    require(type(adapter.__dict__) is dict
            and all(type(key) is str for key in adapter.__dict__)
            and type(adapter.state) is str
            and type(owned.__dict__) is dict
            and all(type(key) is str for key in owned.__dict__)
            and type(owned.state) is str
            and type(owned.child) is dict
            and all(type(key) is str for key in owned.child)
            and set(owned.child) == {"pid", "starttime", "boot_id"}
            and type(owned.child["pid"]) is int
            and type(owned.child["starttime"]) is int
            and type(owned.child["boot_id"]) is str
            and type(owned.lifecycle) is dict
            and all(type(key) is str for key in owned.lifecycle)
            and type(owned.lifecycle.get("state")) is str,
            "strict allocated step adapter lifecycle")
    origin = authority.lifecycle_origin
    require(type(origin) is OWNED._ForkOrigin
            and type(origin.__dict__) is dict
            and all(type(key) is str for key in origin.__dict__)
            and set(origin.__dict__)
                == {"pid", "claimed", "pre_fork_enrollment"}
            and type(origin.pid) is int
            and type(origin.claimed) is bool
            and origin.claimed is True
            and origin.pid == owned.child["pid"]
            and origin.pre_fork_enrollment
                is authority.pre_fork_enrollment,
            "allocated step fork origin changed")
    require(type(settlement.receipt) is dict
            and all(type(key) is str for key in settlement.receipt)
            and type(settlement.owner) is dict
            and all(type(key) is str for key in settlement.owner)
            and type(domain.owner) is dict
            and all(type(key) is str for key in domain.owner)
            and type(attempt.capture_handle.normal_completion) is dict
            and all(type(key) is str
                    for key in attempt.capture_handle.normal_completion)
            and all(type(value) is set for value in (
                attempt.handle.owned, attempt.handle.lost,
                attempt.handle.attempted_closes,
                attempt.handle.attempted_writes))
            and all(type(role) is str for values in (
                attempt.handle.owned, attempt.handle.lost,
                attempt.handle.attempted_closes,
                attempt.handle.attempted_writes) for role in values)
            and type(attempt.handle.state) is str
            and attempt.handle.state == "FD_GRAPH_READY"
            and type(attempt.handle.poisoned) is bool
            and attempt.handle.poisoned is False
            and attempt.handle.active is None
            and type(attempt.capture_handle.streams) is dict
            and all(type(key) is str
                    for key in attempt.capture_handle.streams)
            and set(attempt.capture_handle.streams) == {"stdout", "stderr"}
            and {item[0] for item in authority.capture_stream_refs}
            == {"stdout", "stderr"}
            and type(owned.pidfd) is int
            and owned.pidfd == authority.owned_pidfd
            and attempt.handle._allocation_authority
            is authority.allocation_authority
            and frozenset(attempt.handle.owned) == authority.fd_owned
            and frozenset(attempt.handle.lost) == authority.fd_lost
            and frozenset(attempt.handle.attempted_closes)
            == authority.fd_attempted_closes
            and frozenset(attempt.handle.attempted_writes)
            == authority.fd_attempted_writes
            and attempt.capture_handle.origin is authority.capture_origin
            and attempt.capture_handle.poller is authority.capture_poller
            and type(attempt._capture_stream_refs) is dict
            and all(type(key) is str
                    for key in attempt._capture_stream_refs)
            and set(attempt._capture_stream_refs) == {"stdout", "stderr"}
            and all(attempt.capture_handle.streams.get(role) is stream
                    and attempt._capture_stream_refs.get(role) is stream
                    for role, stream in authority.capture_stream_refs),
            "allocated step frozen outer provenance changed")
    poller = attempt.capture_handle.poller
    require(type(poller) is select.epoll
            and poller is authority.capture_poller
            and type(poller.closed) is bool and poller.closed is False
            and type(authority.epoll_fd) is int
            and poller.fileno() == authority.epoll_fd,
            "allocated step capture epoll changed")
    GRAPH.CONSUMER._builtin_tree(
        settlement.receipt, "allocated step live settlement")
    GRAPH.CONSUMER._builtin_tree(
        settlement.owner, "allocated step settlement owner")
    GRAPH.CONSUMER._builtin_tree(
        domain.owner, "allocated step domain owner")
    GRAPH.CONSUMER._builtin_tree(
        attempt.capture_handle.normal_completion,
        "allocated step capture completion")
    GRAPH.CONSUMER._builtin_tree(
        owned.child, "allocated step owned child")
    require(ABORT.SUP.canonical(settlement.receipt)
            == authority.settlement_receipt_bytes
            and ABORT.SUP.canonical(settlement.owner) == authority.owner_bytes
            and ABORT.SUP.canonical(domain.owner) == authority.owner_bytes,
            "allocated step canonical provenance changed")
    require(type(attempt._owned_child_bytes) is bytes
            and ABORT.SUP.canonical(owned.child)
                == attempt._owned_child_bytes,
            "allocated step owned child changed")
    _validate_step_fd_graph(result, attempt.handle)
    require(owned.lifecycle is authority.lifecycle
            and owned.lifecycle is attempt.lifecycle
            and owned.lifecycle["origin"] is authority.lifecycle_origin
            and authority.lifecycle_origin.pre_fork_enrollment
            is authority.pre_fork_enrollment
            and attempt.ticket is authority.pre_fork_enrollment
            and shared.pre_fork_enrollment is authority.pre_fork_enrollment
            and ABORT.SUP.canonical(attempt.capture_handle.normal_completion)
            == authority.monitor_result_bytes,
            "allocated step lifecycle/capture provenance changed")
    require(type(domain.__dict__) is dict
            and all(type(key) is str for key in domain.__dict__)
            and type(domain).__dict__.get("_continuity")
            is _DOMAIN_CONTINUITY_METHOD
            and "_continuity" not in domain.__dict__
            and type(domain).__dict__.get("_clock") is _DOMAIN_CLOCK_METHOD
            and "_clock" not in domain.__dict__
            and type(domain).__dict__.get("_receipt")
            is _DOMAIN_RECEIPT_METHOD
            and "_receipt" not in domain.__dict__
            and type(domain).__dict__.get("_unknown")
            is _DOMAIN_UNKNOWN_METHOD
            and "_unknown" not in domain.__dict__
            and type(domain).__dict__.get("_local_owner")
            is _DOMAIN_LOCAL_OWNER_METHOD
            and "_local_owner" not in domain.__dict__,
            "allocated domain nested step surfaces changed")
    require(type(domain.state) is str
            and domain.state in allowed_domain_states
            and type(domain.deadline_ns) is int
            and domain.deadline_ns == authority.deadline_ns
            and type(domain.request_id) is str
            and domain.request_id == authority.request_id
            and domain.request_id == shared.graph.request_id
            and type(domain.domain_id) is str
            and domain.domain_id == authority.domain_id
            and domain.domain_id == shared.domain_id
            and type(domain.last_clock_ns) is int
            and authority.initial_watermark_ns <= domain.last_clock_ns
            and domain.last_clock_ns <= result.watermark.value
            and domain.owner_thread is result.owner_thread
            and domain._seal is shared.domain_seal
            and domain.bridge_enrollment is shared.binding
            and type(domain.sequence) is int
            and domain.sequence in (authority.initial_sequence,
                                    authority.initial_sequence + 1)
            and type(domain.records) is list
            and len(domain.records) <= 1,
            "allocated step domain changed")
    _strict_leader_identity(domain.expected_leader,
                            "allocated step expected leader")
    _strict_leader_identity(domain.leader, "allocated step accepted leader")
    require(domain.expected_leader == domain.leader
            and domain.leader == leader_capability.leader
            and adapter.state == "DESCENDANT_HANDOFF"
            and owned.state == "REAPED"
            and owned.lifecycle["state"] == "OWNED_LEADER_REAPED_ONLY",
            "allocated step leader chain changed")
    return True


def _normalize_step_terminal(value):
    require(type(value) is types.SimpleNamespace
            and type(value.__dict__) is dict
            and all(type(key) is str for key in value.__dict__)
            and set(value.__dict__) == {"si_pid", "si_code", "si_status"}
            and type(value.__dict__["si_pid"]) is int
            and value.__dict__["si_pid"] > 0
            and type(value.__dict__["si_code"]) is int
            and type(value.__dict__["si_status"]) is int,
            "strict modeled terminal result")
    return types.SimpleNamespace(
        si_pid=value.__dict__["si_pid"],
        si_code=value.__dict__["si_code"],
        si_status=value.__dict__["si_status"])


class _AllocatedStepBackend:
    """Strict model adapter guarding the outer authority around callbacks."""
    def __init__(self, key, result, delegate):
        delegate_type = type(delegate)
        require(key is _ALLOCATED_ATTACH_KEY
                and delegate_type.__getattribute__
                is object.__getattribute__
                and type(delegate.__dict__) is dict
                and all(type(name) is str for name in delegate.__dict__)
                and delegate.__dict__.get(
                    "allocated_descendant_model_backend") is True,
                "explicit allocated descendant model backend required")
        names = ("owner_identity", "kernel_thread_ids", "sigchld_policy",
                 "get_subreaper", "monotonic_ns", "wait_all_wall_wnowait",
                 "child_identity", "pidfd_open", "wait_pidfd",
                 "close_pidfd")
        require(all(type(delegate_type.__dict__.get(name))
                    is types.FunctionType for name in names),
                "pinned allocated model backend methods required")
        self.result = result
        self.delegate = delegate
        self._methods = {
            name: delegate_type.__dict__[name] for name in names}

    def _before(self):
        _validate_allocated_step_outer(self.result, ("STEPPING",))

    def _after(self):
        _validate_allocated_step_outer(self.result, ("STEPPING",))

    def owner_identity(self):
        self._before()
        value = self._methods["owner_identity"](self.delegate)
        self._after()
        require(type(value) is dict
                and all(type(key) is str for key in value)
                and set(value) == {"pid", "starttime", "boot_id"},
                "strict modeled owner identity")
        GRAPH.CONSUMER._builtin_tree(value, "modeled step owner")
        return dict(value)

    def kernel_thread_ids(self):
        self._before()
        value = self._methods["kernel_thread_ids"](self.delegate)
        self._after()
        require(type(value) is list and len(value) == 1
                and type(value[0]) is int,
                "strict modeled thread ids")
        return list(value)

    def sigchld_policy(self):
        self._before()
        value = self._methods["sigchld_policy"](self.delegate)
        self._after()
        require(type(value) is dict
                and all(type(key) is str for key in value)
                and set(value) == {
                    "handler_default", "ignored", "no_cldwait"}
                and all(type(item) is bool for item in value.values()),
                "strict modeled SIGCHLD policy")
        return dict(value)

    def get_subreaper(self):
        self._before()
        value = self._methods["get_subreaper"](self.delegate)
        self._after()
        require(type(value) is int, "strict modeled subreaper")
        return value

    def monotonic_ns(self):
        self._before()
        value = self._methods["monotonic_ns"](self.delegate)
        self._after()
        require(type(value) is int
                and value >= self.result.watermark.value
                and value < self.result.authority.deadline_ns,
                "strict modeled step clock")
        _STEP_WATERMARK_ADVANCE_METHOD(
            self.result.watermark, _STEP_WATERMARK_KEY, value,
            self.result.authority.deadline_ns)
        return value

    def wait_all_wall_wnowait(self, options):
        self._before()
        require(type(options) is int
                and options == DESC.WAIT_DISCOVERY_OPTIONS,
                "strict modeled discovery options")
        self.result.discovery_attempted = True
        try:
            value = self._methods["wait_all_wall_wnowait"](
                self.delegate, options)
        except ChildProcessError as exc:
            self._after()
            require(type(exc) is ChildProcessError
                    and type(exc.errno) is int and exc.errno == errno.ECHILD,
                    "strict modeled ECHILD")
            self.result.echild_observed = True
            raise ChildProcessError(errno.ECHILD, "modeled ECHILD")
        self._after()
        if value is None:
            return None
        return _normalize_step_terminal(value)

    def child_identity(self, pid):
        self._before()
        require(type(pid) is int and pid > 0, "strict modeled child pid")
        self.result.identity_attempted = True
        value = self._methods["child_identity"](self.delegate, pid)
        self._after()
        require(type(value) is dict
                and all(type(key) is str for key in value)
                and set(value) == {"pid", "starttime", "boot_id", "ppid"}
                and all(type(value[key]) is int
                        for key in ("pid", "starttime", "ppid"))
                and type(value["boot_id"]) is str,
                "strict modeled child identity")
        GRAPH.CONSUMER._builtin_tree(value, "modeled step child")
        return dict(value)

    def pidfd_open(self, pid):
        self._before()
        require(type(pid) is int and pid > 0, "strict modeled pidfd pid")
        self.result.pidfd_open_attempted = True
        value = self._methods["pidfd_open"](self.delegate, pid)
        require(type(value) is int and value > 2
                and value != self.result.authority.owned_pidfd
                and value not in self.result.authority.preserved_fds,
                "strict modeled adopted pidfd")
        self.result.adopted_pidfd = value
        self.result.pidfd_open_returned = True
        self._after()
        return value

    def wait_pidfd(self, pidfd, options):
        self._before()
        require(type(pidfd) is int and pidfd >= 0
                and self.result.pidfd_open_returned is True
                and pidfd == self.result.adopted_pidfd
                and type(options) is int
                and options in (DESC.WAIT_PIDFD_OBSERVE,
                                DESC.WAIT_PIDFD_REAP),
                "strict modeled pidfd wait")
        if options == DESC.WAIT_PIDFD_OBSERVE:
            self.result.pidfd_observe_attempted = True
        else:
            self.result.pidfd_reap_attempted = True
        value = self._methods["wait_pidfd"](
            self.delegate, pidfd, options)
        normalized = _normalize_step_terminal(value)
        evidence = {"pid": normalized.si_pid,
                    "code": normalized.si_code,
                    "status": normalized.si_status}
        if options == DESC.WAIT_PIDFD_OBSERVE:
            self.result.pidfd_observe_result = evidence
        else:
            self.result.pidfd_reap_result = evidence
        self._after()
        return normalized

    def close_pidfd(self, pidfd):
        self._before()
        require(type(pidfd) is int and pidfd >= 0
                and self.result.pidfd_open_returned is True
                and pidfd == self.result.adopted_pidfd,
                "strict modeled close pidfd")
        self.result.pidfd_close_attempted = True
        try:
            value = self._methods["close_pidfd"](self.delegate, pidfd)
        except BaseException:
            self.result.pidfd_close_unknown = True
            raise
        if value is not None:
            self.result.pidfd_close_unknown = True
            raise Refusal("strict modeled close result")
        self.result.pidfd_close_returned = True
        try:
            self._after()
        except BaseException:
            self.result.pidfd_close_unknown = True
            raise
        self.result.pidfd_close_confirmed = True


def _validate_allocated_step_postcondition(result, receipt):
    domain = result.domain
    require(type(receipt) is dict
            and all(type(key) is str for key in receipt),
            "strict allocated step receipt")
    GRAPH.CONSUMER._builtin_tree(receipt, "allocated step receipt")
    require(set(receipt) == {
                "schema", "request_id", "domain_id", "owner",
                "wait_domain", "leader", "adopted_records",
                "pending_adopted", "terminal_echild", "outcome",
                "reason", "runtime_authorized", "storage_authorized",
                "postcondition_verified"}
            and type(receipt["schema"]) is int
            and receipt["schema"] == 1
            and type(receipt["owner"]) is dict
            and type(receipt["leader"]) is dict
            and type(receipt["adopted_records"]) is list
            and (receipt["pending_adopted"] is None
                 or type(receipt["pending_adopted"]) is dict)
            and (receipt["terminal_echild"] is None
                 or type(receipt["terminal_echild"]) is dict),
            "allocated step receipt schema changed")
    GRAPH.CONSUMER._builtin_tree(domain.owner,
                                 "allocated step domain owner")
    GRAPH.CONSUMER._builtin_tree(domain.leader,
                                 "allocated step domain leader")
    GRAPH.CONSUMER._builtin_tree(domain.records,
                                 "allocated step domain records")
    if domain.pending is not None:
        GRAPH.CONSUMER._builtin_tree(domain.pending,
                                     "allocated step domain pending")
    if domain.terminal_echild is not None:
        GRAPH.CONSUMER._builtin_tree(
            domain.terminal_echild, "allocated step domain ECHILD")
    outcome = receipt.get("outcome")
    require(type(outcome) is str
            and receipt.get("request_id") == domain.request_id
            and receipt.get("domain_id") == domain.domain_id
            and receipt.get("runtime_authorized") is False
            and receipt.get("storage_authorized") is False
            and receipt.get("postcondition_verified") is False,
            "allocated step receipt authority changed")
    require(receipt["owner"] == domain.owner
            and receipt["leader"] == domain.leader
            and receipt["adopted_records"] == domain.records
            and receipt["pending_adopted"] == domain.pending
            and receipt["terminal_echild"] == domain.terminal_echild,
            "allocated step receipt/domain evidence changed")
    if outcome == "CHILDREN_PENDING":
        require(domain.state == "DRAINING"
                and domain.sequence == result.authority.initial_sequence
                and domain.records == [] and domain.pending is None
                and domain.terminal_echild is None,
                "allocated pending step postcondition")
        return "ALLOCATED_STEP_PENDING_NONQUALIFIED"
    if outcome == "ADOPTED_CHILD_REAPED_CONTINUE":
        require(domain.state == "DRAINING"
                and domain.sequence == result.authority.initial_sequence + 1
                and len(domain.records) == 1
                and domain.pending is None
                and domain.terminal_echild is None
                and result.pidfd_open_attempted
                and result.pidfd_open_returned
                and result.pidfd_observe_attempted
                and type(result.pidfd_observe_result) is dict
                and result.pidfd_reap_attempted
                and type(result.pidfd_reap_result) is dict
                and result.pidfd_close_attempted
                and result.pidfd_close_returned
                and result.pidfd_close_confirmed
                and not result.pidfd_close_unknown,
                "allocated adopted step postcondition")
        return "ALLOCATED_STEP_ADOPTED_NONQUALIFIED"
    if outcome == "MODEL_CHILD_DOMAIN_DRAINED":
        terminal = domain.terminal_echild
        require(domain.state == "DRAINED"
                and domain.sequence == result.authority.initial_sequence
                and domain.records == [] and domain.pending is None
                and type(terminal) is dict
                and all(type(key) is str for key in terminal)
                and set(terminal) == {"errno", "observed_ns", "options"}
                and type(terminal["errno"]) is int
                and terminal["errno"] == errno.ECHILD
                and type(terminal["observed_ns"]) is int
                and result.authority.initial_watermark_ns
                <= terminal["observed_ns"]
                < result.authority.deadline_ns
                and type(terminal["options"]) is int
                and terminal["options"] == DESC.WAIT_DISCOVERY_OPTIONS,
                "allocated drained step postcondition")
        require(result.echild_observed is True,
                "allocated drain has no guarded ECHILD witness")
        return "ALLOCATED_MODEL_DOMAIN_DRAINED_NONQUALIFIED"
    require(outcome == "UNKNOWN" and domain.state == "UNKNOWN",
            "unexpected allocated step outcome")
    return "UNKNOWN"


def advance_allocated_descendant_once(bridge, modeled_syscalls):
    """Run exactly one guarded model-domain step; mint no release authority."""
    require(type(bridge) is AllocatedDescendantDrainBridge,
            "exact allocated drain bridge required")
    result = None
    try:
        require(type(bridge.state) is str
                and bridge.state == "ALLOCATED_DRAIN_READY"
                and type(bridge.ready_watermark_ns) is int
                and bridge.advance_attempted is False
                and bridge.advance_result is None
                and bridge.attempt.descendant_step_started is False
                and bridge.attempt.descendant_step_result is None,
                "fresh allocated descendant advance required")
        _validate_allocated_drain_chain(
            bridge.source, bridge, transferred=True, accepted=True,
            ready=True, now_ns=bridge.ready_watermark_ns)
        require(type(bridge.domain).__dict__.get("step")
                is _DOMAIN_STEP_METHOD
                and "step" not in bridge.domain.__dict__,
                "allocated domain step surface changed")
        result = AllocatedDescendantStepResult(
            _ALLOCATED_ATTACH_KEY, bridge)
        bridge.advance_attempted = True
        bridge.advance_result = result
        bridge.state = "ADVANCING"
        object.__setattr__(bridge.shared, "state",
                           "DESCENDANT_STEP_ADVANCING")
        _ATTEMPT_SET_METHOD(
            bridge.attempt, "descendant_step_started", True)
        _ATTEMPT_SET_METHOD(
            bridge.attempt, "descendant_step_result", result)
        _validate_allocated_step_outer(result, ("DRAINING",))
        guarded = _AllocatedStepBackend(
            _ALLOCATED_ATTACH_KEY, result, modeled_syscalls)
        receipt = _DOMAIN_STEP_METHOD(bridge.domain, guarded)
        require(type(receipt) is dict
                and all(type(key) is str for key in receipt),
                "strict returned allocated step receipt")
        GRAPH.CONSUMER._builtin_tree(
            receipt, "returned allocated step receipt")
        result.receipt = copy.deepcopy(receipt)
        result.receipt_bytes = ABORT.SUP.canonical(receipt)
        _validate_allocated_step_outer(
            result, ("DRAINING", "DRAINED", "UNKNOWN"),
            receipt_stored=True)
        classification = _validate_allocated_step_postcondition(
            result, result.receipt)
        result.state = classification
        bridge.state = classification
        object.__setattr__(bridge.shared, "state", classification)
        if classification == "ALLOCATED_MODEL_DOMAIN_DRAINED_NONQUALIFIED":
            require(result.terminal_evidence is None
                    and bridge.attempt.descendant_terminal_evidence is None,
                    "fresh allocated terminal evidence slot required")
            evidence = AllocatedTerminalDomainEvidence(
                _TERMINAL_EVIDENCE_KEY, result)
            object.__setattr__(result, "terminal_evidence", evidence)
            _ATTEMPT_SET_METHOD(
                bridge.attempt, "descendant_terminal_evidence", evidence)
            validate_allocated_terminal_evidence(evidence)
        return result
    except BaseException as exc:
        if type(result) is AllocatedDescendantStepResult:
            result._poison(type(exc).__name__)
        bridge._poison(type(exc).__name__)
        if type(bridge.shared) is GRAPH.AllocatedDescendantEnrollment:
            bridge.shared._poison()
            bridge.shared.graph._poison()
        if type(bridge.domain) is DESC.ChildDomain:
            bridge.domain.state = "UNKNOWN"
        if type(bridge.attempt) is ABORT.AbortForkAttempt:
            bridge.attempt._poison("ALLOCATED_DESCENDANT_STEP")
        raise


def _assert_allocated_capture_frozen(
        attempt, owned, adapter, capture, allocation_authority):
    """Final callback-free check of the exact unarmed capture authority."""
    import prelive_abort_fork_split as ABORT
    require(type(owned) is OWNED._OwnedPidfdHandle
            and type(adapter) is OWNED._CaptureChildAdapter
            and type(capture) is CAPTURE.CaptureHandle
            and type(capture.origin) is CAPTURE._PipeOrigin
            and type(capture.origin.owner) is dict
            and type(capture.origin.child) is dict
            and type(capture.origin.identities) is dict
            and type(capture.streams) is dict
            and all(type(key) is str for key in capture.streams)
            and set(capture.streams) == {"stdout", "stderr"}
            and type(capture.fd_roles) is dict
            and all(type(fd) is int and type(role) is str
                    for fd, role in capture.fd_roles.items())
            and type(attempt._capture_stream_refs) is dict
            and all(type(key) is str
                    for key in attempt._capture_stream_refs)
            and set(attempt._capture_stream_refs) == {"stdout", "stderr"},
            "allocated capture containers changed")
    request = GRAPH._strict_bound_request(attempt.persisted.bound)
    handle = attempt.handle
    require(type(owned.state) is str and owned.state == "BOUND"
            and owned.observation is None
            and owned.owner_thread is attempt.owner_thread
            and type(owned.pidfd) is int
            and owned.pidfd == attempt._owned_pidfd_fd
            and type(owned.child) is dict
            and all(type(key) is str for key in owned.child)
            and type(owned.launcher) is dict
            and all(type(key) is str for key in owned.launcher),
            "allocated owned pidfd authority changed")
    GRAPH.CONSUMER._builtin_tree(owned.child,
                                 "allocated owned child")
    GRAPH.CONSUMER._builtin_tree(owned.launcher,
                                 "allocated owned launcher")
    require(ABORT.SUP.canonical(owned.child) == attempt._owned_child_bytes
            and ABORT.SUP.canonical(owned.launcher)
            == attempt._owned_launcher_bytes,
            "allocated owned provenance changed")
    expected_owned = {
        "grant_parent", "status_parent", "stdout_parent", "stderr_parent"}
    expected_closes = set(ABORT.PARENT_CLOSE_ROLES)
    require(type(handle.__dict__) is dict
            and all(type(key) is str for key in handle.__dict__)
            and all(name not in handle.__dict__ for name in (
                "_validate_stored_graph", "_validate_stored_role",
                "_authority_record"))
            and type(handle.owned) is set
            and type(handle.lost) is set
            and type(handle.attempted_closes) is set
            and type(handle.attempted_writes) is set
            and all(type(role) is str for roles in (
                handle.owned, handle.lost, handle.attempted_closes,
                handle.attempted_writes) for role in roles)
            and type(handle.state) is str
            and handle.state == "FD_GRAPH_READY"
            and type(handle.poisoned) is bool and handle.poisoned is False
            and handle.owned == expected_owned
            and handle.lost == set()
            and handle.attempted_closes == expected_closes
            and handle.attempted_writes == set()
            and handle.active is None,
            "allocated FD ownership graph changed")
    require(_strict_allocation_authority(handle) is allocation_authority,
            "allocated FD allocation authority changed")
    identity_keys = {"fd", "dev", "inode", "mode", "flags", "fd_flags"}
    require(type(handle.bundle) is dict
            and type(handle.frozen_bundle) is dict
            and all(type(role) is str for bundle in (
                handle.bundle, handle.frozen_bundle) for role in bundle)
            and set(handle.bundle) == set(FDS._ALL_ROLES)
            and set(handle.frozen_bundle) == set(FDS._ALL_ROLES),
            "allocated stored FD graph containers changed")
    for bundle in (handle.bundle, handle.frozen_bundle):
        for role in sorted(FDS._ALL_ROLES):
            identity = bundle[role]
            require(type(identity) is dict
                    and all(type(key) is str for key in identity)
                    and set(identity) == identity_keys
                    and all(type(identity[key]) is int
                            for key in identity_keys),
                    "allocated stored FD identity changed")
    role_authority = {
        role: dict(zip(FDS._IDENTITY_KEYS, values))
        for role, values in allocation_authority}
    for bundle in (handle.bundle, handle.frozen_bundle):
        for role in sorted(FDS._ALL_ROLES):
            require(bundle[role] == role_authority[role],
                    "allocated stored FD authority changed")
    GRAPH.CONSUMER._builtin_tree(capture.origin.owner,
                                 "allocated capture owner")
    GRAPH.CONSUMER._builtin_tree(capture.origin.child,
                                 "allocated capture child")
    GRAPH.CONSUMER._builtin_tree(capture.origin.identities,
                                 "allocated capture identities")
    for role in ("stdout", "stderr"):
        stream = capture.streams[role]
        require(type(stream) is CAPTURE._Stream
                and stream is attempt._capture_stream_refs[role]
                and type(stream.role) is str and stream.role == role
                and type(stream.identity) is dict
                and type(stream.limit) is int
                and type(stream.stored) is bytearray
                and stream.stored == bytearray()
                and type(stream.observed_bytes) is int
                and stream.observed_bytes == 0
                and type(stream.eof) is bool and stream.eof is False
                and type(stream.truncated) is bool
                and stream.truncated is False,
                "allocated capture stream changed")
        GRAPH.CONSUMER._builtin_tree(
            stream.identity, "allocated capture stream identity")
        CAPTURE._identity(stream.identity)
        require(stream.limit == request["capture_limit"]
                and stream.identity == capture.origin.identities[role],
                "allocated capture stream provenance changed")
        fd_role = role + "_parent"
        require(type(role_authority[fd_role]) is dict
                and all(stream.identity[key] == role_authority[fd_role][key]
                        for key in ("fd", "dev", "inode", "mode", "flags")),
                "allocated capture/FD role authority changed")
    require(type(capture.state) is str and capture.state == "BOUND"
            and capture.owner_thread is attempt.owner_thread
            and capture.origin is attempt._capture_origin
            and ABORT.SUP.canonical({"owner": capture.origin.owner,
                                     "child": capture.origin.child,
                                     "identities": capture.origin.identities})
            == attempt._capture_origin_bytes
            and capture.origin.claimed is True
            and capture.poller is attempt._capture_poller
            and getattr(capture.poller, "closed", False) is False
            and capture.fd_roles == {
                capture.streams[role].identity["fd"]: role
                for role in ("stdout", "stderr")}
            and capture.monitor_active is False
            and capture.primary_failure is None
            and capture.settlement_used is False
            and capture.execution_deadline_ns is None
            and capture.normal_completion is None
            and capture.normal_completion_capability is None
            and capture.child_adapter is adapter
            and capture.child_adapter_type is OWNED._CaptureChildAdapter
            and adapter.handle is owned
            and owned.capture_adapter is adapter
            and adapter.capture_authority is CAPTURE._CHILD_ADAPTER_KEY
            and adapter.capture_consumer is capture
            and type(adapter.state) is str and adapter.state == "PRIMARY"
            and adapter.cached is None
            and adapter.owner_thread is attempt.owner_thread,
            "allocated unarmed capture authority changed")


def prepare_domain_enrollment(domain):
    require(type(domain) is DESC.ChildDomain and domain.state == "PREPARED"
            and domain.launcher_claimed is False,
            "fresh prepared descendant domain required")
    enrollment = Enrollment(_ENROLL_KEY, domain)
    DESC.reserve_bridge_enrollment(
        DESC._BRIDGE_KEY, domain, enrollment.binding)
    return enrollment


def attach_owned_domain(enrollment, owned_handle, adapter, capture):
    try:
        require(type(enrollment) is Enrollment and not enrollment.used
                and threading.current_thread() is enrollment.owner_thread,
                "fresh owned enrollment required")
        require(type(owned_handle) is OWNED._OwnedPidfdHandle
                and type(adapter) is OWNED._CaptureChildAdapter
                and type(capture) is CAPTURE.CaptureHandle,
                "exact owned/capture objects required")
        require(adapter.handle is owned_handle
                and owned_handle.capture_adapter is adapter
                and capture.child_adapter is adapter
                and adapter.capture_consumer is capture
                and capture.state == "BOUND" and capture.monitor_active is False
                and adapter.state == "PRIMARY" and adapter.cached is None,
                "owned/capture attachment changed")
        require(enrollment.owner_thread is threading.current_thread()
                and owned_handle.owner_thread is threading.current_thread()
                and adapter.owner_thread is threading.current_thread()
                and capture.owner_thread is threading.current_thread()
                and enrollment.domain.owner
                == owned_handle.lifecycle["supervisor"]
                == capture.origin.owner,
                "supervisor ownership graph mismatch")
        require(enrollment.domain.state == "PREPARED"
                and enrollment.domain.bridge_enrollment is enrollment.binding,
                "descendant domain enrollment changed")
        origin = owned_handle.lifecycle["origin"]
        require(type(origin) is OWNED._ForkOrigin and origin.claimed is True
                and origin.pid == owned_handle.child["pid"]
                and origin.pre_fork_enrollment is enrollment.pre_fork_enrollment
                and enrollment.pre_fork_enrollment.used is True
                and enrollment.pre_fork_enrollment.binding is enrollment.binding,
                "exact fork origin is unavailable")
        leader = {"pid": owned_handle.child["pid"],
                  "starttime": owned_handle.child["starttime"],
                  "boot_id": owned_handle.child["boot_id"]}
        domain_origin = DESC._launcher_origin(
            DESC._LEADER_KEY, enrollment.domain, leader)
        enrollment.domain.claim_launcher(domain_origin)
        OWNED.enroll_descendant_bridge(adapter, enrollment.binding, capture)
        enrollment.used = True
        return DescendantBridge(_BRIDGE_KEY, enrollment, owned_handle, adapter,
                                capture, domain_origin)
    except BaseException:
        if type(enrollment) is Enrollment:
            enrollment.used = True
            DESC.poison_bridge_domain(DESC._BRIDGE_KEY, enrollment.domain)
        raise


def attach_allocated_owned_domain(shared, attempt):
    """Attach the shared branch without minting descendant-drain authority."""
    import prelive_abort_fork_split as ABORT
    require(type(shared) is GRAPH.AllocatedDescendantEnrollment
            and type(attempt) is ABORT.AbortForkAttempt,
            "exact allocated attachment inputs required")
    adapter = None
    try:
        require(shared.state == "FORKED" and shared.used is False
                and attempt.descendant_attachment is None
                and attempt.state == "LINUX_CAPTURE_BOUND_UNARMED"
                and attempt.poisoned is False,
                "fresh allocated fork/capture branch required")
        owned = attempt.owned_pidfd
        adapter = attempt.capture_adapter
        capture = attempt.capture_handle
        allocation_authority = _strict_allocation_authority(attempt.handle)
        require(type(owned) is OWNED._OwnedPidfdHandle
                and type(adapter) is OWNED._CaptureChildAdapter
                and type(capture) is CAPTURE.CaptureHandle,
                "exact allocated owned/capture references required")
        GRAPH.validate_allocated_descendant_fork(
            shared, attempt, "LINUX_CAPTURE_BOUND_UNARMED")
        object.__setattr__(shared, "used", True)
        object.__setattr__(shared, "state", "ATTACHING")
        require(_adapter_surface_is_pinned(
                    adapter, "assert_current_no_callback",
                    _ADAPTER_ASSERT_METHOD),
                "allocated adapter validation surface changed")
        attempt._validate_linux_capture_chain(
            "LINUX_CAPTURE_BOUND_UNARMED", attached=True,
            validate_shared=False)
        GRAPH.validate_allocated_descendant_attach_phase(
            shared, attempt, "PREPARED")
        require(shared.state == "ATTACHING" and shared.used is True
                and attempt.descendant_attachment is None,
                "allocated attachment changed reentrantly")
        domain = shared.domain
        require(type(owned) is OWNED._OwnedPidfdHandle
                and type(adapter) is OWNED._CaptureChildAdapter
                and type(capture) is CAPTURE.CaptureHandle
                and owned is adapter.handle
                and owned.capture_adapter is adapter
                and capture.child_adapter is adapter
                and adapter.capture_consumer is capture
                and adapter.state == "PRIMARY" and adapter.cached is None
                and adapter.descendant_binding is None
                and adapter.descendant_consumer is None
                and adapter.reap_capability is None
                and capture.state == "BOUND"
                and capture.monitor_active is False
                and domain.state == "PREPARED"
                and domain.bridge_enrollment is shared.binding
                and domain.deadline_ns == shared.cleanup_hard_limit_ns,
                "allocated attachment authority changed")
        leader = {"pid": owned.child["pid"],
                  "starttime": owned.child["starttime"],
                  "boot_id": owned.child["boot_id"]}
        GRAPH.CONSUMER._builtin_tree(
            leader, "allocated attachment leader")
        domain_origin = DESC._launcher_origin(
            DESC._LEADER_KEY, domain, leader)
        domain.claim_launcher(domain_origin)
        GRAPH.validate_allocated_descendant_attach_phase(
            shared, attempt, "LAUNCHER_CLAIMED")
        require(shared.state == "ATTACHING" and shared.used is True
                and domain.state == "LAUNCHER_CLAIMED",
                "allocated domain launcher claim changed")
        require(_adapter_surface_is_pinned(
                    adapter, "enroll_descendant", _ADAPTER_ENROLL_METHOD),
                "allocated adapter enrollment surface changed")
        _ADAPTER_ENROLL_METHOD(
            adapter, OWNED._DESCENDANT_KEY, shared.binding, capture)
        require(_adapter_surface_is_pinned(
                    adapter, "assert_current_no_callback",
                    _ADAPTER_ASSERT_METHOD),
                "allocated adapter validation surface changed")
        attempt._validate_linux_capture_chain(
            "LINUX_CAPTURE_BOUND_UNARMED", attached=True,
            validate_shared=False)
        GRAPH.validate_allocated_descendant_attach_phase(
            shared, attempt, "LAUNCHER_CLAIMED")
        require(shared.state == "ATTACHING" and shared.used is True
                and attempt.state == "LINUX_CAPTURE_BOUND_UNARMED"
                and attempt.poisoned is False
                and attempt.owned_pidfd is owned
                and attempt.capture_adapter is adapter
                and attempt.capture_handle is capture
                and adapter.descendant_binding is shared.binding
                and adapter.descendant_consumer is capture,
                "allocated adapter enrollment changed")
        _assert_allocated_capture_frozen(
            attempt, owned, adapter, capture, allocation_authority)
        attachment = AllocatedOwnedDomainAttachment(
            _ALLOCATED_ATTACH_KEY, shared, attempt, owned, adapter, capture,
            domain_origin, allocation_authority)
        attempt._set("descendant_attachment", attachment)
        object.__setattr__(shared, "state", "ATTACHED_HARD_LIMIT_ONLY")
        require(attempt.descendant_attachment is attachment
                and attachment.shared is shared
                and attachment.cleanup_deadline_bound is False,
                "allocated attachment publication changed")
        return attachment
    except BaseException:
        shared._poison()
        shared.graph._poison()
        if type(adapter) is OWNED._CaptureChildAdapter:
            adapter._poison("UNKNOWN_DESCENDANT_ATTACHMENT")
        attempt._poison("DESCENDANT_ATTACHMENT")
        raise


def validate_allocated_attachment_phase(
        attachment, attempt, expected_attempt_state, reaped=False,
        binding_phase=False):
    """Validate the exact inert attachment during capture/reap progression."""
    import prelive_abort_fork_split as ABORT
    require(type(attachment) is AllocatedOwnedDomainAttachment
            and type(attempt) is ABORT.AbortForkAttempt
            and type(expected_attempt_state) is str
            and type(reaped) is bool and type(binding_phase) is bool,
            "exact allocated attachment continuation inputs required")
    shared = attachment.shared
    adapter = attachment.adapter
    try:
        require(type(attachment.__dict__) is dict
                and all(type(key) is str for key in attachment.__dict__)
                and set(attachment.__dict__) == {
                    "shared", "attempt", "domain", "owned_handle",
                    "adapter", "capture", "domain_origin",
                    "allocation_authority", "binding",
                    "pre_fork_enrollment", "owner_thread", "state",
                    "cleanup_deadline_bound", "used", "_sealed"}
                and attachment.attempt is attempt
                and attempt.descendant_attachment is attachment
                and attachment.domain is shared.domain
                and attachment.owned_handle is attempt.owned_pidfd
                and attachment.adapter is attempt.capture_adapter
                and attachment.capture is attempt.capture_handle
                and attachment.binding is shared.binding
                and attachment.pre_fork_enrollment
                is shared.pre_fork_enrollment
                and attachment.owner_thread is attempt.owner_thread
                and attachment.owner_thread is threading.current_thread()
                and type(attachment.state) is str
                and attachment.state == "ATTACHED_HARD_LIMIT_ONLY"
                and type(attachment.cleanup_deadline_bound) is bool
                and attachment.cleanup_deadline_bound is False
                and type(attachment.used) is bool
                and attachment.used is binding_phase
                and type(attachment._sealed) is bool
                and attachment._sealed is True
                and _strict_allocation_authority(attempt.handle)
                is attachment.allocation_authority,
                "allocated attachment continuation changed")
        require(type(shared).__dict__.get("_validate_forked")
                is _SHARED_FORK_VALIDATE_METHOD
                and type(shared.__dict__) is dict
                and all(type(key) is str for key in shared.__dict__)
                and "_validate_forked" not in shared.__dict__,
                "allocated shared validator surface changed")
        _SHARED_FORK_VALIDATE_METHOD(
            shared, attempt, expected_attempt_state,
            expected_shared_state=(
                "DEADLINE_BINDING" if binding_phase
                else "ATTACHED_HARD_LIMIT_ONLY"),
            expected_used=True,
            expected_domain_state="LAUNCHER_CLAIMED",
            expected_lifecycle_state=(
                "OWNED_LEADER_REAPED_ONLY" if reaped else "PIDFD_BOUND"))
        origin = attachment.domain_origin
        require(type(origin) is DESC._LauncherOrigin
                and type(origin.__dict__) is dict
                and all(type(key) is str for key in origin.__dict__)
                and set(origin.__dict__) == {"seal", "leader", "used"}
                and type(origin.leader) is dict
                and all(type(key) is str for key in origin.leader)
                and set(origin.leader) == {"pid", "starttime", "boot_id"}
                and type(origin.leader["pid"]) is int
                and origin.leader["pid"] > 0
                and type(origin.leader["starttime"]) is int
                and origin.leader["starttime"] > 0
                and type(origin.leader["boot_id"]) is str
                and origin.seal is attachment.domain._seal
                and type(origin.used) is bool and origin.used is True,
                "allocated launcher origin changed")
        GRAPH.CONSUMER._builtin_tree(
            origin.leader, "allocated attachment launcher leader")
        require(origin.leader == attachment.domain.expected_leader
                and adapter.descendant_binding is attachment.binding
                and adapter.descendant_consumer is attachment.capture,
                "allocated attachment backlinks changed")
        return attachment
    except BaseException:
        if type(shared) is GRAPH.AllocatedDescendantEnrollment:
            shared._poison()
            shared.graph._poison()
        if type(adapter) is OWNED._CaptureChildAdapter:
            adapter._poison("UNKNOWN_DESCENDANT_ATTACHMENT_CONTINUITY")
        attempt._poison("DESCENDANT_ATTACHMENT_CONTINUITY")
        raise


def _validate_deadline_binding_preimage(
        attachment, settlement, descendant, receipt_bytes,
        cleanup_started, deadline, hard_limit, finished_ns,
        attempt_watermark, domain_watermark, monitor_capability,
        monitor_completed_ns):
    attempt = attachment.attempt
    domain = attachment.domain
    receipt = settlement.receipt
    monitor = attempt.monitor_capability
    require(type(cleanup_started) is int
            and type(deadline) is int and type(hard_limit) is int
            and type(finished_ns) is int
            and type(attempt_watermark) is int
            and type(domain_watermark) is int
            and type(monitor_completed_ns) is int
            and type(attempt.cleanup_started_ns) is int
            and attempt.cleanup_started_ns == cleanup_started
            and type(attempt.cleanup_deadline_ns) is int
            and attempt.cleanup_deadline_ns == deadline
            and type(attempt.last_clock_ns) is int
            and attempt.last_clock_ns == attempt_watermark
            and type(domain.deadline_ns) is int
            and domain.deadline_ns == hard_limit
            and type(domain.last_clock_ns) is int
            and domain.last_clock_ns == domain_watermark,
            "allocated deadline scalar preimage changed")
    require(type(settlement.__dict__) is dict
            and all(type(key) is str for key in settlement.__dict__)
            and set(settlement.__dict__) == {
                "receipt", "child_adapter", "deadline_ns",
                "leader_reaped", "owner", "probe_used", "owner_thread",
                "capture_handle", "normal_completion_capability",
                "descendant_capability"}
            and type(receipt) is dict
            and all(type(key) is str for key in receipt)
            and type(settlement.owner) is dict
            and all(type(key) is str for key in settlement.owner)
            and type(attempt.settlement_receipt) is dict
            and all(type(key) is str
                    for key in attempt.settlement_receipt)
            and type(descendant) is CAPTURE._DescendantSettlementCapability
            and monitor is monitor_capability
            and type(monitor) is CAPTURE._NormalCompletionCapability
            and type(monitor.completed_at_ns) is int
            and monitor.completed_at_ns == monitor_completed_ns
            and monitor_completed_ns <= cleanup_started
            and type(monitor.completion) is dict
            and all(type(key) is str for key in monitor.completion)
            and type(descendant.__dict__) is dict
            and all(type(key) is str for key in descendant.__dict__)
            and set(descendant.__dict__) == {
                "capture_handle", "child_adapter", "primary_failure",
                "primary_completion", "settlement_kind",
                "normal_completion_capability", "finished_ns",
                "deadline_ns", "owner", "owner_thread", "used"},
            "strict allocated deadline capability containers")
    GRAPH.CONSUMER._builtin_tree(
        receipt, "allocated deadline live settlement")
    GRAPH.CONSUMER._builtin_tree(
        settlement.owner, "allocated deadline settlement owner")
    GRAPH.CONSUMER._builtin_tree(
        attempt.settlement_receipt,
        "allocated deadline frozen settlement")
    GRAPH.CONSUMER._builtin_tree(
        descendant.primary_completion,
        "allocated deadline descendant completion")
    GRAPH.CONSUMER._builtin_tree(
        monitor.completion, "allocated deadline monitor completion")
    GRAPH.CONSUMER._builtin_tree(
        descendant.owner, "allocated deadline descendant owner")
    require(ABORT.SUP.canonical(receipt) == receipt_bytes
            and ABORT.SUP.canonical(attempt.settlement_receipt)
            == receipt_bytes
            and settlement is attempt.settlement_handle
            and settlement.capture_handle is attachment.capture
            and settlement.child_adapter is attachment.adapter
            and settlement.normal_completion_capability
            is monitor
            and settlement.descendant_capability is descendant
            and type(settlement.deadline_ns) is int
            and settlement.deadline_ns == deadline
            and type(settlement.leader_reaped) is bool
            and settlement.leader_reaped is True
            and type(settlement.probe_used) is bool
            and settlement.probe_used is False
            and settlement.owner_thread is attempt.owner_thread
            and ABORT.SUP.canonical(settlement.owner)
            == attempt._owner_bytes
            and ABORT.SUP.canonical(receipt["owner"])
            == attempt._owner_bytes
            and descendant.capture_handle is attachment.capture
            and descendant.child_adapter is attachment.adapter
            and descendant.normal_completion_capability
            is monitor
            and type(descendant.primary_failure) is type(None)
            and type(descendant.settlement_kind) is str
            and descendant.settlement_kind == "NORMAL_COMPLETION"
            and ABORT.SUP.canonical(descendant.primary_completion)
            == attempt._monitor_result_bytes
            and ABORT.SUP.canonical(monitor.completion)
            == attempt._monitor_result_bytes
            and type(descendant.finished_ns) is int
            and descendant.finished_ns == finished_ns
            and type(descendant.deadline_ns) is int
            and descendant.deadline_ns == deadline
            and descendant.owner_thread is attempt.owner_thread
            and ABORT.SUP.canonical(descendant.owner)
            == attempt._owner_bytes
            and type(descendant.used) is bool
            and descendant.used is False
            and receipt["cleanup_deadline_ns"] == deadline
            and receipt["started_ns"] == cleanup_started
            and receipt["finished_ns"] == finished_ns,
            "allocated deadline capability preimage changed")
    return True


def bind_allocated_settlement_deadline(attachment, settlement, clock):
    """Narrow the hard limit once; mint no descendant-drain authority."""
    require(type(attachment) is AllocatedOwnedDomainAttachment
            and type(settlement) is CAPTURE.SettlementHandle,
            "exact allocated deadline binding inputs required")
    attempt = attachment.attempt
    shared = attachment.shared
    adapter = attachment.adapter
    domain = attachment.domain
    try:
        require(type(attempt) is ABORT.AbortForkAttempt
                and attempt.descendant_deadline_binding_started is False
                and attempt.descendant_deadline_binding is None
                and attempt.settlement_handle is settlement
                and attempt.state == "LINUX_LEADER_REAPED_UNQUALIFIED"
                and attempt.poisoned is False,
                "fresh allocated settlement deadline binding required")
        validate_allocated_attachment_phase(
            attachment, attempt, "LINUX_LEADER_REAPED_UNQUALIFIED",
            reaped=True)
        require(type(attempt).__dict__.get("_validate_linux_reaped_chain")
                is _REAPED_CHAIN_VALIDATE_METHOD
                and type(attempt).__dict__.get("_set")
                is _ATTEMPT_SET_METHOD
                and type(attempt.__dict__) is dict
                and all(type(key) is str for key in attempt.__dict__)
                and "_validate_linux_reaped_chain" not in attempt.__dict__
                and "_set" not in attempt.__dict__,
                "allocated reaped validator surface changed")
        request = GRAPH._strict_bound_request(attempt.persisted.bound)
        cleanup_ms = request["cleanup_ms"]
        require(type(cleanup_ms) is int and 1 <= cleanup_ms <= 10000,
                "strict allocated cleanup budget")
        budget_ns = cleanup_ms * 1000000
        cleanup_started = attempt.cleanup_started_ns
        require(type(attempt.deadline_ns) is int
                and type(cleanup_started) is int,
                "strict allocated deadline arithmetic inputs")
        hard_limit = attempt.deadline_ns + budget_ns
        deadline = min(cleanup_started + budget_ns, hard_limit)
        receipt = settlement.receipt
        descendant = settlement.descendant_capability
        require(type(cleanup_started) is int
                and type(hard_limit) is int
                and type(deadline) is int
                and hard_limit == shared.cleanup_hard_limit_ns
                and domain.deadline_ns == hard_limit
                and type(receipt) is dict
                and all(type(key) is str for key in receipt)
                and type(descendant)
                is CAPTURE._DescendantSettlementCapability
                and type(descendant.used) is bool
                and descendant.used is False
                and type(settlement.probe_used) is bool
                and settlement.probe_used is False,
                "allocated settlement deadline preimage changed")
        GRAPH.CONSUMER._builtin_tree(
            receipt, "allocated deadline settlement receipt")
        receipt_bytes = ABORT.SUP.canonical(receipt)
        finished_ns = receipt["finished_ns"]
        attempt_watermark = attempt.last_clock_ns
        domain_watermark = domain.last_clock_ns
        monitor = attempt.monitor_capability
        require(type(finished_ns) is int
                and type(attempt_watermark) is int
                and type(domain_watermark) is int
                and type(monitor) is CAPTURE._NormalCompletionCapability
                and type(monitor.completed_at_ns) is int,
                "strict allocated deadline watermarks")
        monitor_completed = monitor.completed_at_ns
        _validate_deadline_binding_preimage(
            attachment, settlement, descendant, receipt_bytes,
            cleanup_started, deadline, hard_limit, finished_ns,
            attempt_watermark, domain_watermark, monitor,
            monitor_completed)
        require(receipt_bytes == attempt._settlement_receipt_bytes
                and settlement.deadline_ns == deadline
                and attempt.cleanup_deadline_ns == deadline
                and receipt["cleanup_deadline_ns"] == deadline
                and receipt["started_ns"] == cleanup_started
                and type(receipt["finished_ns"]) is int
                and monitor_completed
                <= cleanup_started <= finished_ns < deadline
                and deadline <= hard_limit
                and settlement.capture_handle is attachment.capture
                and settlement.child_adapter is adapter
                and settlement.normal_completion_capability
                is attempt.monitor_capability
                and descendant.capture_handle is attachment.capture
                and descendant.child_adapter is adapter
                and descendant.deadline_ns == deadline
                and descendant.finished_ns == receipt["finished_ns"]
                and descendant.owner_thread is attempt.owner_thread,
                "allocated settlement deadline evidence changed")
        object.__setattr__(attachment, "used", True)
        object.__setattr__(shared, "state", "DEADLINE_BINDING")
        _ATTEMPT_SET_METHOD(
            attempt, "descendant_deadline_binding_started", True)
        validate_allocated_attachment_phase(
            attachment, attempt, "LINUX_LEADER_REAPED_UNQUALIFIED",
            reaped=True, binding_phase=True)
        _REAPED_CHAIN_VALIDATE_METHOD(
            attempt, "LINUX_LEADER_REAPED_UNQUALIFIED")
        now = clock.monotonic_ns()
        require(type(now) is int,
                "strict allocated deadline bind clock")
        _validate_deadline_binding_preimage(
            attachment, settlement, descendant, receipt_bytes,
            cleanup_started, deadline, hard_limit, finished_ns,
            attempt_watermark, domain_watermark, monitor,
            monitor_completed)
        _REAPED_CHAIN_VALIDATE_METHOD(
            attempt, "LINUX_LEADER_REAPED_UNQUALIFIED")
        validate_allocated_attachment_phase(
            attachment, attempt, "LINUX_LEADER_REAPED_UNQUALIFIED",
            reaped=True, binding_phase=True)
        _validate_deadline_binding_preimage(
            attachment, settlement, descendant, receipt_bytes,
            cleanup_started, deadline, hard_limit, finished_ns,
            attempt_watermark, domain_watermark, monitor,
            monitor_completed)
        require(now >= finished_ns
                and now >= attempt_watermark
                and now >= domain_watermark
                and now < deadline
                and settlement.descendant_capability is descendant
                and descendant.used is False
                and settlement.probe_used is False
                and ABORT.SUP.canonical(settlement.receipt) == receipt_bytes
                and domain.deadline_ns == hard_limit
                and domain.last_clock_ns
                == shared.domain_prepared_watermark_ns,
                "allocated deadline authority changed after clock")
        domain.deadline_ns = deadline
        domain.last_clock_ns = now
        require(type(domain.deadline_ns) is int
                and domain.deadline_ns == deadline
                and deadline <= hard_limit
                and domain.last_clock_ns == now
                and domain.state == "LAUNCHER_CLAIMED"
                and domain.leader is None
                and domain.records == []
                and domain.pending is None
                and domain.terminal_echild is None
                and settlement.descendant_capability is descendant
                and descendant.used is False
                and settlement.probe_used is False
                and ABORT.SUP.canonical(settlement.receipt) == receipt_bytes,
                "allocated deadline narrowing postcondition changed")
        successor = AllocatedDeadlineBoundAttachment(
            _ALLOCATED_ATTACH_KEY, attachment, settlement, descendant,
            deadline, hard_limit, now, receipt_bytes)
        _ATTEMPT_SET_METHOD(
            attempt, "descendant_deadline_binding", successor)
        object.__setattr__(shared, "state",
                           "DEADLINE_BOUND_NON_DRAINABLE")
        require(attempt.descendant_deadline_binding is successor
                and successor.source_attachment is attachment
                and successor.deadline_ns == domain.deadline_ns
                and successor.descendant_capability is descendant
                and successor.used is False,
                "allocated deadline binding publication changed")
        return successor
    except BaseException:
        if type(shared) is GRAPH.AllocatedDescendantEnrollment:
            shared._poison()
            shared.graph._poison()
        if type(adapter) is OWNED._CaptureChildAdapter:
            adapter._poison("UNKNOWN_DESCENDANT_DEADLINE_BINDING")
        if type(attempt) is ABORT.AbortForkAttempt:
            attempt._poison("DESCENDANT_DEADLINE_BINDING")
        raise


def advance_descendant_domain(bridge, settlement, syscalls):
    """Transfer exact reap authority once, then perform at most one domain step."""
    try:
        require(type(bridge) is DescendantBridge, "exact bridge required")
        bridge._enter(("ATTACHED", "DRAINING"))
        if bridge.settlement is None:
            require(type(settlement) is CAPTURE.SettlementHandle
                    and settlement.capture_handle is bridge.capture
                    and settlement.child_adapter is bridge.adapter
                    and settlement.owner_thread is bridge.owner_thread
                    and settlement.leader_reaped is True
                    and settlement.probe_used is False
                    and settlement.deadline_ns == bridge.domain.deadline_ns
                    and settlement.receipt["cleanup_outcome"]
                    == "LEADER_REAPED_STREAMS_EOF"
                    and settlement.receipt.get("settlement_kind")
                    in ("PRIMARY_FAILURE", "NORMAL_COMPLETION"),
                    "exact completed settlement required")
            settlement_cap = settlement.descendant_capability
            require(type(settlement_cap) is CAPTURE._DescendantSettlementCapability
                    and settlement_cap.used is False
                    and settlement_cap.capture_handle is bridge.capture
                    and settlement_cap.child_adapter is bridge.adapter
                    and settlement_cap.owner == bridge.domain.owner
                    and settlement_cap.owner_thread is bridge.owner_thread
                    and type(settlement_cap.finished_ns) is int
                    and settlement_cap.finished_ns >= 0
                    and settlement_cap.deadline_ns == bridge.domain.deadline_ns
                    and settlement_cap.primary_failure
                    == bridge.capture.primary_failure
                    and settlement_cap.primary_completion
                    == settlement.receipt.get("primary_completion")
                    and settlement.receipt.get("primary_failure")
                    == settlement_cap.primary_failure
                    and ((settlement_cap.settlement_kind == "PRIMARY_FAILURE"
                          and settlement_cap.primary_failure is not None
                          and settlement_cap.primary_completion is None)
                         or (settlement_cap.settlement_kind == "NORMAL_COMPLETION"
                             and settlement_cap.primary_failure is None
                             and settlement_cap.primary_completion is not None)),
                    "frozen settlement evidence mismatch")
            if settlement_cap.settlement_kind == "NORMAL_COMPLETION":
                CAPTURE.validate_transferred_normal_completion(
                    settlement, bridge.capture, bridge.adapter)
            bridge.settlement = settlement
            bridge.primary_failure = copy.deepcopy(
                settlement_cap.primary_failure)
            bridge.primary_completion = copy.deepcopy(
                settlement_cap.primary_completion)
            bridge.settlement_evidence = {
                "finished_ns": settlement_cap.finished_ns,
                "deadline_ns": settlement_cap.deadline_ns,
                "leader_reaped_streams_eof": True}
            now = syscalls.monotonic_ns()
            require(type(now) is int and now >= 0
                    and bridge.state == "ADVANCING"
                    and now >= settlement_cap.finished_ns
                    and now < bridge.domain.deadline_ns,
                    "descendant handoff deadline")
            DESC.advance_bridge_clock_watermark(
                DESC._BRIDGE_KEY, bridge.domain, now)
            claimed_settlement = CAPTURE.take_descendant_settlement_capability(
                settlement, bridge.capture, bridge.adapter)
            require(claimed_settlement is settlement_cap,
                    "settlement capability changed")
            capability = OWNED.take_exact_leader_reap(
                bridge.adapter, bridge.enrollment.binding, bridge.capture)
            require(bridge.state == "ADVANCING"
                    and type(capability) is OWNED._ExactLeaderReapCapability
                    and capability.binding is bridge.enrollment.binding
                    and capability.adapter is bridge.adapter
                    and capability.handle is bridge.owned_handle
                    and capability.fork_origin
                    is bridge.owned_handle.lifecycle["origin"]
                    and capability.receipt is bridge.adapter.cached
                    and capability.owner_thread is bridge.owner_thread
                    and capability.used is False,
                    "opaque leader reap capability mismatch")
            capability.used = True
            claim = DESC._leader_reap_claim(
                DESC._LEADER_KEY, bridge.domain, bridge.domain_origin)
            require(capability.leader == claim.leader,
                    "leader capability/domain claim mismatch")
            bridge.domain.accept_leader_reap(claim)
            bridge.settlement_capability = claimed_settlement
            bridge.leader_capability = capability
        else:
            require(settlement is bridge.settlement,
                    "settlement changed during descendant drain")
        bridge._validate_transferred_chain()
        progress = bridge.domain.step(syscalls)
        bridge.progress = copy.deepcopy(progress)
        require(bridge.state == "ADVANCING", "bridge state changed during step")
        bridge._validate_transferred_chain()
        if progress["outcome"] == "MODEL_CHILD_DOMAIN_DRAINED":
            bridge._validate_transferred_chain()
            bridge.state = "DOMAIN_DRAINED"
        elif progress["outcome"] in (
                "CHILDREN_PENDING", "ADOPTED_CHILD_REAPED_CONTINUE"):
            bridge.state = "DRAINING"
        else:
            bridge._poison()
        return bridge.snapshot()
    except BaseException as exc:
        if type(bridge) is DescendantBridge:
            bridge._poison(type(exc).__name__)
            return bridge.snapshot()
        raise
