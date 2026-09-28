#!/usr/bin/python3
"""Inert, source-staged child-domain accounting; no CLI or live wiring.

The primitive models one fresh Linux subreaper.  P_ALL is discovery only;
terminal children are pinned and reaped through P_PIDFD.  Its strongest result
is MODEL_CHILD_DOMAIN_DRAINED and never authorizes runtime or storage work.
"""

import copy
import errno
import os
import re
import signal
import threading


LINUX_WAIT_WALL = 0x40000000
WAIT_DISCOVERY_OPTIONS = os.WEXITED | os.WNOHANG | os.WNOWAIT | LINUX_WAIT_WALL
WAIT_PIDFD_OBSERVE = os.WEXITED | os.WNOHANG | os.WNOWAIT | LINUX_WAIT_WALL
WAIT_PIDFD_REAP = os.WEXITED | os.WNOHANG | LINUX_WAIT_WALL
MAX_ADOPTED_RECORDS = 64

_DOMAIN_KEY = object()
_LEADER_KEY = object()
_RECEIPT_KEY = object()
_BRIDGE_KEY = object()


class Refusal(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise Refusal(message)


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _identity(value, label, ppid=False):
    keys = {"pid", "starttime", "boot_id"} | ({"ppid"} if ppid else set())
    require(type(value) is dict and set(value) == keys, label + " schema")
    require(_integer(value["pid"], 1) and _integer(value["starttime"], 1),
            label + " numeric identity")
    require(type(value["boot_id"]) is str and re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        value["boot_id"]) is not None, label + " boot identity")
    if ppid:
        require(_integer(value["ppid"], 1), label + " parent identity")


def _terminal(value, expected_pid=None):
    require(value is not None and _integer(value.si_pid, 1)
            and type(value.si_code) is int and type(value.si_status) is int,
            "terminal wait receipt")
    require(value.si_code in (os.CLD_EXITED, os.CLD_KILLED, os.CLD_DUMPED),
            "nonterminal wait receipt")
    if value.si_code == os.CLD_EXITED:
        require(0 <= value.si_status <= 255, "invalid exit status")
    else:
        require(1 <= value.si_status < signal.NSIG, "invalid signal status")
    if expected_pid is not None:
        require(value.si_pid == expected_pid, "terminal PID mismatch")
    return {"pid": value.si_pid, "code": value.si_code,
            "status": value.si_status}


