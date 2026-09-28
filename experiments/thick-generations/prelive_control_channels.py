#!/usr/bin/python3
"""Source-only control-pipe ownership, status parser and one-shot grant model."""

import copy
import fcntl
import hashlib
import json
import os
import re
import stat
import threading

import prelive_capture_settlement as CAPTURE
import prelive_owned_child as OWNED
import prelive_pregrant_composition as PREGRANT


MAX_STATUS = 1024
MAX_READS_PER_TURN = 4
_READY_KEY = object()


class Refusal(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise Refusal(message)


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def hex32(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def owner(value):
    require(type(value) is dict and set(value) == {"pid", "starttime", "boot_id"}
            and integer(value["pid"], 1) and integer(value["starttime"], 1)
            and type(value["boot_id"]) is str
            and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                             value["boot_id"]),
            "control owner identity")
    return copy.deepcopy(value)


def fd_identity(value, access, nonblocking):
    require(type(value) is dict and set(value) == {
                "fd", "dev", "inode", "mode", "flags", "fd_flags"}
            and integer(value["fd"], 3) and integer(value["dev"])
            and integer(value["inode"], 1) and integer(value["mode"])
            and integer(value["flags"]) and integer(value["fd_flags"])
            and stat.S_ISFIFO(value["mode"])
            and value["flags"] & os.O_ACCMODE == access
            and bool(value["flags"] & os.O_NONBLOCK) is nonblocking
            and value["fd_flags"] & fcntl.FD_CLOEXEC,
            "control FD identity")
    return copy.deepcopy(value)


class ControlOrigin:
    def __init__(self, pre_fork_ticket, request_id, control_owner, bundle,
                 unarmed_deadline_ns):
        require(type(pre_fork_ticket) is OWNED._PreForkDescendantEnrollment
                and pre_fork_ticket.used is False
                and getattr(pre_fork_ticket, "control_origin", None) is None
                and hex32(request_id) and integer(unarmed_deadline_ns, 1),
                "control pre-fork origin")
        pre_fork_ticket.control_origin = self
        self.pre_fork_ticket = pre_fork_ticket
        self.request_id = request_id
        self.owner = owner(control_owner)
        self.unarmed_deadline_ns = unarmed_deadline_ns
        roles = {
            "grant_parent": (os.O_WRONLY, True),
            "grant_child": (os.O_RDONLY, False),
            "status_parent": (os.O_RDONLY, True),
            "status_child": (os.O_WRONLY, False),
            "stdout_parent": (os.O_RDONLY, True),
            "stdout_child": (os.O_WRONLY, False),
            "stderr_parent": (os.O_RDONLY, True),
            "stderr_child": (os.O_WRONLY, False),
        }
        require(type(bundle) is dict and set(bundle) == set(roles),
                "control bundle schema")
        self.bundle = {name: fd_identity(bundle[name], *spec)
                       for name, spec in roles.items()}
        fds = [value["fd"] for value in self.bundle.values()]
        require(len(set(fds)) == len(fds), "control FD alias")
        pipes = []
        for prefix in ("grant", "status", "stdout", "stderr"):
            left = self.bundle[prefix + "_parent"]
            right = self.bundle[prefix + "_child"]
            require((left["dev"], left["inode"])
                    == (right["dev"], right["inode"]),
                    "control pipe endpoint mismatch")
            pipes.append((left["dev"], left["inode"]))
        require(len(set(pipes)) == 4, "control pipe identity alias")
        self.claimed = False
        self.consumer = None
        self.owner_thread = threading.current_thread()

    def __copy__(self):
        raise Refusal("control origin is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("control origin is noncopyable")


class ReadinessCapability:
    def __init__(self, key, channels, generation, status_identity, child):
        require(key is _READY_KEY, "readiness construction is private")
        object.__setattr__(self, "channels", channels)
        object.__setattr__(self, "generation", generation)
        object.__setattr__(self, "status_identity", copy.deepcopy(status_identity))
        object.__setattr__(self, "child", copy.deepcopy(child))
        object.__setattr__(self, "_frozen", True)

    def __setattr__(self, name, value):
        if getattr(self, "_frozen", False):
            raise Refusal("readiness capability is immutable")
        object.__setattr__(self, name, value)

    def __copy__(self):
        raise Refusal("readiness capability is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("readiness capability is noncopyable")


class ControlChannels:
    def __init__(self, origin, owned_handle, capture_origin, io_model):
        self.origin = origin
        self.owned_handle = owned_handle
        self.capture_origin = capture_origin
        self.io = io_model
        self.frozen_owner = copy.deepcopy(origin.owner)
        self.frozen_bundle = copy.deepcopy(origin.bundle)
        self.frozen_request_id = origin.request_id
        self.frozen_deadline_ns = origin.unarmed_deadline_ns
        self.frozen_child = {
            "lifecycle_token": owned_handle.lifecycle["token"],
            "pid": owned_handle.child["pid"],
            "starttime": owned_handle.child["starttime"],
            "boot_id": owned_handle.child["boot_id"]}
        self.graph_digest = hashlib.sha256(json.dumps({
            "owner": self.frozen_owner, "bundle": self.frozen_bundle,
            "child": self.frozen_child, "request_id": self.frozen_request_id,
            "deadline_ns": self.frozen_deadline_ns}, sort_keys=True,
            separators=(",", ":")).encode("ascii")).hexdigest()
        self.owner_thread = threading.current_thread()
        self.state = "WAITING_READY"
        self.status = bytearray()
        self.generation = 0
        self.readiness = None
        self.readiness_taken = False
        self.grant_attempted = False
        self.grant_fd_owned = True
        self.write_confirmed = False
        self.close_confirmed = False
        self._attempt_latched = False
        self._write_latched = False
        self._close_latched = False
        self.error_seen = False
        self.status_eof = False
        self.last_clock_ns = None
        self._clock_watermark_ns = None
        self.controller = None

    def __copy__(self):
        raise Refusal("control channels are noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("control channels are noncopyable")

    def _poison(self):
        self.state = "UNKNOWN"

    def _clock(self, deadline_ns):
        require(self.state != "UNKNOWN"
                and deadline_ns == self.frozen_deadline_ns
                and self.origin.unarmed_deadline_ns == self.frozen_deadline_ns,
                "control state/deadline changed")
        value = self.io.monotonic_ns_model()
        require(integer(value) and integer(deadline_ns, 1)
                and (self._clock_watermark_ns is None
                     or value >= self._clock_watermark_ns)
                and value < deadline_ns and self.state != "UNKNOWN",
                "control deadline/clock")
        self.last_clock_ns = value
        self._clock_watermark_ns = value

    def _validate_frozen_authority(self):
        current_child = self.owned_handle.child
        require(type(current_child) is dict
                and set(current_child) == {"pid", "starttime", "boot_id"}
                and integer(current_child["pid"], 1)
                and integer(current_child["starttime"], 1)
                and type(current_child["boot_id"]) is str,
                "current owned child schema")
        owner(self.owned_handle.lifecycle["supervisor"])
        require(hex32(self.owned_handle.lifecycle["token"])
                and integer(self.owned_handle.lifecycle["origin"].pid, 1),
                "current owned lifecycle schema")
        require(self.state != "UNKNOWN"
                and threading.current_thread() is self.owner_thread
                and self.origin.owner == self.frozen_owner
                and self.origin.bundle == self.frozen_bundle
                and self.origin.request_id == self.frozen_request_id
                and self.origin.unarmed_deadline_ns == self.frozen_deadline_ns
                and self.origin.pre_fork_ticket.control_origin is self.origin
                and self.origin.claimed is True
                and self.origin.consumer is self
                and self.owned_handle.state == "BOUND"
                and self.owned_handle.lifecycle["state"] == "PIDFD_BOUND"
                and self.owned_handle.lifecycle["origin"].pre_fork_enrollment
                is self.origin.pre_fork_ticket
                and self.origin.pre_fork_ticket.used is True
                and self.owned_handle.lifecycle["supervisor"] == self.frozen_owner
                and self.owned_handle.owner_thread is self.owner_thread
                and self.capture_origin.child == self.frozen_child
                and self.owned_handle.lifecycle["token"]
                == self.frozen_child["lifecycle_token"]
                and self.owned_handle.child == {
                    "pid": self.frozen_child["pid"],
                    "starttime": self.frozen_child["starttime"],
                    "boot_id": self.frozen_child["boot_id"]}
                and self.capture_origin.owner == self.frozen_owner,
                "control authority graph")
        require(self.owned_handle.control_channels is self
                and self.capture_origin.control_channels is self
                and self.last_clock_ns == self._clock_watermark_ns,
                "control consumer/watermark changed")
        CAPTURE._owner(self.capture_origin.owner)
        CAPTURE._child(self.capture_origin.child)
        CAPTURE._identity(self.capture_origin.identities["stdout"])
        CAPTURE._identity(self.capture_origin.identities["stderr"])
        owner(self.origin.owner)
        owner(self.frozen_owner)
        for role, expected in self.frozen_bundle.items():
            access = (os.O_RDONLY if role in (
                "grant_child", "status_parent", "stdout_parent", "stderr_parent")
                else os.O_WRONLY)
            nonblocking = role in ("grant_parent", "status_parent",
                                   "stdout_parent", "stderr_parent")
            fd_identity(expected, access, nonblocking)
            current = fd_identity(self.origin.bundle[role], access, nonblocking)
            require(current == expected, "control origin endpoint changed")
        roles = ["status_parent"]
        if self.grant_fd_owned:
            roles.append("grant_parent")
        require(self.capture_origin.identities["stdout"]
                == {key: self.origin.bundle["stdout_parent"][key]
                    for key in ("fd", "dev", "inode", "mode", "flags")}
                and self.capture_origin.identities["stderr"]
                == {key: self.origin.bundle["stderr_parent"][key]
                    for key in ("fd", "dev", "inode", "mode", "flags")},
                "capture/control origin mismatch")
        if self._attempt_latched:
            require(self.grant_attempted is True, "grant attempt latch changed")
        if self._write_latched:
            require(self.write_confirmed is True, "write evidence changed")
        if self._close_latched:
            require(self.close_confirmed is True, "close evidence changed")

    def _observe_authority(self):
        self._validate_frozen_authority()
        observed_owner = owner(self.io.owner_identity_model())
        self._validate_frozen_authority()
        require(observed_owner == self.frozen_owner, "control owner drift")
        roles = ["status_parent"]
        if self.grant_fd_owned: roles.append("grant_parent")
        for role in roles:
            expected = self.frozen_bundle[role]
            access = os.O_WRONLY if role == "grant_parent" else os.O_RDONLY
            observed = fd_identity(
                self.io.fd_identity_model(expected["fd"]), access, True)
            self._validate_frozen_authority()
            require(observed == expected, "control parent FD drift")

    def _validate_graph_digest(self):
        current = hashlib.sha256(json.dumps({
            "owner": self.frozen_owner, "bundle": self.frozen_bundle,
            "child": self.frozen_child, "request_id": self.frozen_request_id,
            "deadline_ns": self.frozen_deadline_ns}, sort_keys=True,
            separators=(",", ":")).encode("ascii")).hexdigest()
        require(current == self.graph_digest, "frozen control graph changed")

    def _apply_status(self, result):
        require(type(result) is dict and set(result) == {"kind", "data"}
                and result["kind"] in ("DATA", "EAGAIN", "HUP", "EOF")
                and type(result["data"]) is bytes,
                "status observation schema")
        kind = result["kind"]
        if kind == "DATA":
            require(result["data"] != b"", "empty status DATA")
            self.status.extend(result["data"])
            require(len(self.status) <= MAX_STATUS, "status overflow")
            if self.status[:1] == b"E":
                self.error_seen = True
                return "STOP"
            require(self.status == b"R", "ambiguous status protocol")
            return "CONTINUE"
        require(result["data"] == b"", "non-DATA status bytes")
        if kind == "EOF":
            self.status_eof = True
            raise Refusal("status EOF before proven exec")
        if kind == "EAGAIN" and self.status == b"R":
            return "READY"
        return "CONTINUE"

    def _drain(self, deadline_ns):
        ready = False
        for unused in range(MAX_READS_PER_TURN):
            self._clock(deadline_ns); self._observe_authority()
            result = self.io.read_status_model(
                self.origin.bundle["status_parent"]["fd"], MAX_STATUS + 1)
            self._observe_authority(); self._clock(deadline_ns)
            self._validate_frozen_authority()
            outcome = self._apply_status(result)
            self._validate_frozen_authority()
            if outcome == "STOP": break
            if outcome == "READY":
                ready = True
                break
        return ready

    def read_status_turn(self, deadline_ns):
        if (threading.current_thread() is not self.owner_thread
                or self.state not in ("WAITING_READY", "READY_OBSERVED")):
            self._poison(); raise Refusal("status turn unavailable")
        prior = self.state
        self.state = "READING_STATUS"
        try:
            ready = self._drain(deadline_ns)
            require(self.state == "READING_STATUS", "status callback reentrancy")
            self._validate_frozen_authority()
            if self.error_seen:
                self.state = "ERROR_SEEN"
            elif ready:
                if self.readiness is None:
                    self.generation += 1
                    self.readiness = ReadinessCapability(
                        _READY_KEY, self, self.generation,
                        self.origin.bundle["status_parent"],
                        self.owned_handle.child)
                self.state = "READY_OBSERVED"
            else:
                self.state = prior
            return self.snapshot()
        except BaseException:
            self._poison()
            raise

    def take_readiness_once(self):
        if (threading.current_thread() is not self.owner_thread
                or self.state != "READY_OBSERVED" or self.readiness is None
                or self.readiness_taken):
            self._poison(); raise Refusal("readiness unavailable")
        self.readiness_taken = True
        return self.readiness

    def _validate_grant_authority(self, controller, permit, readiness):
        self._validate_graph_digest()
        require(type(controller) is PREGRANT.PreGrantController
                and type(permit) is PREGRANT.OneShotGrantPermit
                and permit.controller is controller and permit is controller.permit
                and permit.used is True and controller.grant_consumed is True
                and controller.state == "GRANT_ATTEMPTED"
                and controller.request["request_id"] == self.frozen_request_id
                and controller.unarmed_deadline_ns
                == self.frozen_deadline_ns
                and controller.control_channels is self
                and hex32(controller.attempt_id)
                and permit.attempt_id == controller.attempt_id
                and controller.binding.pidfd_handle is self.owned_handle
                and controller.journal.state == "SEALED_ACTIVE"
                and readiness is self.readiness
                and readiness.channels is self
                and readiness.generation == self.generation
                and readiness.status_identity
                == self.origin.bundle["status_parent"]
                and readiness.child == self.owned_handle.child,
                "exact pregrant/readiness authority")
        controller._validate_chain()
        controller._validate_permit()
        require(self.status == b"R" and not self.error_seen
                and not self.status_eof, "status no longer grants readiness")

    def attach_pregrant_controller(self, controller):
        require(self.controller is None
                and type(controller) is PREGRANT.PreGrantController
                and controller.state == "CHILD_BOUND_PERSISTED"
                and controller.binding.pidfd_handle is self.owned_handle
                and controller.request["request_id"] == self.frozen_request_id
                and controller.unarmed_deadline_ns
                == self.frozen_deadline_ns
                and getattr(controller, "control_channels", None) is None,
                "control/pregrant attachment")
        controller.control_channels = self
        self.controller = controller

    def issue_grant_once(self, controller, permit, readiness, deadline_ns):
        if (threading.current_thread() is not self.owner_thread
                or self.state != "READY_OBSERVED" or not self.readiness_taken
                or self.grant_attempted):
            self._poison(); raise Refusal("grant unavailable")
        self.state = "VALIDATING_GRANT"
        try:
            self._validate_grant_authority(controller, permit, readiness)
            require(controller is self.controller, "foreign control controller")
            self._observe_authority(); self._clock(deadline_ns)
            require(self._drain(deadline_ns) is True and not self.error_seen,
                    "final status drain")
            self._validate_grant_authority(controller, permit, readiness)
            self._observe_authority(); self._clock(deadline_ns)
            self._validate_grant_authority(controller, permit, readiness)
            self._validate_frozen_authority()
            self.grant_attempted = True
            self._attempt_latched = True
            self.state = "GRANT_ATTEMPTED"
            written = self.io.write_grant_model(
                self.frozen_bundle["grant_parent"]["fd"], b"G")
            require(type(written) is int and written == 1,
                    "one-byte grant write")
            self.write_confirmed = True
            self._write_latched = True
            self._validate_grant_authority(controller, permit, readiness)
            self._observe_authority()
            self._validate_grant_authority(controller, permit, readiness)
            self._validate_frozen_authority()
            self.grant_fd_owned = False
            self._validate_frozen_authority()
            result = self.io.close_grant_model(
                self.frozen_bundle["grant_parent"]["fd"])
            require(type(result) is dict and set(result) == {"closed"}
                    and result["closed"] is True,
                    "grant writer close")
            require(self.state == "GRANT_ATTEMPTED",
                    "grant close callback reentrancy")
            self._validate_grant_authority(controller, permit, readiness)
            self._observe_authority()
            self._validate_grant_authority(controller, permit, readiness)
            self.close_confirmed = True
            self._close_latched = True
            self._validate_frozen_authority()
            self.state = "WRITER_CLOSE_CONFIRMED"
            return self.snapshot()
        except BaseException:
            self._poison()
            raise

    def snapshot(self):
        return {"schema": 1, "classification": {
                    "WAITING_READY": "WAITING_READY",
                    "READY_OBSERVED": "MODEL_READY_OBSERVED",
                    "ERROR_SEEN": "MODEL_LAUNCHER_ERROR",
                    "WRITER_CLOSE_CONFIRMED":
                        "G_WRITE_AND_WRITER_CLOSE_CONFIRMED",
                    "UNKNOWN": "UNKNOWN"}.get(self.state, "UNKNOWN_BUSY"),
                "status_bytes": len(self.status),
                "readiness_generation": self.generation,
                "readiness_taken": self.readiness_taken,
                "error_seen": self.error_seen, "status_eof": self.status_eof,
                "grant_attempted": self._attempt_latched,
                "write_confirmed": self._write_latched,
                "writer_close_confirmed": self._close_latched,
                "grant_fd_owned": self.grant_fd_owned,
                "child_may_exist": True, "settlement_required": True,
                "child_observed_grant_eof": False, "exec_proven": False,
                "runtime_authorized": False, "storage_authorized": False,
                "postcondition_verified": False}


def record_control_origin(pre_fork_ticket, request_id, control_owner, bundle,
                          unarmed_deadline_ns):
    return ControlOrigin(pre_fork_ticket, request_id, control_owner, bundle,
                         unarmed_deadline_ns)


def bind_parent_control(origin, owned_handle, capture_origin, io_model):
    require(type(origin) is ControlOrigin and origin.claimed is False
            and origin.consumer is None
            and origin.owner_thread is threading.current_thread()
            and type(owned_handle) is OWNED._OwnedPidfdHandle
            and type(capture_origin) is CAPTURE._PipeOrigin
            and getattr(owned_handle, "control_channels", None) is None
            and getattr(capture_origin, "control_channels", None) is None
            and getattr(io_model, "model_only", None) is True,
            "control binding inputs")
    origin.claimed = True
    channels = ControlChannels(origin, owned_handle, capture_origin, io_model)
    origin.consumer = channels
    owned_handle.control_channels = channels
    capture_origin.control_channels = channels
    try:
        channels._observe_authority()
    except BaseException:
        channels._poison()
        raise
    return channels
