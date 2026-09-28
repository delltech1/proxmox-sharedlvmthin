import copy
import importlib
import os
from pathlib import Path
import stat
import sys
import threading
from types import SimpleNamespace
import unittest

EXP = Path(__file__).resolve().parents[2] / "experiments/thick-generations"
sys.path.insert(0, str(EXP))
OWNED = importlib.import_module("prelive_owned_child")
CAP = importlib.import_module("prelive_capture_settlement")
LAB = importlib.import_module("prelive_control_channels")

BOOT = "12345678-1234-1234-1234-123456789abc"
OWNER = {"pid": 100, "starttime": 200, "boot_id": BOOT}


def ident(fd, inode, access, nonblock):
    flags = access | (os.O_NONBLOCK if nonblock else 0)
    return {"fd": fd, "dev": 1, "inode": inode,
            "mode": stat.S_IFIFO | 0o600, "flags": flags,
            "fd_flags": 1}


def bundle():
    return {"grant_parent": ident(10, 100, os.O_WRONLY, True),
            "grant_child": ident(11, 100, os.O_RDONLY, False),
            "status_parent": ident(12, 101, os.O_RDONLY, True),
            "status_child": ident(13, 101, os.O_WRONLY, False),
            "stdout_parent": ident(14, 102, os.O_RDONLY, True),
            "stdout_child": ident(15, 102, os.O_WRONLY, False),
            "stderr_parent": ident(16, 103, os.O_RDONLY, True),
            "stderr_child": ident(17, 103, os.O_WRONLY, False)}


class OwnedCalls:
    def __init__(self): self.starts = [400, 400]
    def current_identity(self): return copy.deepcopy(OWNER)
    def starttime(self, _pid): return self.starts.pop(0)
    def launcher(self, _pid):
        return {"kind": "UNARMED_LAUNCHER_NOT_PAYLOAD", "exe_dev": 1,
                "exe_inode": 2, "cmdline_sha256": "a" * 64}
    def pidfd_open(self, _pid): return 50
    def probe_waitable(self, _pidfd): return None
    def waitid(self, _pidfd, _options): return None
    def close(self, _fd): pass


class IO:
    model_only = True
    def __init__(self, values):
        self.values = list(values); self.times = iter(range(100, 100000, 10))
        self.identities = bundle(); self.writes = 0; self.closes = 0
        self.write_result = 1; self.close_result = {"closed": True}
        self.read_hook = None; self.write_hook = None; self.close_hook = None
        self.clock_hook = None
        self.fd_hook = None
    def owner_identity_model(self): return copy.deepcopy(OWNER)
    def fd_identity_model(self, fd):
        if self.fd_hook: self.fd_hook(fd)
        for value in self.identities.values():
            if value["fd"] == fd: return copy.deepcopy(value)
        raise RuntimeError("fd")
    def monotonic_ns_model(self):
        if self.clock_hook: self.clock_hook()
        return next(self.times)
    def read_status_model(self, _fd, _maximum):
        if self.read_hook: self.read_hook()
        return copy.deepcopy(self.values.pop(0))
    def write_grant_model(self, _fd, data):
        self.writes += 1
        if self.write_hook: self.write_hook()
        self.last_write = data; return self.write_result
    def close_grant_model(self, _fd):
        self.closes += 1
        if self.close_hook: self.close_hook()
        return copy.deepcopy(self.close_result)


def graph(values):
    binding = object()
    ticket = OWNED.prepare_descendant_fork_enrollment(
        OWNED._PREFORK_KEY, binding, OWNER)
    origin = LAB.record_control_origin(
        ticket, "c" * 32, OWNER, bundle(), 1000000)
    lifecycle = OWNED._record_owned_fork(
        OWNED._FORK_KEY, 300, OWNER, "d" * 32, ticket)
    calls = OwnedCalls()
    expected = {"kind": "UNARMED_LAUNCHER_NOT_PAYLOAD", "exe_dev": 1,
                "exe_inode": 2, "cmdline_sha256": "a" * 64}
    handle = OWNED.bind_owned_pidfd(lifecycle, expected, calls)
    child = {"lifecycle_token": lifecycle["token"], "pid": 300,
             "starttime": 400, "boot_id": BOOT}
    capture = CAP._record_capture_pipes(
        CAP._PIPE_KEY, OWNER, child,
        {key: bundle()["stdout_parent"][key]
         for key in ("fd", "dev", "inode", "mode", "flags")},
        {key: bundle()["stderr_parent"][key]
         for key in ("fd", "dev", "inode", "mode", "flags")})
    io = IO(values)
    return LAB.bind_parent_control(origin, handle, capture, io), io


