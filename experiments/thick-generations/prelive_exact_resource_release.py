#!/usr/bin/python3
"""Source/mock exact-FD release model; no live syscall backend."""

import copy
import re
import threading
import types


_BIND_KEY = object()
HEX64 = re.compile(r"[0-9a-f]{64}")


class Refusal(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise Refusal(message)


def _owner(value):
    require(type(value) is dict
            and all(type(key) is str for key in value)
            and set(value) == {"pid", "starttime", "boot_id"}
            and type(value["pid"]) is int and value["pid"] > 0
            and type(value["starttime"]) is int
            and value["starttime"] > 0
            and type(value["boot_id"]) is str
            and re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                value["boot_id"]),
            "strict resource owner")
    return copy.deepcopy(value)


class ExactFDReleaseBinding:
    """Opaque primary/anchor OFD binding with one-shot primary release."""

    def __init__(self, key, owner, resource_kind, primary_fd, anchor_fd,
                 authority, authority_digest, deadline_ns):
        require(key is _BIND_KEY, "private exact resource binding")
        require(type(resource_kind) is str and resource_kind == "PIDFD"
                and type(primary_fd) is int and primary_fd >= 3
                and type(anchor_fd) is int and anchor_fd >= 3
                and primary_fd != anchor_fd
                and authority is not None
                and type(authority_digest) is str
                and HEX64.fullmatch(authority_digest)
                and type(deadline_ns) is int and deadline_ns > 0,
                "strict exact resource binding")
        self.owner = _owner(owner)
        self._owner_preimage = (owner["pid"], owner["starttime"],
                                owner["boot_id"])
        self.owner_thread = threading.current_thread()
        self.resource_kind = resource_kind
        self.primary_fd = primary_fd
        self.anchor_fd = anchor_fd
        self.authority = authority
        self.authority_digest = authority_digest
        self.deadline_ns = deadline_ns
        self.state = "BOUND_UNQUALIFIED"
        self.release_attempted = False
        self.reentrant_poisoned = False
        self.primary_owned = True
        self.anchor_owned = True
        self.close_confirmed = False
        self.receipt = None
        self._backend = None
        self._sealed = True

    _PROTECTED = frozenset(("owner", "owner_thread", "resource_kind",
        "_owner_preimage",
        "primary_fd", "anchor_fd", "authority", "authority_digest",
        "deadline_ns", "state", "release_attempted", "primary_owned",
        "reentrant_poisoned", "anchor_owned", "close_confirmed", "receipt", "_backend",
        "_sealed"))

    def __setattr__(self, name, value):
        if "_sealed" in self.__dict__ and name in type(self)._PROTECTED:
            raise Refusal("exact resource binding is immutable externally")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        if "_sealed" in self.__dict__:
            raise Refusal("exact resource binding cannot be deleted")
        object.__delattr__(self, name)

    def __copy__(self):
        raise Refusal("exact resource binding is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("exact resource binding is noncopyable")

    def _set(self, name, value):
        object.__setattr__(self, name, value)

    def _preflight(self, authority, authority_digest, state):
        require(threading.current_thread() is self.owner_thread
                and type(self.__dict__) is dict
                and all(type(key) is str for key in self.__dict__)
                and set(self.__dict__) == {"owner", "owner_thread",
                    "_owner_preimage",
                    "resource_kind", "primary_fd", "anchor_fd",
                    "authority", "authority_digest", "deadline_ns",
                    "state", "release_attempted", "primary_owned",
                    "reentrant_poisoned", "anchor_owned", "close_confirmed", "receipt",
                    "_backend", "_sealed"}
                and _owner(self.owner) == self.owner
                and type(self._owner_preimage) is tuple
                and len(self._owner_preimage) == 3
                and self._owner_preimage == (self.owner["pid"],
                    self.owner["starttime"], self.owner["boot_id"])
                and type(self.resource_kind) is str
                and self.resource_kind == "PIDFD"
                and type(self.primary_fd) is int and self.primary_fd >= 3
                and type(self.anchor_fd) is int and self.anchor_fd >= 3
                and self.primary_fd != self.anchor_fd
                and self.authority is authority
                and type(authority_digest) is str
                and authority_digest == self.authority_digest
                and HEX64.fullmatch(self.authority_digest)
                and type(self.deadline_ns) is int and self.deadline_ns > 0
                and type(self.state) is str and self.state == state
                and all(type(value) is bool for value in (
                    self.release_attempted, self.reentrant_poisoned,
                    self.primary_owned,
                    self.anchor_owned, self.close_confirmed))
                and self.reentrant_poisoned is False
                and self.receipt is None,
                "exact resource authority changed")

    def release_primary_once(self, authority, authority_digest, calls):
        if self.release_attempted:
            if self.state in ("VERIFYING_RESOURCE_BINDING",
                              "CLOSING_PRIMARY",
                              "FINALIZING_PRIMARY_CLOSE"):
                self._set("reentrant_poisoned", True)
                self._set("state", "RESOURCE_RELEASE_UNKNOWN")
            raise Refusal("exact primary release is one-shot")
        backend_type = type(calls)
        methods = ("owner_identity", "monotonic_ns", "single_threaded",
                   "same_open_file_description", "close")
        require(type(backend_type) is type,
                "custom exact resource backend metaclass forbidden")
        class_namespace = type.__getattribute__(backend_type, "__dict__")
        dict_descriptor = class_namespace.get("__dict__")
        require(type(dict_descriptor)
                is types.GetSetDescriptorType,
                "custom exact resource instance namespace forbidden")
        require(class_namespace.get("exact_resource_release_mock")
                is True
                and backend_type.__getattribute__ is object.__getattribute__
                and all(type(class_namespace.get(name))
                        is types.FunctionType for name in methods),
                "closed exact resource release mock required")
        instance_dict = dict_descriptor.__get__(calls, backend_type)
        require(type(instance_dict) is dict
                and all(type(key) is str for key in instance_dict),
                "strict exact resource backend storage")
        class_callbacks = {name: class_namespace[name]
                           for name in methods}
        instance_callbacks = {}
        frozen_callbacks = {}
        for name in methods:
            override = instance_dict.get(name)
            require(override is None or type(override) is types.FunctionType,
                    "strict exact resource callback override")
            instance_callbacks[name] = override
            frozen_callbacks[name] = (override if override is not None else
                types.MethodType(class_callbacks[name], calls))
        require(all(type(callback) in (types.MethodType, types.FunctionType)
                    for callback in frozen_callbacks.values()),
                "strict exact resource callbacks required")
        def validate_surface():
            require(type(calls) is backend_type,
                    "exact resource backend type changed")
            current = dict_descriptor.__get__(calls, backend_type)
            require(type(current) is dict
                    and all(type(key) is str for key in current)
                    and all(class_namespace.get(name)
                            is class_callbacks[name] for name in methods)
                    and all(current.get(name) is instance_callbacks[name]
                            for name in methods),
                    "exact resource callback surface changed")
        try:
            self._preflight(authority, authority_digest,
                            "BOUND_UNQUALIFIED")
            require(self.release_attempted is False
                    and self.primary_owned is True
                    and self.anchor_owned is True
                    and self.close_confirmed is False,
                    "fresh exact primary release required")
            self._set("release_attempted", True)
            self._set("_backend", calls)
            self._set("state", "VERIFYING_RESOURCE_BINDING")
            owner = _owner(frozen_callbacks["owner_identity"]())
            validate_surface()
            self._preflight(authority, authority_digest,
                            "VERIFYING_RESOURCE_BINDING")
            require(owner == self.owner,
                    "resource release owner changed")
            single = frozen_callbacks["single_threaded"]()
            validate_surface()
            self._preflight(authority, authority_digest,
                            "VERIFYING_RESOURCE_BINDING")
            require(type(single) is bool and single,
                    "exclusive resource release thread required")
            started = frozen_callbacks["monotonic_ns"]()
            validate_surface()
            self._preflight(authority, authority_digest,
                            "VERIFYING_RESOURCE_BINDING")
            require(type(started) is int and 0 <= started < self.deadline_ns,
                    "resource release deadline reached")
            comparison = frozen_callbacks["same_open_file_description"](
                self.primary_fd, self.anchor_fd)
            validate_surface()
            self._preflight(authority, authority_digest,
                            "VERIFYING_RESOURCE_BINDING")
            require(type(comparison) is dict
                    and all(type(key) is str for key in comparison)
                    and set(comparison) == {"same", "checked_ns"}
                    and type(comparison["same"]) is bool
                    and type(comparison["checked_ns"]) is int
                    and started <= comparison["checked_ns"]
                    < self.deadline_ns,
                    "strict timed open-file-description comparison")
            same = comparison["same"]
            checked_ns = comparison["checked_ns"]
            if not same:
                self._set("state", "QUARANTINED_RESOURCE_IDENTITY")
                receipt = {"schema": 1,
                    "classification": "QUARANTINED_RESOURCE_IDENTITY",
                    "resource_kind": "PIDFD",
                    "primary_close_attempted": False,
                    "primary_close_confirmed": False,
                    "primary_owned": True, "anchor_owned": True,
                    "runtime_authorized": False,
                    "storage_authorized": False, "exec_proven": False}
                self._set("receipt", copy.deepcopy(receipt))
                return receipt
            self._set("state", "CLOSING_PRIMARY")
            primary = self.primary_fd
            self._set("primary_owned", False)
            try:
                result = frozen_callbacks["close"](primary)
                require(result is None, "strict primary close result")
            except BaseException as exc:
                self._set("state", "PRIMARY_CLOSE_UNKNOWN")
                receipt = {"schema": 1,
                    "classification": "PRIMARY_CLOSE_UNKNOWN",
                    "resource_kind": "PIDFD",
                    "primary_close_attempted": True,
                    "primary_close_confirmed": False,
                    "primary_owned": False, "anchor_owned": True,
                    "error": type(exc).__name__,
                    "runtime_authorized": False,
                    "storage_authorized": False, "exec_proven": False}
                self._set("receipt", copy.deepcopy(receipt))
                return receipt
            self._set("close_confirmed", True)
            try:
                validate_surface()
                self._preflight(authority, authority_digest,
                                "CLOSING_PRIMARY")
            except BaseException as exc:
                self._set("state", "PRIMARY_CLOSED_AUTHORITY_UNKNOWN")
                receipt = {"schema": 1,
                    "classification": "PRIMARY_CLOSED_AUTHORITY_UNKNOWN",
                    "resource_kind": "PIDFD",
                    "primary_close_attempted": True,
                    "primary_close_confirmed": True,
                    "primary_owned": False, "anchor_owned": True,
                    "error": type(exc).__name__,
                    "runtime_authorized": False,
                    "storage_authorized": False, "exec_proven": False}
                self._set("receipt", copy.deepcopy(receipt))
                return receipt
            self._set("state", "FINALIZING_PRIMARY_CLOSE")
            finished = frozen_callbacks["monotonic_ns"]()
            try:
                validate_surface()
                require(self.state == "FINALIZING_PRIMARY_CLOSE"
                        and self.reentrant_poisoned is False,
                        "resource release final callback changed state")
                self._preflight(authority, authority_digest,
                                "FINALIZING_PRIMARY_CLOSE")
            except BaseException as exc:
                self._set("state", "PRIMARY_CLOSED_AUTHORITY_UNKNOWN")
                receipt = {"schema": 1,
                    "classification": "PRIMARY_CLOSED_AUTHORITY_UNKNOWN",
                    "resource_kind": "PIDFD",
                    "primary_close_attempted": True,
                    "primary_close_confirmed": True,
                    "primary_owned": False, "anchor_owned": True,
                    "error": type(exc).__name__,
                    "runtime_authorized": False,
                    "storage_authorized": False, "exec_proven": False}
                self._set("receipt", copy.deepcopy(receipt))
                return receipt
            require(type(finished) is int
                    and finished >= checked_ns,
                    "resource release clock regressed")
            timely = finished < self.deadline_ns
            self._set("state", ("PRIMARY_CLOSED_ANCHOR_OWNED"
                      if timely else "PRIMARY_CLOSED_CONFIRMED_LATE"))
            receipt = {"schema": 1,
                "classification": ("PRIMARY_CLOSED_ANCHOR_OWNED"
                    if timely else "PRIMARY_CLOSED_CONFIRMED_LATE"),
                "resource_kind": "PIDFD",
                "primary_close_attempted": True,
                "primary_close_confirmed": True,
                "primary_owned": False, "anchor_owned": True,
                "started_ns": started, "finished_ns": finished,
                "deadline_ns": self.deadline_ns,
                "runtime_authorized": False,
                "storage_authorized": False, "exec_proven": False}
            self._set("receipt", copy.deepcopy(receipt))
            return receipt
        except BaseException:
            if self.close_confirmed:
                self._set("state", "PRIMARY_CLOSED_AUTHORITY_UNKNOWN")
            elif self.state != "QUARANTINED_RESOURCE_IDENTITY":
                self._set("state", "RESOURCE_RELEASE_UNKNOWN")
            raise


def record_exact_pidfd_binding(owner, primary_fd, anchor_fd, authority,
                               authority_digest, deadline_ns):
    """Record only; Linux acquisition/KCMP are not qualified.

    authority_digest is a caller-supplied binding label, not a digest computed
    from or proof of the opaque mutable authority object. A quarantined result
    authorizes closing neither primary nor anchor.
    """
    return ExactFDReleaseBinding(
        _BIND_KEY, owner, "PIDFD", primary_fd, anchor_fd, authority,
        authority_digest, deadline_ns)