class _LauncherOrigin:
    def __init__(self, key, seal, leader):
        require(key is _LEADER_KEY, "launcher origin construction is private")
        self.seal = seal
        self.leader = copy.deepcopy(leader)
        self.used = False

    def __copy__(self):
        raise Refusal("launcher origin is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("launcher origin is noncopyable")


class LeaderReapClaim:
    def __init__(self, key, seal, leader):
        require(key is _LEADER_KEY, "leader claim construction is private")
        self.seal = seal
        self.leader = copy.deepcopy(leader)
        self.used = False

    def __copy__(self):
        raise Refusal("leader reap claim is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("leader reap claim is noncopyable")


def _launcher_origin(key, domain, leader):
    require(key is _LEADER_KEY and type(domain) is ChildDomain,
            "launcher origin construction is private")
    _identity(leader, "leader")
    require(leader["boot_id"] == domain.owner["boot_id"]
            and leader["pid"] != domain.owner["pid"], "invalid leader identity")
    return _LauncherOrigin(key, domain._seal, leader)


def _leader_reap_claim(key, domain, origin):
    """Future bridge-only constructor; tests use it without granting authority."""
    require(key is _LEADER_KEY and type(domain) is ChildDomain,
            "leader claim construction is private")
    require(type(origin) is _LauncherOrigin and origin.used
            and origin.seal is domain._seal
            and domain.expected_leader == origin.leader,
            "leader claim does not match launcher origin")
    return LeaderReapClaim(key, domain._seal, origin.leader)


class ChildDomain:
    def __init__(self, key, owner, request_id, domain_id, deadline_ns):
        require(key is _DOMAIN_KEY, "child domain construction is private")
        self.owner = copy.deepcopy(owner)
        self.request_id = request_id
        self.domain_id = domain_id
        self.deadline_ns = deadline_ns
        self.owner_thread = threading.current_thread()
        self._seal = object()
        self.state = "PREPARED"
        self.launcher_claimed = False
        self.leader = None
        self.expected_leader = None
        self.records = []
        self.pending = None
        self.terminal_echild = None
        self.sequence = 0
        self.last_clock_ns = -1
        self.bridge_enrollment = None

    def __copy__(self):
        raise Refusal("child domain is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("child domain is noncopyable")

    def claim_launcher(self, origin):
        try:
            self._mutation_entry("PREPARED")
            require(not self.launcher_claimed, "launcher claim unavailable")
            require(type(origin) is _LauncherOrigin and not origin.used
                    and origin.seal is self._seal,
                    "foreign or reused launcher origin")
            _identity(origin.leader, "launcher leader")
            origin.used = True
            self.launcher_claimed = True
            self.expected_leader = copy.deepcopy(origin.leader)
            self.state = "LAUNCHER_CLAIMED"
        except BaseException:
            self.state = "UNKNOWN"
            raise

    def accept_leader_reap(self, claim):
        try:
            self._mutation_entry("LAUNCHER_CLAIMED")
            require(self.launcher_claimed, "leader reap is out of phase")
            require(type(claim) is LeaderReapClaim and not claim.used
                    and claim.seal is self._seal
                    and claim.leader == self.expected_leader,
                    "foreign or reused leader reap claim")
            _identity(claim.leader, "leader")
            claim.used = True
            self.leader = copy.deepcopy(claim.leader)
            self.state = "DRAINING"
        except BaseException:
            self.state = "UNKNOWN"
            raise

    def _mutation_entry(self, expected_state):
        if (threading.current_thread() is not self.owner_thread
                or self.state != expected_state):
            self.state = "UNKNOWN"
            raise Refusal("foreign, reentrant or out-of-phase mutation")

    def _local_owner(self):
        require(threading.current_thread() is self.owner_thread,
                "foreign owner thread")

    def _continuity(self, syscalls, expected_state):
        self._local_owner()
        require(self.state == expected_state, "child-domain reentrancy or poison")
        current = syscalls.owner_identity()
        require(self.state == expected_state, "owner callback reentrancy")
        _identity(current, "observed owner")
        require(current == self.owner, "owner identity changed")
        tasks = syscalls.kernel_thread_ids()
        require(self.state == expected_state, "thread callback reentrancy")
        require(type(tasks) is list and len(tasks) == 1
                and type(tasks[0]) is int and tasks[0] == self.owner["pid"],
                "supervisor is not a single kernel thread")
        policy = syscalls.sigchld_policy()
        require(self.state == expected_state, "SIGCHLD callback reentrancy")
        require(type(policy) is dict and set(policy) == {
            "handler_default", "ignored", "no_cldwait"}, "SIGCHLD policy schema")
        require(all(type(value) is bool for value in policy.values()),
                "SIGCHLD policy types")
        require(policy == {"handler_default": True, "ignored": False,
                           "no_cldwait": False}, "SIGCHLD policy changed")
        subreaper = syscalls.get_subreaper()
        require(self.state == expected_state, "subreaper callback reentrancy")
        require(type(subreaper) is int and subreaper == 1,
                "subreaper state changed")

    def _clock(self, syscalls, expected_state):
        now = syscalls.monotonic_ns()
        require(self.state == expected_state, "clock callback reentrancy")
        require(_integer(now), "monotonic clock")
        require(now >= self.last_clock_ns, "monotonic clock regressed")
        self.last_clock_ns = now
        require(now < self.deadline_ns, "child-domain deadline reached")
        return now

    def _unknown(self, reason):
        self.state = "UNKNOWN"
        return self._receipt(_RECEIPT_KEY, "UNKNOWN", reason)

    def _receipt(self, key, outcome, reason=None):
        require(key is _RECEIPT_KEY, "receipt construction is private")
        return {"schema": 1, "request_id": self.request_id,
                "domain_id": self.domain_id, "owner": copy.deepcopy(self.owner),
                "wait_domain": "P_ALL_WEXITED_WNOHANG_WNOWAIT___WALL",
                "leader": copy.deepcopy(self.leader),
                "adopted_records": copy.deepcopy(self.records),
                "pending_adopted": copy.deepcopy(self.pending),
                "terminal_echild": copy.deepcopy(self.terminal_echild),
                "outcome": outcome, "reason": reason,
                "runtime_authorized": False, "storage_authorized": False,
                "postcondition_verified": False}

    def snapshot(self):
        outcomes = {"PREPARED": "PREPARED", "LAUNCHER_CLAIMED": "LEADER_PENDING",
                    "DRAINING": "CHILDREN_PENDING", "STEPPING": "UNKNOWN_BUSY",
                    "DRAINED": "MODEL_CHILD_DOMAIN_DRAINED", "UNKNOWN": "UNKNOWN",
                    "UNKNOWN_PREPARATION": "UNKNOWN"}
        return self._receipt(_RECEIPT_KEY, outcomes.get(self.state, "UNKNOWN"))

    def step(self, syscalls):
        """Perform at most one discovery/bind/reap cycle."""
        try:
            require(self.state == "DRAINING", "child domain is not drainable")
            self.state = "STEPPING"
            self._continuity(syscalls, "STEPPING")
            self._clock(syscalls, "STEPPING")
            try:
                found = syscalls.wait_all_wall_wnowait(WAIT_DISCOVERY_OPTIONS)
            except ChildProcessError as exc:
                require(exc.errno == errno.ECHILD, "unexpected wait-all error")
                require(self.state == "STEPPING", "wait-all callback reentrancy")
                self._continuity(syscalls, "STEPPING")
                now = self._clock(syscalls, "STEPPING")
                self.terminal_echild = {"errno": errno.ECHILD, "observed_ns": now,
                                        "options": WAIT_DISCOVERY_OPTIONS}
                self.state = "DRAINED"
                return self._receipt(_RECEIPT_KEY, "MODEL_CHILD_DOMAIN_DRAINED")
            require(self.state == "STEPPING", "wait-all callback reentrancy")
            discovered = None if found is None else _terminal(found)
            self._continuity(syscalls, "STEPPING")
            self._clock(syscalls, "STEPPING")
            if discovered is None:
                self.state = "DRAINING"
                return self._receipt(_RECEIPT_KEY, "CHILDREN_PENDING")
            require(len(self.records) < MAX_ADOPTED_RECORDS,
                    "adopted-child record limit reached")
            self.pending = {"discovery": copy.deepcopy(discovered),
                            "identity": None, "pidfd_opened": False,
                            "pidfd_observed": False, "reap_attempted": False,
                            "reap_confirmed": False, "pidfd_close_confirmed": False}
            first = syscalls.child_identity(discovered["pid"])
            require(self.state == "STEPPING", "identity callback reentrancy")
            first = copy.deepcopy(first)
            _identity(first, "adopted child", ppid=True)
            require(first["pid"] == discovered["pid"]
                    and first["boot_id"] == self.owner["boot_id"]
                    and first["ppid"] == self.owner["pid"],
                    "adopted child identity mismatch")
            self.pending["identity"] = copy.deepcopy(first)
            pidfd = None
            try:
                pidfd = syscalls.pidfd_open(discovered["pid"])
                if _integer(pidfd):
                    self.pending["pidfd_opened"] = True
                require(_integer(pidfd), "invalid adopted-child pidfd")
                require(self.state == "STEPPING", "pidfd-open callback reentrancy")
                second = syscalls.child_identity(discovered["pid"])
                require(self.state == "STEPPING", "second identity callback reentrancy")
                second = copy.deepcopy(second)
                _identity(second, "second adopted child", ppid=True)
                require(second == first, "adopted child identity changed")
                pinned_raw = syscalls.wait_pidfd(pidfd, WAIT_PIDFD_OBSERVE)
                require(self.state == "STEPPING", "pidfd-observe callback reentrancy")
                pinned = _terminal(pinned_raw, discovered["pid"])
                require(pinned == discovered, "P_ALL/P_PIDFD receipt mismatch")
                self.pending["pidfd_observed"] = True
                self._continuity(syscalls, "STEPPING")
                self._clock(syscalls, "STEPPING")
                self.pending["reap_attempted"] = True
                reaped_raw = syscalls.wait_pidfd(pidfd, WAIT_PIDFD_REAP)
                require(self.state == "STEPPING", "pidfd-reap callback reentrancy")
                reaped = _terminal(reaped_raw, discovered["pid"])
                require(reaped == discovered, "adopted-child reap mismatch")
                self.pending["reap_confirmed"] = True
                self._continuity(syscalls, "STEPPING")
                self._clock(syscalls, "STEPPING")
            finally:
                if _integer(pidfd):
                    syscalls.close_pidfd(pidfd)
                    self.pending["pidfd_close_confirmed"] = True
                    require(self.state == "STEPPING",
                            "pidfd-close callback reentrancy")
            self._continuity(syscalls, "STEPPING")
            self._clock(syscalls, "STEPPING")
            self.sequence += 1
            self.records.append({"sequence": self.sequence,
                                 "identity": copy.deepcopy(first),
                                 "terminal": discovered,
                                 "exact_pidfd_reap": True})
            self.pending = None
            self.state = "DRAINING"
            return self._receipt(_RECEIPT_KEY, "ADOPTED_CHILD_REAPED_CONTINUE")
        except BaseException as exc:
            return self._unknown(type(exc).__name__)


def prepare_child_domain(owner, request_id, domain_id, deadline_ns, syscalls):
    """Prepare a fresh all-child domain before the only permitted fork."""
    _identity(owner, "owner")
    require(type(request_id) is str and 1 <= len(request_id) <= 128,
            "request id")
    require(type(domain_id) is str and re.fullmatch(r"[0-9a-f]{32}", domain_id),
            "domain id")
    require(_integer(deadline_ns, 1), "child-domain deadline")
    domain = ChildDomain(_DOMAIN_KEY, owner, request_id, domain_id, deadline_ns)
    try:
        current = syscalls.owner_identity()
        _identity(current, "observed owner")
        require(current == owner, "owner identity mismatch")
        tasks = syscalls.kernel_thread_ids()
        require(type(tasks) is list and len(tasks) == 1
                and type(tasks[0]) is int and tasks[0] == owner["pid"],
                "supervisor is not a single kernel thread")
        policy = syscalls.sigchld_policy()
        require(type(policy) is dict and set(policy) == {
            "handler_default", "ignored", "no_cldwait"}
                and all(type(value) is bool for value in policy.values()),
                "SIGCHLD policy schema")
        require(policy == {"handler_default": True, "ignored": False,
                           "no_cldwait": False}, "unsafe SIGCHLD policy")
        initial = syscalls.get_subreaper()
        require(type(initial) is int and initial == 0,
                "scope is already a subreaper")
        for phase in ("before", "after"):
            try:
                baseline = syscalls.wait_all_wall_wnowait(WAIT_DISCOVERY_OPTIONS)
            except ChildProcessError as exc:
                require(exc.errno == errno.ECHILD, phase + " baseline wait error")
            else:
                require(False, phase + " baseline child domain is not empty")
            if phase == "before":
                syscalls.set_subreaper(1)
                readback = syscalls.get_subreaper()
                require(type(readback) is int and readback == 1,
                        "subreaper SET/readback failed")
        domain._continuity(syscalls, "PREPARED")
        domain._clock(syscalls, "PREPARED")
        return domain
    except BaseException:
        domain.state = "UNKNOWN_PREPARATION"
        raise


def reserve_bridge_enrollment(key, domain, binding):
    """Reserve one pre-fork bridge binding; source-only internal API."""
    require(key is _BRIDGE_KEY and type(domain) is ChildDomain,
            "bridge enrollment construction is private")
    try:
        domain._mutation_entry("PREPARED")
        require(binding is not None and domain.bridge_enrollment is None,
                "bridge enrollment unavailable")
        domain.bridge_enrollment = binding
    except BaseException:
        domain.state = "UNKNOWN"
        raise


def poison_bridge_domain(key, domain):
    require(key is _BRIDGE_KEY and type(domain) is ChildDomain,
            "bridge poison authority")
    domain.state = "UNKNOWN"


def advance_bridge_clock_watermark(key, domain, observed_ns):
    require(key is _BRIDGE_KEY and type(domain) is ChildDomain,
            "bridge clock authority")
    require(domain.state == "LAUNCHER_CLAIMED"
            and _integer(observed_ns)
            and observed_ns >= domain.last_clock_ns
            and observed_ns < domain.deadline_ns,
            "bridge clock watermark")
    domain.last_clock_ns = observed_ns