class JournalBackend:
    model_only = True
    def persist_model(self, raw, _event):
        return {"bytes_written": len(raw), "file_synced": True,
                "dir_synced": True}


class IntegratedLauncher(IO):
    def __init__(self, values):
        super().__init__(values)
        self.gate = None; self.channels = None; self.readiness = None
        self.capture = None; self.control_origin = None
        self.foreign_permit = False
        self.override_deadline = None
    def prepare_bind_unarmed_model(self, req, owner_value, ticket, lifecycle,
                                   channel):
        self.control_origin = LAB.record_control_origin(
            ticket, req["request_id"], owner_value, bundle(),
            self.gate.unarmed_deadline_ns)
        recorded = OWNED._record_owned_fork(
            OWNED._FORK_KEY, 300, owner_value, "d" * 32, ticket)
        lifecycle.update(recorded)
        calls = OwnedCalls()
        expected = {"kind": "UNARMED_LAUNCHER_NOT_PAYLOAD", "exe_dev": 1,
                    "exe_inode": 2, "cmdline_sha256": "a" * 64}
        handle = OWNED.bind_owned_pidfd(lifecycle, expected, calls)
        child_identity = {"lifecycle_token": lifecycle["token"], "pid": 300,
                          "starttime": 400, "boot_id": BOOT}
        self.capture = CAP._record_capture_pipes(
            CAP._PIPE_KEY, owner_value, child_identity,
            {key: bundle()["stdout_parent"][key]
             for key in ("fd", "dev", "inode", "mode", "flags")},
            {key: bundle()["stderr_parent"][key]
             for key in ("fd", "dev", "inode", "mode", "flags")})
        return {"child": {"request_id": req["request_id"], "boot_id": BOOT,
                           "pid": 300, "starttime": 400,
                           "owner_pid": owner_value["pid"],
                           "owner_starttime": owner_value["starttime"],
                           "launcher_sha256": "a" * 64, "armed": False},
                "launcher": copy.deepcopy(req["launcher"]),
                "owned_handle": handle,
                "capture_adapter": OWNED.bind_capture_child_adapter(handle, calls),
                "fork_origin": lifecycle["origin"],
                "grant_channel": channel, "readiness": "R"}
    def recheck_unarmed_model(self, binding, channel):
        return {"launcher_alive": binding.pidfd_handle.state == "BOUND",
                "pidfd_bound": binding.pidfd_handle.pidfd == 50,
                "channel_exact": channel is binding.grant_channel,
                "readiness": "R"}
    def grant_once_model(self, binding, permit, channel):
        selected = permit
        if self.foreign_permit:
            selected = LAB.PREGRANT.OneShotGrantPermit(
                LAB.PREGRANT._PERMIT_KEY, self.gate, permit.intent,
                permit.child_bound, permit.issued, permit.binding, "e" * 32)
            selected.used = True
        self.channels.issue_grant_once(
            self.gate, selected, self.readiness,
            self.override_deadline if self.override_deadline is not None
            else self.gate.unarmed_deadline_ns)
        return {"bytes_written": 1, "writer_eof": True, "channel": channel}


def pregrant_request():
    executable = {"path": "/usr/bin/true", "sha256": "a" * 64,
                  "dev": 1, "inode": 2}
    return {"schema": 1, "request_id": "c" * 32,
            "purpose": "DISPOSABLE_KERNEL_LAB", "boot_id": BOOT,
            "argv": ["/usr/bin/true"],
            "environment": dict(LAB.PREGRANT.SUP.ENVIRONMENT),
            "executable": copy.deepcopy(executable),
            "launcher": copy.deepcopy(executable), "timeout_ms": 5000,
            "cleanup_ms": 1000, "capture_limit": 4096,
            "signal_policy": "NONE", "storage_authorized": False,
            "postcondition_verified": False}


def ready_graph(final_values=None):
    values = [{"kind": "DATA", "data": b"R"},
              {"kind": "EAGAIN", "data": b""}]
    values.extend(final_values if final_values is not None else
                  [{"kind": "EAGAIN", "data": b""}])
    launcher = IntegratedLauncher(values)
    gate = LAB.PREGRANT.prepare_pregrant_model(
        pregrant_request(), OWNER, JournalBackend(), launcher)
    launcher.gate = gate
    gate.persist_intent(); gate.prepare_and_bind_unarmed()
    channels = LAB.bind_parent_control(
        launcher.control_origin, gate.binding.pidfd_handle,
        launcher.capture, launcher)
    channels.attach_pregrant_controller(gate)
    channels.read_status_turn(gate.unarmed_deadline_ns)
    launcher.readiness = channels.take_readiness_once()
    launcher.channels = channels
    return channels, launcher, gate, launcher.readiness


class ControlChannelTests(unittest.TestCase):
    def test_split_ready_requires_eagain_then_capability(self):
        channels, io = graph([{"kind": "DATA", "data": b"R"},
                              {"kind": "EAGAIN", "data": b""}])
        result = channels.read_status_turn(1000000)
        self.assertEqual(result["classification"], "MODEL_READY_OBSERVED")
        ready = channels.take_readiness_once()
        self.assertIs(ready.channels, channels)
        with self.assertRaises(LAB.Refusal): channels.take_readiness_once()

    def test_error_split_and_error_prefix_refuse_readiness(self):
        for values in ([{"kind": "DATA", "data": b"E"}],
                       [{"kind": "DATA", "data": b"E:"},
                        {"kind": "DATA", "data": b"bad"}]):
            channels, io = graph(values)
            result = channels.read_status_turn(1000000)
            self.assertEqual(result["classification"], "MODEL_LAUNCHER_ERROR")
            self.assertTrue(result["error_seen"])

    def test_re_error_duplicate_unknown_and_eof_are_unknown(self):
        for data in (b"RE:error", b"RR", b"X"):
            channels, io = graph([{"kind": "DATA", "data": data}])
            with self.assertRaises(LAB.Refusal): channels.read_status_turn(1000000)
            self.assertEqual(channels.state, "UNKNOWN")
        for prefix in ([], [{"kind": "DATA", "data": b"R"}]):
            channels, io = graph(prefix + [{"kind": "EOF", "data": b""}])
            with self.assertRaises(LAB.Refusal): channels.read_status_turn(1000000)
            self.assertTrue(channels.status_eof)

    def test_hup_is_not_eof_or_readiness(self):
        channels, io = graph([{"kind": "HUP", "data": b""}] * 4)
        result = channels.read_status_turn(1000000)
        self.assertEqual(result["classification"], "WAITING_READY")
        self.assertFalse(result["status_eof"])

    def test_fd_alias_mode_and_cloexec_refuse_origin(self):
        for attack in ("alias", "mode", "cloexec", "blocking"):
            value = bundle()
            if attack == "alias": value["status_parent"]["fd"] = 10
            elif attack == "mode": value["grant_parent"]["flags"] = os.O_RDONLY | os.O_NONBLOCK
            elif attack == "cloexec": value["grant_parent"]["fd_flags"] = 0
            else: value["grant_parent"]["flags"] = os.O_WRONLY
            ticket = OWNED.prepare_descendant_fork_enrollment(
                OWNED._PREFORK_KEY, object(), OWNER)
            with self.assertRaises(LAB.Refusal):
                LAB.record_control_origin(
                    ticket, "c" * 32, OWNER, value, 1000000)

    def test_deadline_and_fd_drift_poison(self):
        channels, io = graph([{"kind": "EAGAIN", "data": b""}])
        io.times = iter([1000000])
        with self.assertRaises(LAB.Refusal): channels.read_status_turn(1000000)
        channels, io = graph([{"kind": "EAGAIN", "data": b""}])
        io.identities["status_parent"]["inode"] += 1
        with self.assertRaises(LAB.Refusal): channels.read_status_turn(1000000)

    def test_copy_and_foreign_origin_reuse_refuse(self):
        channels, io = graph([{"kind": "HUP", "data": b""}] * 4)
        with self.assertRaises(LAB.Refusal): copy.copy(channels)
        with self.assertRaises(LAB.Refusal): copy.deepcopy(channels.origin)
        with self.assertRaises(LAB.Refusal):
            LAB.bind_parent_control(channels.origin, channels.owned_handle,
                                    channels.capture_origin, io)

    def test_ticket_and_owned_handle_allow_only_one_control_consumer(self):
        ticket = OWNED.prepare_descendant_fork_enrollment(
            OWNED._PREFORK_KEY, object(), OWNER)
        LAB.record_control_origin(ticket, "c" * 32, OWNER, bundle(), 1000000)
        with self.assertRaises(LAB.Refusal):
            LAB.record_control_origin(ticket, "c" * 32, OWNER, bundle(), 1000000)
        channels, io = graph([{"kind": "HUP", "data": b""}] * 4)
        second_ticket = OWNED.prepare_descendant_fork_enrollment(
            OWNED._PREFORK_KEY, object(), OWNER)
        second_origin = LAB.record_control_origin(
            second_ticket, "c" * 32, OWNER, bundle(), 1000000)
        second_ticket.used = True
        second_origin.pre_fork_ticket = channels.origin.pre_fork_ticket
        with self.assertRaises(LAB.Refusal):
            LAB.bind_parent_control(second_origin, channels.owned_handle,
                                    channels.capture_origin, io)

    def test_one_byte_grant_and_writer_close_ceiling(self):
        channels, io, controller, readiness = ready_graph()
        gate_result = controller.issue_grant_once("f" * 32)
        result = channels.snapshot()
        self.assertEqual(gate_result["classification"],
                         "MODEL_GRANT_PROTOCOL_COMPLETED_EXEC_UNPROVEN")
        self.assertEqual(result["classification"],
                         "G_WRITE_AND_WRITER_CLOSE_CONFIRMED")
        self.assertEqual(io.last_write, b"G")
        self.assertEqual((io.writes, io.closes), (1, 1))
        self.assertFalse(result["child_observed_grant_eof"])
        self.assertFalse(result["exec_proven"])

    def test_error_during_final_drain_means_zero_writes(self):
        channels, io, controller, readiness = ready_graph(
            [{"kind": "DATA", "data": b"E:error"}])
        self.assertEqual(controller.issue_grant_once("f" * 32)["classification"],
                         "UNKNOWN")
        self.assertEqual(io.writes, 0)
        self.assertEqual(channels.state, "UNKNOWN")

    def test_deadline_before_write_means_zero_writes(self):
        channels, io, controller, readiness = ready_graph()
        io.times = iter([controller.unarmed_deadline_ns])
        self.assertEqual(controller.issue_grant_once("f" * 32)["classification"],
                         "UNKNOWN")
        self.assertEqual(io.writes, 0)

    def test_extended_deadline_is_not_accepted(self):
        channels, io, controller, readiness = ready_graph()
        io.override_deadline = controller.unarmed_deadline_ns + 1
        self.assertEqual(controller.issue_grant_once("f" * 32)["classification"],
                         "UNKNOWN")
        self.assertEqual(io.writes, 0)

    def test_short_bool_or_ambiguous_write_is_one_attempt(self):
        for result in (0, True):
            channels, io, controller, readiness = ready_graph()
            io.write_result = result
            self.assertEqual(controller.issue_grant_once("f" * 32)["classification"],
                             "UNKNOWN")
            self.assertEqual(io.writes, 1)
            self.assertTrue(channels.grant_attempted)
            self.assertEqual(io.closes, 0)
        channels, io, controller, readiness = ready_graph()
        io.write_hook = lambda: (_ for _ in ()).throw(OSError("after write"))
        self.assertEqual(controller.issue_grant_once("f" * 32)["classification"],
                         "UNKNOWN")
        self.assertEqual(io.writes, 1)

    def test_close_ambiguity_is_one_attempt_without_fd_ownership(self):
        channels, io, controller, readiness = ready_graph()
        io.close_result = {"closed": False}
        self.assertEqual(controller.issue_grant_once("f" * 32)["classification"],
                         "UNKNOWN")
        self.assertEqual((io.writes, io.closes), (1, 1))
        self.assertFalse(channels.grant_fd_owned)
        self.assertTrue(channels.write_confirmed)
        self.assertFalse(channels.close_confirmed)

    def test_close_ack_bool_aliases_refuse(self):
        for value in (1, 1.0):
            channels, io, controller, readiness = ready_graph()
            io.close_result = {"closed": value}
            self.assertEqual(controller.issue_grant_once("f" * 32)["classification"],
                             "UNKNOWN")
            self.assertEqual(io.closes, 1)

    def test_authority_poison_during_write_or_close_is_unknown(self):
        for phase in ("write", "close"):
            channels, io, controller, readiness = ready_graph()
            def poison():
                channels.owned_handle.state = "UNKNOWN_OBSERVE_OUTCOME"
            if phase == "write": io.write_hook = poison
            else: io.close_hook = poison
            self.assertEqual(controller.issue_grant_once("f" * 32)["classification"],
                             "UNKNOWN")
            self.assertEqual(channels.state, "UNKNOWN")
            self.assertEqual(io.writes, 1)
            self.assertEqual(io.closes, 0 if phase == "write" else 1)

    def test_replayed_readiness_and_foreign_permit_refuse(self):
        channels, io, controller, readiness = ready_graph()
        io.foreign_permit = True
        self.assertEqual(controller.issue_grant_once("f" * 32)["classification"],
                         "UNKNOWN")
        self.assertEqual(io.writes, 0)

    def test_permit_mutation_during_write_or_close_never_completes(self):
        for phase in ("write", "close"):
            channels, io, controller, readiness = ready_graph()
            def mutate(): controller.permit.attempt_id = "0" * 32
            if phase == "write": io.write_hook = mutate
            else: io.close_hook = mutate
            self.assertEqual(controller.issue_grant_once("f" * 32)["classification"],
                             "UNKNOWN")
            self.assertEqual(io.writes, 1)
            self.assertEqual(io.closes, 0 if phase == "write" else 1)

    def test_reentry_or_status_mutation_during_write_stops_before_close(self):
        for attack in ("reentry", "status", "attempt_latch"):
            channels, io, controller, readiness = ready_graph()
            def mutate(selected=attack):
                if selected == "reentry":
                    try: channels.read_status_turn(channels.origin.unarmed_deadline_ns)
                    except LAB.Refusal: pass
                elif selected == "status": channels.error_seen = True
                else: channels.grant_attempted = False
            io.write_hook = mutate
            self.assertEqual(controller.issue_grant_once("f" * 32)["classification"],
                             "UNKNOWN")
            self.assertEqual(io.closes, 0)
            self.assertEqual(channels.state, "UNKNOWN")

    def test_last_control_clock_mutation_stops_before_write(self):
        channels, io, controller, readiness = ready_graph()
        calls = []
        def clock():
            if channels.state == "VALIDATING_GRANT":
                calls.append(True)
                if len(calls) == 4:
                    controller.permit.attempt_id = "0" * 32
        io.clock_hook = clock
        self.assertEqual(controller.issue_grant_once("f" * 32)["classification"],
                         "UNKNOWN")
        self.assertEqual(io.writes, 0)

    def test_last_fd_callback_poison_cannot_false_complete(self):
        channels, io, controller, readiness = ready_graph()
        def fd_hook(_fd):
            if (channels.state == "GRANT_ATTEMPTED"
                    and channels.grant_fd_owned is False):
                channels.owned_handle.state = "UNKNOWN_OBSERVE_OUTCOME"
        io.fd_hook = fd_hook
        self.assertEqual(controller.issue_grant_once("f" * 32)["classification"],
                         "UNKNOWN")
        self.assertEqual((io.writes, io.closes), (1, 1))
        self.assertFalse(channels.close_confirmed)

    def test_last_fd_callback_parser_or_permit_mutation_cannot_complete(self):
        for attack in ("parser", "permit"):
            channels, io, controller, readiness = ready_graph()
            def fd_hook(_fd, selected=attack):
                if io.closes == 1:
                    if selected == "parser": channels.error_seen = True
                    else: controller.permit.attempt_id = "0" * 32
            io.fd_hook = fd_hook
            self.assertEqual(controller.issue_grant_once("f" * 32)["classification"],
                             "UNKNOWN")
            self.assertEqual((io.writes, io.closes), (1, 1))
            self.assertNotEqual(channels.state, "WRITER_CLOSE_CONFIRMED")

    def test_last_status_clock_owned_poison_never_publishes_ready(self):
        channels, io = graph([{"kind": "DATA", "data": b"R"},
                              {"kind": "EAGAIN", "data": b""}])
        calls = []
        def clock():
            calls.append(True)
            if len(calls) == 4:
                channels.owned_handle.state = "UNKNOWN_OBSERVE_OUTCOME"
        io.clock_hook = clock
        with self.assertRaises(LAB.Refusal): channels.read_status_turn(1000000)
        self.assertIsNone(channels.readiness)
        self.assertEqual(channels.state, "UNKNOWN")

    def test_current_origin_endpoint_type_aliases_never_grant(self):
        for field, replacement in (("fd", 10.0), ("fd_flags", True)):
            channels, io, controller, readiness = ready_graph()
            channels.origin.bundle["grant_parent"][field] = replacement
            result = controller.issue_grant_once("f" * 32)
            self.assertEqual(result["classification"], "UNKNOWN")
            self.assertEqual((io.writes, io.closes), (0, 0))

    def test_last_clock_origin_endpoint_type_alias_never_writes(self):
        for field, replacement in (("fd", 10.0), ("fd_flags", True)):
            channels, io, controller, readiness = ready_graph()
            calls = []
            def clock(selected=field, value=replacement):
                if channels.state != "VALIDATING_GRANT": return
                calls.append(True)
                if len(calls) == 4:
                    channels.origin.bundle["grant_parent"][selected] = value
            io.clock_hook = clock
            result = controller.issue_grant_once("f" * 32)
            self.assertEqual(result["classification"], "UNKNOWN")
            self.assertEqual((io.writes, io.closes), (0, 0))

    def test_origin_mutation_during_status_callback_is_unknown(self):
        channels, io = graph([{"kind": "DATA", "data": b"R"}])
        io.read_hook = lambda: channels.origin.bundle["status_parent"].__setitem__(
            "inode", 999)
        with self.assertRaises(LAB.Refusal): channels.read_status_turn(1000000)
        self.assertEqual(channels.state, "UNKNOWN")

    def test_float_owner_fd_or_child_identity_refuses(self):
        channels, io = graph([{"kind": "EAGAIN", "data": b""}])
        io.owner_identity_model = lambda: {
            "pid": 100.0, "starttime": 200, "boot_id": BOOT}
        with self.assertRaises(LAB.Refusal): channels.read_status_turn(1000000)
        channels, io = graph([{"kind": "EAGAIN", "data": b""}])
        original = io.fd_identity_model
        def float_fd(fd):
            value = original(fd); value["fd"] = float(value["fd"]); return value
        io.fd_identity_model = float_fd
        with self.assertRaises(LAB.Refusal): channels.read_status_turn(1000000)
        channels, io = graph([{"kind": "EAGAIN", "data": b""}])
        channels.owned_handle.child["pid"] = 300.0
        with self.assertRaises(LAB.Refusal): channels.read_status_turn(1000000)

    def test_foreign_thread_status_turn_monotonically_poisons(self):
        channels, io = graph([{"kind": "EAGAIN", "data": b""}])
        errors = []
        def foreign():
            try: channels.read_status_turn(1000000)
            except BaseException as exc: errors.append(exc)
        worker = threading.Thread(target=foreign); worker.start(); worker.join()
        self.assertEqual(len(errors), 1)
        self.assertEqual(channels.state, "UNKNOWN")
        self.assertEqual(len(io.values), 1)

    def test_status_overflow_is_unknown(self):
        channels, io = graph([{"kind": "DATA", "data": b"E" + b"x" * 1024}])
        with self.assertRaises(LAB.Refusal): channels.read_status_turn(1000000)
        self.assertEqual(channels.state, "UNKNOWN")

    def test_completed_grant_cannot_be_replayed(self):
        channels, io, controller, readiness = ready_graph()
        controller.issue_grant_once("f" * 32)
        controller.issue_grant_once("f" * 32)
        self.assertEqual((io.writes, io.closes), (1, 1))


if __name__ == "__main__": unittest.main()
