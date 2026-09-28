import copy
import errno
import fcntl
import hashlib
import importlib
import os
from pathlib import Path
import select
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
EXP = ROOT / "experiments/thick-generations"
sys.path.insert(0, str(EXP))

LAB = importlib.import_module("prelive_abort_fork_split")
GRAPH = importlib.import_module("prelive_allocated_graph_enrollment")
FDS = importlib.import_module("prelive_real_fd_adapter")
CONSUMER = importlib.import_module("prelive_launcher_identity_consumer")
BRIDGE = importlib.import_module("prelive_v2_intent_file_bridge")
OWNED = importlib.import_module("prelive_owned_child")
LINUX = importlib.import_module("prelive_abort_linux_child")
CAPTURE = importlib.import_module("prelive_capture_settlement")
DESC = importlib.import_module("prelive_descendant_accounting")
DBRIDGE = importlib.import_module("prelive_descendant_bridge")
APROTO = importlib.import_module("prelive_allocated_abort_protocol")
ARELEASE = importlib.import_module("prelive_allocated_release_inputs")
ADEADLINE = importlib.import_module("prelive_allocated_release_deadline")
RPLAN = importlib.import_module("prelive_allocated_resource_plan")
from tests.unit import test_prelive_launcher_identity_consumer as FIXTURE


class ChildExit(BaseException):
    def __init__(self, code):
        self.code = code


class ChildCalls:
    abort_child_split_mock = True

    def __init__(self, kernel):
        self.kernel = kernel

    def monotonic_ns(self):
        return self.kernel.monotonic_ns()

    def current_child_identity(self):
        return {"pid": self.kernel.pid, "ppid": self.kernel.owner["pid"],
                "starttime": 900, "boot_id": self.kernel.owner["boot_id"]}

    def validate_child_graph(self, descriptor):
        self.kernel.validate_descriptor(descriptor)
        return True

    def close_role_once(self, role, expected_identity):
        if self.kernel.mode == "hold_grant_writer" and role == "grant_parent":
            return True
        if role in self.kernel.child_closed:
            raise OSError("child close replay")
        fd = self.kernel.child_fds[role]
        self.kernel.child_closed.add(role)
        del self.kernel.child_fds[role]
        try:
            self.kernel.validate_child_fd(role, fd, expected_identity)
        except BaseException:
            self.kernel.quarantined_fds.append(fd)
            raise
        os.close(fd)
        return True

    def wait_writable(self, role, deadline_ns):
        if self.monotonic_ns() >= deadline_ns:
            raise TimeoutError("write deadline")
        return True

    def write_role(self, role, data):
        if role not in ("status_child", "stdout_child", "stderr_child"):
            raise AssertionError("unauthorized child write role")
        count = min(len(data), 3 if self.kernel.mode == "short_writes" else len(data))
        return os.write(self.kernel.child_fds[role], data[:count])

    def read_grant(self, maximum):
        fd = self.kernel.child_fds["grant_child"]
        readable, _, _ = select.select([fd], [], [], 0)
        if not readable:
            raise BlockingIOError(errno.EAGAIN, "grant pending")
        return os.read(fd, maximum)

    def exit_child(self, code):
        for role, fd in list(self.kernel.child_fds.items()):
            if role not in self.kernel.child_closed:
                self.kernel.child_closed.add(role)
                del self.kernel.child_fds[role]
                try: os.close(fd)
                except OSError: pass
        self.kernel.exit_code = code
        raise ChildExit(code)


class SourceEpollReleaseBackend:
    source_epoll_release_mock = True

    def __init__(self, observed_ns, finished_ns=None, observe_hook=None,
                 close_hook=None, result=None, observation=None):
        self.observed_ns = observed_ns
        self.finished_ns = (observed_ns + 1 if finished_ns is None
                            else finished_ns)
        self.observe_hook = observe_hook
        self.close_hook = close_hook
        self.result = result
        self.observation = observation
        self.last_observation = None
        self.observations = 0
        self.closes = 0

    def observe_epoll(self, poller, fd):
        self.observations += 1
        if self.observe_hook is not None:
            self.observe_hook()
        result = (self.observation if self.observation is not None else
                  {"same_object": True, "closed": False, "fd": fd,
                   "observed_ns": self.observed_ns})
        self.last_observation = result
        return result

    def close_epoll(self, poller, fd):
        self.closes += 1
        if self.close_hook is not None:
            self.close_hook()
        if self.result is not None:
            return self.result
        return {"closed": True, "finished_ns": self.finished_ns}


class SwappedSourceEpollReleaseBackend(SourceEpollReleaseBackend):
    pass


class SourceStatusReleaseBackend:
    source_status_release_mock = True

    def __init__(self, observed_ns, finished_ns=None, observe_hook=None,
                 close_hook=None, result=None, observation=None):
        self.observed_ns = observed_ns
        self.finished_ns = (observed_ns + 1 if finished_ns is None
                            else finished_ns)
        self.observe_hook = observe_hook
        self.close_hook = close_hook
        self.result = result
        self.observation = observation
        self.observations = 0
        self.closes = 0

    def observe_status(self, fd, identity):
        self.observations += 1
        if self.observe_hook is not None:
            self.observe_hook()
        return (self.observation if self.observation is not None else
                {"role": "status_parent", "fd": fd,
                 "identity": copy.deepcopy(identity),
                 "identity_matches": True, "closed": False,
                 "observed_ns": self.observed_ns})

    def close_status(self, fd, identity):
        self.closes += 1
        if self.close_hook is not None:
            self.close_hook()
        if self.result is not None:
            return self.result
        return {"closed": True, "finished_ns": self.finished_ns}


class SwappedSourceStatusReleaseBackend(SourceStatusReleaseBackend):
    pass


class SourceStdoutReleaseBackend:
    source_stdout_release_mock = True

    def __init__(self, observed_ns, finished_ns=None, observe_hook=None,
                 close_hook=None, result=None, observation=None):
        self.observed_ns = observed_ns
        self.finished_ns = (observed_ns + 1 if finished_ns is None
                            else finished_ns)
        self.observe_hook = observe_hook
        self.close_hook = close_hook
        self.result = result
        self.observation = observation
        self.observations = 0
        self.closes = 0

    def observe_stdout(self, fd, identity):
        self.observations += 1
        if self.observe_hook is not None:
            self.observe_hook()
        return (self.observation if self.observation is not None else
                {"role": "stdout_parent", "fd": fd,
                 "identity": copy.deepcopy(identity),
                 "identity_matches": True, "closed": False,
                 "observed_ns": self.observed_ns})

    def close_stdout(self, fd, identity):
        self.closes += 1
        if self.close_hook is not None:
            self.close_hook()
        if self.result is not None:
            return self.result
        return {"closed": True, "finished_ns": self.finished_ns}


class SwappedSourceStdoutReleaseBackend(SourceStdoutReleaseBackend):
    pass


class SourceStderrReleaseBackend:
    source_stderr_release_mock = True

    def __init__(self, observed_ns, finished_ns=None, observe_hook=None,
                 close_hook=None, result=None, observation=None):
        self.observed_ns = observed_ns
        self.finished_ns = (observed_ns + 1 if finished_ns is None
                            else finished_ns)
        self.observe_hook = observe_hook
        self.close_hook = close_hook
        self.result = result
        self.observation = observation
        self.observations = 0
        self.closes = 0

    def observe_stderr(self, fd, identity):
        self.observations += 1
        if self.observe_hook is not None:
            self.observe_hook()
        return (self.observation if self.observation is not None else
                {"role": "stderr_parent", "fd": fd,
                 "identity": copy.deepcopy(identity),
                 "identity_matches": True, "closed": False,
                 "observed_ns": self.observed_ns})

    def close_stderr(self, fd, identity):
        self.closes += 1
        if self.close_hook is not None:
            self.close_hook()
        if self.result is not None:
            return self.result
        return {"closed": True, "finished_ns": self.finished_ns}


class SwappedSourceStderrReleaseBackend(SourceStderrReleaseBackend):
    pass


class SourcePidfdAnchorBackend:
    source_pidfd_anchor_mock = True

    def __init__(self, acquired_ns, anchor_fd=900, hook=None, result=None):
        self.acquired_ns = acquired_ns
        self.anchor_fd = anchor_fd
        self.hook = hook
        self.result = result
        self.calls = 0

    def acquire_anchor(self, primary_fd, provenance_sha256):
        self.calls += 1
        if self.hook is not None:
            self.hook()
        if self.result is not None:
            return self.result
        return {"primary_fd": primary_fd, "anchor_fd": self.anchor_fd,
                "acquired_ns": self.acquired_ns, "model_assumed": True}


class SwappedSourcePidfdAnchorBackend(SourcePidfdAnchorBackend):
    pass


class SourcePidfdReleaseBackend:
    source_pidfd_release_mock = True

    def __init__(self, checked_ns, finished_ns=None, same=True,
                 compare_hook=None, close_hook=None,
                 comparison=None, close_result=None):
        self.checked_ns = checked_ns
        self.finished_ns = (checked_ns + 1 if finished_ns is None
                            else finished_ns)
        self.same = same
        self.compare_hook = compare_hook
        self.close_hook = close_hook
        self.comparison = comparison
        self.close_result = close_result
        self.compares = 0
        self.closes = 0

    def compare_pidfd(self, primary_fd, anchor_fd, provenance_sha256):
        self.compares += 1
        if self.compare_hook is not None:
            self.compare_hook()
        if self.comparison is not None:
            return self.comparison
        return {"primary_fd": primary_fd, "anchor_fd": anchor_fd,
                "provenance_sha256": provenance_sha256,
                "same": self.same, "checked_ns": self.checked_ns,
                "model_assumed": True}

    def close_pidfd(self, primary_fd, anchor_fd, provenance_sha256):
        self.closes += 1
        if self.close_hook is not None:
            self.close_hook()
        if self.close_result is not None:
            return self.close_result
        return {"primary_fd": primary_fd, "anchor_fd": anchor_fd,
                "provenance_sha256": provenance_sha256,
                "closed": True, "finished_ns": self.finished_ns}


class SwappedSourcePidfdReleaseBackend(SourcePidfdReleaseBackend):
    pass


class FakeForkKernel:
    abort_fork_split_mock = True

    def __init__(self, handle, owner, mode="parent_first"):
        self.handle = handle
        self.owner = copy.deepcopy(owner)
        self.mode = mode
        self.now = 1000000100
        self.pid = owner["pid"] + 1000
        self.child_fds = {}
        self.child_closed = set()
        self.quarantined_fds = []
        self.parent_closed_roles = set()
        self.routine = None
        self.child_calls = ChildCalls(self)
        self.exit_code = None
        self.fork_calls = 0

    def monotonic_ns(self):
        self.now += 1
        return self.now

    def current_parent_identity(self):
        return copy.deepcopy(self.owner)

    def fork_once(self, context, routine_factory):
        self.fork_calls += 1
        for role, identity in self.handle.frozen_bundle.items():
            self.child_fds[role] = fcntl.fcntl(
                identity["fd"], fcntl.F_DUPFD_CLOEXEC, 3)
        self.routine = routine_factory(context)
        if self.mode == "fork_after_effect_error":
            raise OSError("ambiguous fork after effect")
        if self.mode == "grant_data":
            os.write(self.handle.frozen_bundle["grant_parent"]["fd"], b"G")
        if self.mode == "child_first":
            progress = self.advance_child(context["deadline_ns"])
            if progress["classification"] != "CHILD_WAITING_GRANT_EOF":
                raise AssertionError(progress)
        return self.pid

    def validate_descriptor(self, descriptor):
        if descriptor != self.routine.context["descriptor"]:
            raise OSError("descriptor drift")
        by_role = {item["role"]: item for item in descriptor["endpoints"]}
        for role, fd in self.child_fds.items():
            self.validate_child_fd(role, fd, by_role[role])
        return True

    def validate_child_fd(self, role, fd, expected):
        info = os.fstat(fd)
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fd_flags = fcntl.fcntl(fd, fcntl.F_GETFD)
        if (info.st_dev, info.st_ino, info.st_mode, flags,
                fd_flags & fcntl.FD_CLOEXEC) != (
                expected["dev"], expected["inode"], expected["mode"],
                expected["flags"], fcntl.FD_CLOEXEC):
            raise OSError("child FD identity drift")

    def parent_closed(self, role, identity):
        if role in self.parent_closed_roles:
            raise OSError("parent close replay")
        expected = self.handle.frozen_bundle[role]
        if identity != expected:
            raise OSError("parent close identity drift")
        self.parent_closed_roles.add(role)
        return True

    def advance_child(self, deadline_ns):
        if self.exit_code is not None:
            return {"classification": "CHILD_EXITED",
                    "exit_code": self.exit_code}
        if self.monotonic_ns() >= deadline_ns:
            raise TimeoutError("child scheduler deadline")
        try:
            return self.routine.step(self.child_calls)
        except ChildExit as exc:
            return {"classification": "CHILD_EXITED", "exit_code": exc.code}

    def child_status(self):
        return {"pid": self.pid, "exit_code": self.exit_code,
                "reaped": 0 if self.mode == "integer_reaped" else False}

    def wait_parent_readable(self, role, deadline_ns):
        fd = self.handle.frozen_bundle[role]["fd"]
        if self.monotonic_ns() >= deadline_ns:
            raise TimeoutError("parent read deadline")
        readable, _, _ = select.select([fd], [], [], 0.01)
        if not readable:
            raise TimeoutError("parent pipe not readable")
        return True

    def cleanup(self):
        for role, fd in list(self.child_fds.items()):
            if role not in self.child_closed:
                self.child_closed.add(role)
                del self.child_fds[role]
                try: os.close(fd)
                except OSError: pass


class FakeOwnedLinuxCalls:
    def __init__(self, owner, launcher, pid, fail_pidfd=False):
        self.owner = copy.deepcopy(owner)
        self.expected_launcher = copy.deepcopy(launcher)
        self.pid = pid
        self.fail_pidfd = fail_pidfd
        self.closed = []
        self.waits = []
        self.wait_options = []
    def current_identity(self): return copy.deepcopy(self.owner)
    def starttime(self, pid):
        if pid != self.pid: raise OSError("foreign pid")
        return 777
    def launcher(self, pid): return copy.deepcopy(self.expected_launcher)
    def pidfd_open(self, pid):
        if self.fail_pidfd: raise OSError("pidfd bind fault")
        if pid != self.pid: raise OSError("foreign pid")
        return 55
    def probe_waitable(self, pidfd):
        if pidfd != 55: raise OSError("foreign pidfd")
        return None
    def waitid(self, pidfd, options):
        if pidfd != 55: raise OSError("foreign pidfd")
        self.wait_options.append(options)
        return self.waits.pop(0) if self.waits else None
    def close(self, fd): self.closed.append(fd)


class AllocatedStepCalls:
    def __init__(self, owner, start):
        self.allocated_descendant_model_backend = True
        self.owner = copy.deepcopy(owner)
        self.times = list(range(start, start + 100))
        self.discovery = []
        self.identities = {}
        self.pidfd_results = []
        self.closed = []
        self.wait_all_calls = 0
        self.owner_hook = None
        self.pidfd_value = None
        self.pidfd_open_hook = None
        self.wait_pidfd_hook = None
        self.close_hook = None
        self.close_result = None
        self.wait_pidfd_calls = 0
    def owner_identity(self):
        if self.owner_hook is not None:
            self.owner_hook()
        return copy.deepcopy(self.owner)
    def kernel_thread_ids(self): return [self.owner["pid"]]
    def sigchld_policy(self):
        return {"handler_default": True, "ignored": False,
                "no_cldwait": False}
    def get_subreaper(self): return 1
    def monotonic_ns(self): return self.times.pop(0)
    def wait_all_wall_wnowait(self, _options):
        self.wait_all_calls += 1
        value = self.discovery.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value
    def child_identity(self, pid): return copy.deepcopy(self.identities[pid])
    def pidfd_open(self, pid):
        if self.pidfd_open_hook is not None:
            self.pidfd_open_hook()
        return pid + 1000 if self.pidfd_value is None else self.pidfd_value
    def wait_pidfd(self, _pidfd, _options):
        self.wait_pidfd_calls += 1
        value = self.pidfd_results.pop(0)
        if self.wait_pidfd_hook is not None:
            self.wait_pidfd_hook(self.wait_pidfd_calls)
        return value
    def close_pidfd(self, fd):
        self.closed.append(fd)
        if self.close_hook is not None:
            self.close_hook()
        return self.close_result


class AllocatedStatusCalls:
    def __init__(self, start, result=None):
        self.allocated_status_model_backend = True
        self.times = [start, start + 1]
        self.result = ({"kind": "EOF", "data": b""}
                       if result is None else result)
        self.clock_hook = None
        self.read_hook = None
        self.clock_calls = 0
        self.read_calls = 0
    def monotonic_ns(self):
        self.clock_calls += 1
        if self.clock_hook is not None:
            self.clock_hook(self.clock_calls)
        return self.times.pop(0)
    def read_status(self, _fd, maximum):
        self.read_calls += 1
        if self.read_hook is not None:
            self.read_hook()
        if isinstance(self.result, BaseException):
            raise self.result
        if maximum != 1:
            raise AssertionError("unexpected status read size")
        return self.result


class SwappedAllocatedStatusCalls(AllocatedStatusCalls):
    pass


class AllocatedDeadlineCalls:
    def __init__(self, now):
        self.allocated_release_deadline_model_backend = True
        self.now = now
        self.clock_calls = 0
        self.clock_hook = None

    def monotonic_ns(self):
        self.clock_calls += 1
        if self.clock_hook is not None:
            self.clock_hook()
        return self.now


class SwappedAllocatedDeadlineCalls(AllocatedDeadlineCalls):
    pass


@unittest.skipUnless(os.name == "posix" and hasattr(os, "pipe2"),
                     "real Linux pipe semantics required")
class AbortForkSplitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="slt-abort-fork-")
        self.parent_patch = mock.patch.object(
            BRIDGE.FILES, "PARENT", self.temp.name)
        self.parent_patch.start()
        self.backends = []
        self.handle = FDS.allocate_real_fd_graph(
            FDS.LinuxFDCalls(), FDS.CLOSE_ONLY_NO_WRITE)
        self.owner = self.handle.owner
        self.started = 1000000000
        self.deadline = 2000000000
        self.enrollment = GRAPH.enroll_allocated_no_grant_graph(
            self.handle, self.owner, "1" * 32, "2" * 32, "3" * 32,
            self.started, self.deadline)
        request = FIXTURE.request()
        request["boot_id"] = self.owner["boot_id"]
        request["request_id"] = self.enrollment.request_id
        run = request["launcher_identity"]["run_binding"]
        run.update({"request_id": self.enrollment.request_id,
                    "boot_id": self.owner["boot_id"],
                    "supervisor": copy.deepcopy(self.owner),
                    "unarmed_deadline_ns": self.deadline,
                    "fd_graph_digest": self.enrollment.graph_digest})
        intent = CONSUMER.encode_v2_intent(request, self.owner)
        bound = self.enrollment.bind_once(
            request, intent, self.started + 1)
        backend = BRIDGE.FILES.create_fresh_file_backend("d" * 32)
        self.backends.append(backend)
        bridge = BRIDGE.create_v2_intent_file_bridge(backend)
        self.persisted = GRAPH.persist_bound_intent_once(
            bound, bridge, self.started + 2)
        self.attempt = None
        self.kernel = None
        self.owned_calls = None

    def reset_with_shared_descendant(self):
        self.tearDown()
        self.temp = tempfile.TemporaryDirectory(prefix="slt-abort-fork-")
        self.parent_patch = mock.patch.object(
            BRIDGE.FILES, "PARENT", self.temp.name)
        self.parent_patch.start()
        self.backends = []
        self.handle = FDS.allocate_real_fd_graph(
            FDS.LinuxFDCalls(), FDS.CLOSE_ONLY_NO_WRITE)
        self.owner = self.handle.owner
        self.started = 1000000000
        self.deadline = 2000000000
        self.enrollment = GRAPH.enroll_allocated_no_grant_graph(
            self.handle, self.owner, "1" * 32, "2" * 32, "3" * 32,
            self.started, self.deadline)
        domain = DESC.ChildDomain(
            DESC._DOMAIN_KEY, self.owner, self.enrollment.request_id,
            "5" * 32, self.deadline + 100 * 1000000)
        domain.last_clock_ns = self.started
        self.shared_descendant = GRAPH.enroll_allocated_descendant_domain(
            self.enrollment, domain, 100, self.started + 1)
        request = FIXTURE.request()
        request["boot_id"] = self.owner["boot_id"]
        request["request_id"] = self.enrollment.request_id
        run = request["launcher_identity"]["run_binding"]
        run.update({"request_id": self.enrollment.request_id,
                    "boot_id": self.owner["boot_id"],
                    "supervisor": copy.deepcopy(self.owner),
                    "unarmed_deadline_ns": self.deadline,
                    "fd_graph_digest": self.enrollment.graph_digest})
        intent = CONSUMER.encode_v2_intent(request, self.owner)
        bound = self.enrollment.bind_once(
            request, intent, self.started + 1)
        backend = BRIDGE.FILES.create_fresh_file_backend("e" * 32)
        self.backends.append(backend)
        bridge = BRIDGE.create_v2_intent_file_bridge(backend)
        self.persisted = GRAPH.persist_bound_intent_once(
            bound, bridge, self.started + 2)
        self.attempt = None
        self.kernel = None
        self.owned_calls = None

    def tearDown(self):
        if (self.attempt is not None
                and self.attempt.capture_handle is not None):
            try:
                CAPTURE.close_capture_epoll(
                    self.attempt.capture_handle,
                    CAPTURE.LinuxCaptureSyscalls())
            except BaseException:
                pass
        if self.kernel is not None:
            self.kernel.cleanup()
        if self.handle.owned:
            try: self.handle.close_all_once()
            except BaseException: pass
        for backend in self.backends:
            if backend.root_fd is not None or backend.parent_fd is not None:
                try: backend.release_descriptors_preserving_state()
                except BaseException: pass
        self.parent_patch.stop()
        self.temp.cleanup()

    def run_mode(self, mode):
        self.attempt = LAB.prepare_abort_attempt(
            self.persisted, "4" * 32, self.started + 3)
        self.kernel = FakeForkKernel(self.handle, self.owner, mode)
        return self.attempt.run_mock_fork_split(self.kernel)

    def test_parent_first_and_child_first_prove_real_pipe_eof_abort(self):
        for mode in ("parent_first", "child_first"):
            with self.subTest(mode=mode):
                if mode != "parent_first":
                    self.tearDown(); self.setUp()
                result = self.run_mode(mode)
                self.assertEqual(result["classification"],
                                 "MOCK_ABORT_CHILD_EXITED_UNREAPED")
                self.assertEqual(result["exit_code"], LAB.EXIT_ABORT_EOF)
                self.assertEqual(result["grant_bytes"], 0)
                self.assertFalse(result["reaped"])
                self.assertFalse(result["terminal_success"])
                self.assertEqual(self.attempt.state,
                                 "CHILD_EXIT_OBSERVED_UNREAPED")
                self.assertTrue(self.attempt.ticket.used)
                self.assertEqual(self.handle.owned, set())

    def test_shared_descendant_records_exact_original_fork_once(self):
        self.reset_with_shared_descendant()
        result = self.run_mode("parent_first")
        shared = self.shared_descendant
        self.assertEqual(result["classification"],
                         "MOCK_ABORT_CHILD_EXITED_UNREAPED")
        self.assertEqual(shared.state, "FORKED")
        self.assertIs(shared.fork_attempt, self.attempt)
        self.assertIs(shared.fork_lifecycle, self.attempt.lifecycle)
        self.assertIs(shared.fork_origin,
                      self.attempt.lifecycle["origin"])
        self.assertIs(shared.fork_origin.pre_fork_enrollment,
                      self.enrollment.ticket)
        self.assertTrue(self.enrollment.ticket.used)
        self.assertEqual(shared.fork_pid, result["pid"])

    def test_shared_descendant_fork_record_replay_is_terminal(self):
        self.reset_with_shared_descendant()
        self.run_mode("parent_first")
        with self.assertRaises(GRAPH.Refusal):
            GRAPH.record_allocated_descendant_fork_once(
                self.shared_descendant, self.attempt,
                self.attempt.lifecycle, "CHILD_EXIT_OBSERVED_UNREAPED")
        self.assertEqual(self.shared_descendant.state, "UNKNOWN")
        self.assertTrue(self.enrollment._poisoned)
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")

    def test_shared_descendant_drift_after_fork_is_detected(self):
        self.reset_with_shared_descendant()
        self.run_mode("parent_first")
        self.shared_descendant.domain.domain_id = "9" * 32
        with self.assertRaises(GRAPH.Refusal):
            GRAPH.validate_allocated_descendant_fork(
                self.shared_descendant, self.attempt,
                "CHILD_EXIT_OBSERVED_UNREAPED")

    def test_shared_descendant_postfork_continuity_drift_is_terminal(self):
        attacks = ("owner", "watermark", "binding", "attempt")
        for attack in attacks:
            with self.subTest(attack=attack):
                if attack != attacks[0]:
                    self.tearDown(); self.setUp()
                self.reset_with_shared_descendant()
                self.run_mode("parent_first")
                if attack == "owner":
                    self.shared_descendant.domain.owner["pid"] += 1
                elif attack == "watermark":
                    self.shared_descendant.domain.last_clock_ns += 1
                elif attack == "binding":
                    self.enrollment.ticket.binding = object()
                else:
                    self.attempt._poison("TEST")
                with self.assertRaises(BaseException):
                    GRAPH.validate_allocated_descendant_fork(
                        self.shared_descendant, self.attempt,
                        "CHILD_EXIT_OBSERVED_UNREAPED")
                self.assertEqual(self.shared_descendant.state, "UNKNOWN")
                self.assertTrue(self.enrollment._poisoned)

    def test_shared_descendant_custom_lifecycle_values_have_no_callbacks(self):
        attacks = ("key", "state")
        for attack in attacks:
            with self.subTest(attack=attack):
                if attack != attacks[0]:
                    self.tearDown(); self.setUp()
                self.reset_with_shared_descendant()
                self.run_mode("parent_first")
                calls = []
                class EvilStr(str):
                    def __eq__(self, other):
                        calls.append("eq")
                        return super().__eq__(other)
                    __hash__ = str.__hash__
                lifecycle = self.attempt.lifecycle
                if attack == "key":
                    value = lifecycle.pop("state")
                    lifecycle[EvilStr("state")] = value
                else:
                    lifecycle["state"] = EvilStr("SPAWNED_UNBOUND")
                calls.clear()
                with self.assertRaises(BaseException):
                    GRAPH.validate_allocated_descendant_fork(
                        self.shared_descendant, self.attempt,
                        "CHILD_EXIT_OBSERVED_UNREAPED")
                self.assertEqual(calls, [])

    def test_shared_descendant_numeric_pid_aliases_are_refused(self):
        for target in ("origin", "child"):
            with self.subTest(target=target):
                if target != "origin":
                    self.tearDown(); self.setUp()
                self.reset_with_shared_descendant()
                self.run_mode("parent_first")
                if target == "origin":
                    self.attempt.lifecycle["origin"].pid = float(
                        self.attempt.returned_pid)
                else:
                    self.attempt.lifecycle["owned_child"]["pid"] = float(
                        self.attempt.returned_pid)
                with self.assertRaises(BaseException):
                    GRAPH.validate_allocated_descendant_fork(
                        self.shared_descendant, self.attempt,
                        "CHILD_EXIT_OBSERVED_UNREAPED")

    def test_shared_descendant_fake_pidfd_bound_state_is_refused(self):
        self.reset_with_shared_descendant()
        self.run_mode("parent_first")
        self.attempt.lifecycle["state"] = "PIDFD_BOUND"
        with self.assertRaises(BaseException):
            GRAPH.validate_allocated_descendant_fork(
                self.shared_descendant, self.attempt,
                "CHILD_EXIT_OBSERVED_UNREAPED")

    def test_shared_descendant_graph_owner_custom_container_has_no_callback(self):
        self.reset_with_shared_descendant()
        self.run_mode("parent_first")
        calls = []
        class EvilDict(dict):
            def items(self):
                calls.append("items")
                return super().items()
            def __deepcopy__(self, memo):
                calls.append("deepcopy")
                return dict(self)
        object.__setattr__(
            self.enrollment, "owner", EvilDict(self.enrollment.owner))
        with self.assertRaises(BaseException):
            GRAPH.validate_allocated_descendant_fork(
                self.shared_descendant, self.attempt,
                "CHILD_EXIT_OBSERVED_UNREAPED")
        self.assertEqual(calls, [])

    def test_shared_descendant_pidfd_starttime_and_phase_are_exact(self):
        attacks = ("starttime", "rollback")
        for attack in attacks:
            with self.subTest(attack=attack):
                if attack != attacks[0]:
                    self.tearDown(); self.setUp()
                self.reset_with_shared_descendant()
                self._bind_linux_unarmed_for_capture()
                lifecycle = self.attempt.lifecycle
                if attack == "starttime":
                    lifecycle["owned_child"]["starttime"] += 1
                else:
                    lifecycle["state"] = "SPAWNED_UNBOUND"
                    lifecycle["owned_child"]["starttime"] = None
                    lifecycle["origin"].claimed = False
                with self.assertRaises(BaseException):
                    GRAPH.validate_allocated_descendant_fork(
                        self.shared_descendant, self.attempt,
                        "LINUX_PIDFD_BOUND_UNARMED")
                self.assertEqual(self.shared_descendant.state, "UNKNOWN")

    def test_allocated_attachment_is_exact_and_non_drainable(self):
        self.reset_with_shared_descendant()
        self._prepare_linux_capture_for_grant_close()
        attachment = DBRIDGE.attach_allocated_owned_domain(
            self.shared_descendant, self.attempt)
        self.assertEqual(attachment.state, "ATTACHED_HARD_LIMIT_ONLY")
        self.assertIs(self.attempt.descendant_attachment, attachment)
        self.assertIs(attachment.pre_fork_enrollment,
                      self.enrollment.ticket)
        self.assertEqual(self.shared_descendant.state,
                         "ATTACHED_HARD_LIMIT_ONLY")
        self.assertTrue(self.shared_descendant.used)
        self.assertEqual(attachment.domain.state, "LAUNCHER_CLAIMED")
        self.assertIs(attachment.adapter.descendant_binding,
                      self.enrollment.binding)
        self.assertEqual(attachment.snapshot(), {
            "schema": 1,
            "classification": "ATTACHED_HARD_LIMIT_ONLY",
            "cleanup_hard_limit_ns":
                self.shared_descendant.cleanup_hard_limit_ns,
            "cleanup_deadline_bound": False,
            "descendant_drain_authorized": False,
            "runtime_authorized": False,
            "storage_authorized": False,
            "postcondition_verified": False})
        with self.assertRaises(DBRIDGE.Refusal):
            DBRIDGE.advance_descendant_domain(
                attachment, object(), object())
        with self.assertRaises(DBRIDGE.Refusal):
            copy.copy(attachment)

    def test_allocated_attachment_continues_through_monitor_and_reap(self):
        self.reset_with_shared_descendant()
        attachment, observation = self._complete_attached_linux_monitor()
        self.assertEqual(self.attempt.state,
                         "LINUX_MONITOR_COMPLETE_UNREAPED")
        self.assertIs(self.attempt.descendant_attachment, attachment)
        self.assertEqual(self.shared_descendant.state,
                         "ATTACHED_HARD_LIMIT_ONLY")
        self.owned_calls.waits = [observation]
        ticks = iter(range(self.started + 100, self.started + 200))
        with mock.patch.object(LAB.time, "monotonic_ns",
                               side_effect=lambda: next(ticks)):
            result = self.attempt.reap_linux_completion_once()
        self.assertEqual(result["classification"],
                         "LINUX_LEADER_REAPED_UNQUALIFIED")
        self.assertIsNotNone(self.attempt.settlement_handle)
        self.assertFalse(
            self.attempt.settlement_handle.descendant_capability.used)
        self.assertEqual(attachment.domain.state, "LAUNCHER_CLAIMED")

    def test_allocated_attachment_drift_before_reap_dispatches_zero_reap(self):
        self.reset_with_shared_descendant()
        self._complete_attached_linux_monitor()
        self.shared_descendant.domain.deadline_ns += 1
        self.owned_calls.waits = [types.SimpleNamespace(
            si_pid=self.attempt.returned_pid, si_code=os.CLD_EXITED,
            si_status=LAB.EXIT_ABORT_EOF)]
        before = len(self.owned_calls.wait_options)
        with mock.patch.object(LAB.time, "monotonic_ns",
                               return_value=self.started + 100):
            with self.assertRaises(BaseException):
                self.attempt.reap_linux_completion_once()
        self.assertEqual(len(self.owned_calls.wait_options), before)
        self.assertIsNone(self.attempt.settlement_handle)
        self.assertEqual(self.shared_descendant.state, "UNKNOWN")

    def test_allocated_attachment_drift_during_reap_retains_unknown(self):
        self.reset_with_shared_descendant()
        self._complete_attached_linux_monitor()
        observation = types.SimpleNamespace(
            si_pid=self.attempt.returned_pid, si_code=os.CLD_EXITED,
            si_status=LAB.EXIT_ABORT_EOF)
        self.owned_calls.waits = [observation]
        original = CAPTURE.reap_attached_completion_once
        def reap_then_drift(*args, **kwargs):
            settlement = original(*args, **kwargs)
            self.shared_descendant.domain.expected_leader["pid"] += 1
            return settlement
        ticks = iter(range(self.started + 100, self.started + 200))
        with mock.patch.object(
                CAPTURE, "reap_attached_completion_once",
                side_effect=reap_then_drift), \
                mock.patch.object(LAB.time, "monotonic_ns",
                                  side_effect=lambda: next(ticks)):
            result = self.attempt.reap_linux_completion_once()
        self.assertEqual(result["classification"],
                         "LINUX_REAP_UNKNOWN_RETAINED")
        self.assertIsNotNone(self.attempt.settlement_handle)
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")
        self.assertEqual(self.shared_descendant.state, "UNKNOWN")
        with self.assertRaises(BaseException):
            self.attempt.reap_linux_completion_once()

    def test_allocated_deadline_binding_is_exact_and_non_drainable(self):
        self.reset_with_shared_descendant()
        attachment = self._complete_attached_linux_reap()
        settlement = self.attempt.settlement_handle
        now = settlement.receipt["finished_ns"] + 1
        binding = DBRIDGE.bind_allocated_settlement_deadline(
            attachment, settlement,
            types.SimpleNamespace(monotonic_ns=lambda: now))
        self.assertIs(self.attempt.descendant_deadline_binding, binding)
        self.assertEqual(binding.state,
                         "DEADLINE_BOUND_NON_DRAINABLE")
        self.assertEqual(binding.deadline_ns,
                         self.attempt.cleanup_deadline_ns)
        self.assertEqual(binding.domain.deadline_ns, binding.deadline_ns)
        self.assertEqual(binding.domain.last_clock_ns, now)
        self.assertEqual(self.shared_descendant.state,
                         "DEADLINE_BOUND_NON_DRAINABLE")
        self.assertFalse(binding.descendant_capability.used)
        self.assertFalse(settlement.probe_used)
        self.assertEqual(binding.snapshot()["classification"],
                         "DEADLINE_BOUND_NON_DRAINABLE")
        self.assertFalse(binding.snapshot()["descendant_drain_authorized"])
        with self.assertRaises(DBRIDGE.Refusal):
            DBRIDGE.advance_descendant_domain(binding, settlement, object())
        with self.assertRaises(DBRIDGE.Refusal):
            copy.copy(binding)

    def test_allocated_deadline_binding_allows_exact_hard_limit(self):
        self.reset_with_shared_descendant()
        attachment = self._complete_attached_linux_reap(
            reap_start=self.deadline)
        settlement = self.attempt.settlement_handle
        self.assertEqual(self.attempt.cleanup_deadline_ns,
                         self.shared_descendant.cleanup_hard_limit_ns)
        now = settlement.receipt["finished_ns"] + 1
        binding = DBRIDGE.bind_allocated_settlement_deadline(
            attachment, settlement,
            types.SimpleNamespace(monotonic_ns=lambda: now))
        self.assertEqual(binding.deadline_ns, binding.hard_limit_ns)
        self.assertFalse(binding.snapshot()["descendant_drain_authorized"])

    def test_allocated_drain_transfer_consumes_once_without_domain_step(self):
        self.reset_with_shared_descendant()
        attachment = self._complete_attached_linux_reap()
        settlement = self.attempt.settlement_handle
        bind_now = settlement.receipt["finished_ns"] + 1
        binding = DBRIDGE.bind_allocated_settlement_deadline(
            attachment, settlement,
            types.SimpleNamespace(monotonic_ns=lambda: bind_now))
        calls = []
        bridge = DBRIDGE.transfer_allocated_descendant_drain(
            binding, settlement,
            types.SimpleNamespace(
                monotonic_ns=lambda: calls.append("clock") or bind_now + 1))
        self.assertEqual(calls, ["clock"])
        self.assertEqual(bridge.state, "ALLOCATED_DRAIN_READY")
        self.assertEqual(binding.domain.state, "DRAINING")
        self.assertEqual(binding.domain.records, [])
        self.assertIsNone(binding.domain.pending)
        self.assertIsNone(binding.domain.terminal_echild)
        self.assertTrue(binding.descendant_capability.used)
        self.assertTrue(settlement.probe_used)
        self.assertTrue(bridge.leader_capability.used)
        self.assertTrue(bridge.leader_claim.used)
        self.assertTrue(bridge.snapshot()["descendant_drain_authorized"])
        self.assertFalse(bridge.snapshot()["runtime_authorized"])
        self.assertFalse(bridge.snapshot()["storage_authorized"])
        with self.assertRaises(BaseException):
            DBRIDGE.transfer_allocated_descendant_drain(
                binding, settlement,
                types.SimpleNamespace(monotonic_ns=lambda: bind_now + 2))
        with self.assertRaises(DBRIDGE.Refusal):
            DBRIDGE.advance_descendant_domain(bridge, settlement, object())

    def test_allocated_drain_transfer_expiry_preserves_consumed_truth(self):
        self.reset_with_shared_descendant()
        attachment = self._complete_attached_linux_reap()
        settlement = self.attempt.settlement_handle
        bind_now = settlement.receipt["finished_ns"] + 1
        binding = DBRIDGE.bind_allocated_settlement_deadline(
            attachment, settlement,
            types.SimpleNamespace(monotonic_ns=lambda: bind_now))
        with self.assertRaises(BaseException):
            DBRIDGE.transfer_allocated_descendant_drain(
                binding, settlement,
                types.SimpleNamespace(
                    monotonic_ns=lambda: binding.deadline_ns))
        bridge = self.attempt.descendant_drain_bridge
        self.assertEqual(bridge.state, "UNKNOWN")
        self.assertTrue(bridge.settlement_transfer_confirmed)
        self.assertTrue(bridge.leader_transfer_confirmed)
        self.assertFalse(bridge.domain_accept_attempted)
        self.assertTrue(binding.descendant_capability.used)
        self.assertTrue(settlement.probe_used)
        self.assertTrue(bridge.leader_capability.used)
        self.assertEqual(attachment.adapter.state,
                         "UNKNOWN_ALLOCATED_DESCENDANT_DRAIN_TRANSFER")
        self.assertEqual(binding.domain.state, "UNKNOWN")
        with self.assertRaises(BaseException):
            DBRIDGE.transfer_allocated_descendant_drain(
                binding, settlement,
                types.SimpleNamespace(monotonic_ns=lambda: bind_now + 2))

    def test_allocated_drain_transfer_blocks_old_probe_before_clock(self):
        self.reset_with_shared_descendant()
        attachment = self._complete_attached_linux_reap()
        settlement = self.attempt.settlement_handle
        bind_now = settlement.receipt["finished_ns"] + 1
        binding = DBRIDGE.bind_allocated_settlement_deadline(
            attachment, settlement,
            types.SimpleNamespace(monotonic_ns=lambda: bind_now))
        observations = []
        def clock():
            with self.assertRaises(BaseException):
                CAPTURE.probe_children_after_reap(settlement, object())
            observations.append((settlement.probe_used,
                                 binding.descendant_capability.used,
                                 attachment.adapter.state))
            return bind_now + 1
        bridge = DBRIDGE.transfer_allocated_descendant_drain(
            binding, settlement,
            types.SimpleNamespace(monotonic_ns=clock))
        self.assertEqual(observations,
                         [(True, True, "DESCENDANT_HANDOFF")])
        self.assertEqual(bridge.state, "ALLOCATED_DRAIN_READY")

    def test_allocated_drain_wrong_settlement_has_zero_effect(self):
        self.reset_with_shared_descendant()
        attachment = self._complete_attached_linux_reap()
        settlement = self.attempt.settlement_handle
        bind_now = settlement.receipt["finished_ns"] + 1
        binding = DBRIDGE.bind_allocated_settlement_deadline(
            attachment, settlement,
            types.SimpleNamespace(monotonic_ns=lambda: bind_now))
        calls = []
        with self.assertRaises(BaseException):
            DBRIDGE.transfer_allocated_descendant_drain(
                binding, object(),
                types.SimpleNamespace(
                    monotonic_ns=lambda: calls.append("clock")))
        self.assertEqual(calls, [])
        self.assertFalse(binding.used)
        self.assertFalse(settlement.probe_used)
        self.assertFalse(binding.descendant_capability.used)
        self.assertFalse(self.attempt.descendant_drain_transfer_started)

    def test_allocated_drain_clock_pidfd_drift_blocks_domain_accept(self):
        self.reset_with_shared_descendant()
        attachment = self._complete_attached_linux_reap()
        settlement = self.attempt.settlement_handle
        bind_now = settlement.receipt["finished_ns"] + 1
        binding = DBRIDGE.bind_allocated_settlement_deadline(
            attachment, settlement,
            types.SimpleNamespace(monotonic_ns=lambda: bind_now))
        def clock():
            attachment.owned_handle.pidfd += 1
            return bind_now + 1
        with self.assertRaises(BaseException):
            DBRIDGE.transfer_allocated_descendant_drain(
                binding, settlement,
                types.SimpleNamespace(monotonic_ns=clock))
        bridge = self.attempt.descendant_drain_bridge
        self.assertEqual(bridge.state, "UNKNOWN")
        self.assertFalse(bridge.domain_accept_attempted)
        self.assertIsNone(bridge.leader_claim)
        self.assertEqual(binding.domain.state, "UNKNOWN")

    def test_allocated_drain_swallowed_bridge_poison_never_publishes(self):
        self.reset_with_shared_descendant()
        attachment = self._complete_attached_linux_reap()
        settlement = self.attempt.settlement_handle
        bind_now = settlement.receipt["finished_ns"] + 1
        binding = DBRIDGE.bind_allocated_settlement_deadline(
            attachment, settlement,
            types.SimpleNamespace(monotonic_ns=lambda: bind_now))
        def clock():
            self.attempt.descendant_drain_bridge._poison("injected")
            return bind_now + 1
        with self.assertRaises(BaseException):
            DBRIDGE.transfer_allocated_descendant_drain(
                binding, settlement,
                types.SimpleNamespace(monotonic_ns=clock))
        bridge = self.attempt.descendant_drain_bridge
        self.assertEqual(bridge.state, "UNKNOWN")
        self.assertEqual(bridge.error, "injected")
        self.assertFalse(bridge.domain_accept_attempted)
        self.assertNotEqual(self.shared_descendant.state,
                            "ALLOCATED_DRAIN_READY")

    def test_allocated_drain_custom_receipt_has_zero_callbacks(self):
        self.reset_with_shared_descendant()
        attachment = self._complete_attached_linux_reap()
        settlement = self.attempt.settlement_handle
        bind_now = settlement.receipt["finished_ns"] + 1
        binding = DBRIDGE.bind_allocated_settlement_deadline(
            attachment, settlement,
            types.SimpleNamespace(monotonic_ns=lambda: bind_now))
        calls = []
        class EvilDict(dict):
            def items(self):
                calls.append("items")
                return super().items()
        settlement.receipt = EvilDict(settlement.receipt)
        with self.assertRaises(BaseException):
            DBRIDGE.transfer_allocated_descendant_drain(
                binding, settlement,
                types.SimpleNamespace(
                    monotonic_ns=lambda: calls.append("clock")))
        self.assertEqual(calls, [])
        self.assertFalse(binding.used)
        self.assertFalse(binding.descendant_capability.used)

    def test_allocated_drain_nested_validator_override_has_zero_effect(self):
        self.reset_with_shared_descendant()
        attachment = self._complete_attached_linux_reap()
        settlement = self.attempt.settlement_handle
        bind_now = settlement.receipt["finished_ns"] + 1
        binding = DBRIDGE.bind_allocated_settlement_deadline(
            attachment, settlement,
            types.SimpleNamespace(monotonic_ns=lambda: bind_now))
        calls = []
        object.__setattr__(
            self.attempt.persisted, "_validate_frozen",
            lambda phase: calls.append("nested"))
        with self.assertRaises(BaseException):
            DBRIDGE.transfer_allocated_descendant_drain(
                binding, settlement,
                types.SimpleNamespace(
                    monotonic_ns=lambda: calls.append("clock")))
        self.assertEqual(calls, [])
        self.assertFalse(binding.used)
        self.assertFalse(settlement.probe_used)
        self.assertFalse(binding.descendant_capability.used)

    def test_allocated_drain_nested_fd_override_has_zero_effect(self):
        self.reset_with_shared_descendant()
        attachment = self._complete_attached_linux_reap()
        settlement = self.attempt.settlement_handle
        bind_now = settlement.receipt["finished_ns"] + 1
        binding = DBRIDGE.bind_allocated_settlement_deadline(
            attachment, settlement,
            types.SimpleNamespace(monotonic_ns=lambda: bind_now))
        calls = []
        object.__setattr__(
            self.attempt.handle, "_validate_stored_role",
            lambda role: calls.append(role))
        with self.assertRaises(BaseException):
            DBRIDGE.transfer_allocated_descendant_drain(
                binding, settlement,
                types.SimpleNamespace(
                    monotonic_ns=lambda: calls.append("clock")))
        self.assertEqual(calls, [])
        self.assertFalse(binding.used)
        self.assertFalse(settlement.probe_used)

    def test_allocated_drain_custom_domain_scalar_has_zero_effect(self):
        self.reset_with_shared_descendant()
        attachment = self._complete_attached_linux_reap()
        settlement = self.attempt.settlement_handle
        bind_now = settlement.receipt["finished_ns"] + 1
        binding = DBRIDGE.bind_allocated_settlement_deadline(
            attachment, settlement,
            types.SimpleNamespace(monotonic_ns=lambda: bind_now))
        calls = []
        class EvilStr(str):
            def __eq__(self, other):
                calls.append("eq")
                return str(self) == other
        binding.domain.request_id = EvilStr(binding.domain.request_id)
        with self.assertRaises(BaseException):
            DBRIDGE.transfer_allocated_descendant_drain(
                binding, settlement,
                types.SimpleNamespace(
                    monotonic_ns=lambda: calls.append("clock")))
        self.assertEqual(calls, [])
        self.assertFalse(binding.used)
        self.assertFalse(settlement.probe_used)

    def test_allocated_drain_custom_domain_deadline_has_zero_effect(self):
        self.reset_with_shared_descendant()
        attachment = self._complete_attached_linux_reap()
        settlement = self.attempt.settlement_handle
        bind_now = settlement.receipt["finished_ns"] + 1
        binding = DBRIDGE.bind_allocated_settlement_deadline(
            attachment, settlement,
            types.SimpleNamespace(monotonic_ns=lambda: bind_now))
        calls = []
        class EvilInt(int):
            def __eq__(self, other):
                calls.append("eq")
                return int(self) == other
        binding.domain.deadline_ns = EvilInt(binding.domain.deadline_ns)
        with self.assertRaises(BaseException):
            DBRIDGE.transfer_allocated_descendant_drain(
                binding, settlement,
                types.SimpleNamespace(
                    monotonic_ns=lambda: calls.append("clock")))
        self.assertEqual(calls, [])
        self.assertFalse(binding.used)
        self.assertFalse(settlement.probe_used)

    def test_allocated_drain_custom_adapter_state_has_zero_effect(self):
        self.reset_with_shared_descendant()
        attachment = self._complete_attached_linux_reap()
        settlement = self.attempt.settlement_handle
        bind_now = settlement.receipt["finished_ns"] + 1
        binding = DBRIDGE.bind_allocated_settlement_deadline(
            attachment, settlement,
            types.SimpleNamespace(monotonic_ns=lambda: bind_now))
        calls = []
        class EvilStr(str):
            def __eq__(self, other):
                calls.append("eq")
                return str(self) == other
        attachment.adapter.state = EvilStr("REAPED")
        with self.assertRaises(BaseException):
            DBRIDGE.transfer_allocated_descendant_drain(
                binding, settlement,
                types.SimpleNamespace(
                    monotonic_ns=lambda: calls.append("clock")))
        self.assertEqual(calls, [])
        self.assertFalse(binding.used)
        self.assertFalse(settlement.probe_used)

    def test_allocated_drain_custom_settlement_deadline_has_zero_effect(self):
        self.reset_with_shared_descendant()
        attachment = self._complete_attached_linux_reap()
        settlement = self.attempt.settlement_handle
        bind_now = settlement.receipt["finished_ns"] + 1
        binding = DBRIDGE.bind_allocated_settlement_deadline(
            attachment, settlement,
            types.SimpleNamespace(monotonic_ns=lambda: bind_now))
        calls = []
        class EvilInt(int):
            def __eq__(self, other):
                calls.append("eq")
                return int(self) == other
        settlement.deadline_ns = EvilInt(settlement.deadline_ns)
        with self.assertRaises(BaseException):
            DBRIDGE.transfer_allocated_descendant_drain(
                binding, settlement,
                types.SimpleNamespace(
                    monotonic_ns=lambda: calls.append("clock")))
        self.assertEqual(calls, [])
        self.assertFalse(binding.used)
        self.assertFalse(settlement.probe_used)
        self.assertFalse(binding.descendant_capability.used)

    def test_allocated_drain_custom_fd_namespace_key_has_zero_effect(self):
        self.reset_with_shared_descendant()
        attachment = self._complete_attached_linux_reap()
        settlement = self.attempt.settlement_handle
        bind_now = settlement.receipt["finished_ns"] + 1
        binding = DBRIDGE.bind_allocated_settlement_deadline(
            attachment, settlement,
            types.SimpleNamespace(monotonic_ns=lambda: bind_now))
        calls = []
        class EvilStr(str):
            def __eq__(self, other):
                calls.append("eq")
                return str(self) == other
            __hash__ = str.__hash__
        self.attempt.handle.__dict__[EvilStr("injected_key")] = True
        with self.assertRaises(BaseException):
            DBRIDGE.transfer_allocated_descendant_drain(
                binding, settlement,
                types.SimpleNamespace(
                    monotonic_ns=lambda: calls.append("clock")))
        self.assertEqual(calls, [])
        self.assertFalse(binding.used)
        self.assertFalse(settlement.probe_used)

    def test_allocated_drain_clock_custom_leader_has_zero_callbacks(self):
        self.reset_with_shared_descendant()
        attachment = self._complete_attached_linux_reap()
        settlement = self.attempt.settlement_handle
        bind_now = settlement.receipt["finished_ns"] + 1
        binding = DBRIDGE.bind_allocated_settlement_deadline(
            attachment, settlement,
            types.SimpleNamespace(monotonic_ns=lambda: bind_now))
        calls = []
        class EvilDict(dict):
            def __eq__(self, other):
                calls.append("eq")
                return dict(self) == other
        def clock():
            attachment.adapter.reap_capability.leader = EvilDict(
                attachment.adapter.reap_capability.leader)
            return bind_now + 1
        with self.assertRaises(BaseException):
            DBRIDGE.transfer_allocated_descendant_drain(
                binding, settlement,
                types.SimpleNamespace(monotonic_ns=clock))
        self.assertEqual(calls, [])
        self.assertFalse(
            self.attempt.descendant_drain_bridge.domain_accept_attempted)

    def test_allocated_drain_clock_owner_drift_blocks_accept(self):
        self.reset_with_shared_descendant()
        attachment = self._complete_attached_linux_reap()
        settlement = self.attempt.settlement_handle
        bind_now = settlement.receipt["finished_ns"] + 1
        binding = DBRIDGE.bind_allocated_settlement_deadline(
            attachment, settlement,
            types.SimpleNamespace(monotonic_ns=lambda: bind_now))
        def clock():
            settlement.owner["pid"] += 1
            return bind_now + 1
        with self.assertRaises(BaseException):
            DBRIDGE.transfer_allocated_descendant_drain(
                binding, settlement,
                types.SimpleNamespace(monotonic_ns=clock))
        bridge = self.attempt.descendant_drain_bridge
        self.assertTrue(bridge.settlement_transfer_confirmed)
        self.assertTrue(bridge.leader_transfer_confirmed)
        self.assertFalse(bridge.domain_accept_attempted)
        self.assertEqual(bridge.state, "UNKNOWN")

    def test_allocated_drain_clock_custom_timestamp_has_no_callback(self):
        self.reset_with_shared_descendant()
        attachment = self._complete_attached_linux_reap()
        settlement = self.attempt.settlement_handle
        bind_now = settlement.receipt["finished_ns"] + 1
        binding = DBRIDGE.bind_allocated_settlement_deadline(
            attachment, settlement,
            types.SimpleNamespace(monotonic_ns=lambda: bind_now))
        calls = []
        class EvilInt(int):
            def __eq__(self, other):
                calls.append("eq")
                return int(self) == other
            def __ge__(self, other):
                calls.append("ge")
                return int(self) >= other
        def clock():
            binding.descendant_capability.finished_ns = EvilInt(
                binding.descendant_capability.finished_ns)
            return bind_now + 1
        with self.assertRaises(BaseException):
            DBRIDGE.transfer_allocated_descendant_drain(
                binding, settlement,
                types.SimpleNamespace(monotonic_ns=clock))
        self.assertEqual(calls, [])
        self.assertFalse(
            self.attempt.descendant_drain_bridge.domain_accept_attempted)

    def test_allocated_step_pending_is_one_shot_nonqualified(self):
        self.reset_with_shared_descendant()
        bridge, watermark = self._complete_allocated_drain_ready()
        calls = AllocatedStepCalls(self.owner, watermark + 1)
        calls.discovery = [None]
        result = DBRIDGE.advance_allocated_descendant_once(bridge, calls)
        self.assertEqual(result.state,
                         "ALLOCATED_STEP_PENDING_NONQUALIFIED")
        self.assertEqual(result.receipt["outcome"], "CHILDREN_PENDING")
        self.assertEqual(calls.wait_all_calls, 1)
        self.assertFalse(result.snapshot()["descendants_qualified"])
        self.assertFalse(result.snapshot()["runtime_authorized"])
        before = calls.wait_all_calls
        with self.assertRaises(BaseException):
            DBRIDGE.advance_allocated_descendant_once(bridge, calls)
        self.assertEqual(calls.wait_all_calls, before)

    def test_allocated_step_echild_is_model_only_nonqualified(self):
        self.reset_with_shared_descendant()
        bridge, watermark = self._complete_allocated_drain_ready()
        calls = AllocatedStepCalls(self.owner, watermark + 1)
        calls.discovery = [ChildProcessError(errno.ECHILD, "none")]
        result = DBRIDGE.advance_allocated_descendant_once(bridge, calls)
        self.assertEqual(result.state,
                         "ALLOCATED_MODEL_DOMAIN_DRAINED_NONQUALIFIED")
        self.assertEqual(result.receipt["outcome"],
                         "MODEL_CHILD_DOMAIN_DRAINED")
        self.assertEqual(bridge.domain.state, "DRAINED")
        self.assertEqual(calls.wait_all_calls, 1)
        self.assertFalse(result.snapshot()["descendants_qualified"])
        self.assertFalse(result.snapshot()["resources_closed"])
        evidence = result.terminal_evidence
        self.assertIsInstance(
            evidence, DBRIDGE.AllocatedTerminalDomainEvidence)
        self.assertIs(self.attempt.descendant_terminal_evidence, evidence)
        self.assertTrue(
            DBRIDGE.validate_allocated_terminal_evidence(evidence))
        self.assertEqual(evidence.snapshot()["classification"],
                         "ORIGINAL_MODEL_ECHILD_SEALED")
        self.assertFalse(evidence.snapshot()["resources_closed"])

    def test_allocated_terminal_evidence_is_private_noncopyable_and_sealed(self):
        self.reset_with_shared_descendant()
        bridge, watermark = self._complete_allocated_drain_ready()
        calls = AllocatedStepCalls(self.owner, watermark + 1)
        calls.discovery = [ChildProcessError(errno.ECHILD, "none")]
        result = DBRIDGE.advance_allocated_descendant_once(bridge, calls)
        evidence = result.terminal_evidence
        with self.assertRaises(BaseException):
            DBRIDGE.AllocatedTerminalDomainEvidence(object(), result)
        with self.assertRaises(BaseException):
            copy.copy(evidence)
        with self.assertRaises(BaseException):
            copy.deepcopy(evidence)
        with self.assertRaises(BaseException):
            evidence._deadline_ns += 1
        with self.assertRaises(BaseException):
            del evidence._receipt_bytes
        with self.assertRaises(BaseException):
            result.terminal_evidence = object()

    def test_allocated_terminal_evidence_rejects_mutable_result_rewrite(self):
        self.reset_with_shared_descendant()
        bridge, watermark = self._complete_allocated_drain_ready()
        calls = AllocatedStepCalls(self.owner, watermark + 1)
        calls.discovery = [ChildProcessError(errno.ECHILD, "none")]
        result = DBRIDGE.advance_allocated_descendant_once(bridge, calls)
        evidence = result.terminal_evidence
        result.receipt["outcome"] = "CHILDREN_PENDING"
        result.receipt_bytes = LAB.SUP.canonical(result.receipt)
        with self.assertRaises(BaseException):
            DBRIDGE.validate_allocated_terminal_evidence(evidence)

    def test_allocated_terminal_evidence_rejects_domain_reconstruction(self):
        self.reset_with_shared_descendant()
        bridge, watermark = self._complete_allocated_drain_ready()
        calls = AllocatedStepCalls(self.owner, watermark + 1)
        calls.discovery = [ChildProcessError(errno.ECHILD, "none")]
        result = DBRIDGE.advance_allocated_descendant_once(bridge, calls)
        evidence = result.terminal_evidence
        bridge.domain.terminal_echild = copy.deepcopy(
            bridge.domain.terminal_echild)
        bridge.domain.terminal_echild["observed_ns"] += 1
        with self.assertRaises(BaseException):
            DBRIDGE.validate_allocated_terminal_evidence(evidence)

    def test_allocated_terminal_evidence_rejects_outer_authority_drift(self):
        mutators = (
            lambda result, bridge: bridge.domain.owner.__setitem__(
                "pid", bridge.domain.owner["pid"] + 1),
            lambda result, bridge: bridge.attempt.handle.owned.remove(
                "stdout_parent"),
            lambda result, bridge: setattr(
                bridge.attempt, "poisoned", True),
            lambda result, bridge: setattr(
                bridge.domain, "request_id", "foreign-request"),
            lambda result, bridge:
                bridge.attachment.owned_handle.child.__setitem__(
                    "starttime",
                    bridge.attachment.owned_handle.child["starttime"] + 1),
            lambda result, bridge: setattr(bridge, "domain", object()),
            lambda result, bridge: bridge.attempt.handle._poison(),
            lambda result, bridge: setattr(
                bridge.attempt.handle, "active", "injected"),
            lambda result, bridge: setattr(
                bridge.attachment.owned_handle.lifecycle["origin"],
                "pid", bridge.domain.leader["pid"] + 1),
            lambda result, bridge: setattr(
                bridge.attachment.owned_handle.lifecycle["origin"],
                "claimed", False))
        for mutate in mutators:
            with self.subTest(mutate=mutate):
                self.reset_with_shared_descendant()
                bridge, watermark = self._complete_allocated_drain_ready()
                calls = AllocatedStepCalls(self.owner, watermark + 1)
                calls.discovery = [ChildProcessError(errno.ECHILD, "none")]
                result = DBRIDGE.advance_allocated_descendant_once(
                    bridge, calls)
                evidence = result.terminal_evidence
                if mutate is mutators[2]:
                    self.attempt._set("poisoned", True)
                else:
                    mutate(result, bridge)
                with self.assertRaises(BaseException):
                    DBRIDGE.validate_allocated_terminal_evidence(evidence)

    def test_allocated_terminal_evidence_rejects_closed_original_epoll(self):
        self.reset_with_shared_descendant()
        bridge, watermark = self._complete_allocated_drain_ready()
        calls = AllocatedStepCalls(self.owner, watermark + 1)
        calls.discovery = [ChildProcessError(errno.ECHILD, "none")]
        result = DBRIDGE.advance_allocated_descendant_once(bridge, calls)
        evidence = result.terminal_evidence
        bridge.attempt.capture_handle.poller.close()
        with self.assertRaises(BaseException):
            DBRIDGE.validate_allocated_terminal_evidence(evidence)

    def test_allocated_terminal_nested_custom_receipt_has_no_callback(self):
        self.reset_with_shared_descendant()
        bridge, watermark = self._complete_allocated_drain_ready()
        calls = AllocatedStepCalls(self.owner, watermark + 1)
        calls.discovery = [ChildProcessError(errno.ECHILD, "none")]
        result = DBRIDGE.advance_allocated_descendant_once(bridge, calls)
        evidence = result.terminal_evidence
        observed = []
        class EvilDict(dict):
            def items(self):
                observed.append("items")
                return super().items()
        result.receipt["owner"] = EvilDict(result.receipt["owner"])
        with self.assertRaises(BaseException):
            DBRIDGE.validate_allocated_terminal_evidence(evidence)
        self.assertEqual(observed, [])

    def test_allocated_terminal_custom_deadline_has_no_comparison_callback(self):
        self.reset_with_shared_descendant()
        bridge, watermark = self._complete_allocated_drain_ready()
        calls = AllocatedStepCalls(self.owner, watermark + 1)
        calls.discovery = [ChildProcessError(errno.ECHILD, "none")]
        result = DBRIDGE.advance_allocated_descendant_once(bridge, calls)
        evidence = result.terminal_evidence
        observed = []
        class EvilInt(int):
            def __eq__(self, other):
                observed.append("eq")
                return int(self) == other
            def __le__(self, other):
                observed.append("le")
                return int(self) <= other
        bridge.domain.deadline_ns = EvilInt(bridge.domain.deadline_ns)
        with self.assertRaises(BaseException):
            DBRIDGE.validate_allocated_terminal_evidence(evidence)
        self.assertEqual(observed, [])

    def test_allocated_pending_and_adopted_mint_no_terminal_evidence(self):
        self.reset_with_shared_descendant()
        bridge, watermark = self._complete_allocated_drain_ready()
        calls = AllocatedStepCalls(self.owner, watermark + 1)
        calls.discovery = [None]
        result = DBRIDGE.advance_allocated_descendant_once(bridge, calls)
        self.assertIsNone(result.terminal_evidence)
        self.assertIsNone(self.attempt.descendant_terminal_evidence)

        self.reset_with_shared_descendant()
        bridge, watermark = self._complete_allocated_drain_ready()
        calls = AllocatedStepCalls(self.owner, watermark + 1)
        child = self.owner["pid"] + 200
        terminal = types.SimpleNamespace(
            si_pid=child, si_code=os.CLD_EXITED, si_status=0)
        calls.discovery = [terminal]
        calls.identities[child] = {
            "pid": child, "starttime": 888,
            "boot_id": self.owner["boot_id"], "ppid": self.owner["pid"]}
        calls.pidfd_results = [terminal, terminal]
        result = DBRIDGE.advance_allocated_descendant_once(bridge, calls)
        self.assertIsNone(result.terminal_evidence)
        self.assertIsNone(self.attempt.descendant_terminal_evidence)

    def test_allocated_status_exact_eof_is_sealed_nonqualified(self):
        self.reset_with_shared_descendant()
        terminal = self._complete_allocated_terminal_evidence()
        calls = AllocatedStatusCalls(terminal._watermark_ns + 1)
        result = APROTO.observe_allocated_status_tail_once(terminal, calls)
        self.assertEqual(result.state,
                         "MODEL_ALLOCATED_STATUS_EOF_NONQUALIFIED")
        self.assertEqual(calls.clock_calls, 2)
        self.assertEqual(calls.read_calls, 1)
        self.assertTrue(result.snapshot()["status_eof_verified"])
        self.assertFalse(result.snapshot()["abort_protocol_verified"])
        self.assertFalse(result.snapshot()["resources_closed"])
        self.assertTrue(
            APROTO.validate_allocated_status_eof(result.eof_evidence))
        self.assertIs(self.attempt.allocated_status_eof_evidence,
                      result.eof_evidence)
        self.assertTrue(terminal._consumed)
        self.assertIs(terminal._consumer, result)
        with self.assertRaises(BaseException):
            copy.copy(result.eof_evidence)
        with self.assertRaises(BaseException):
            APROTO.observe_allocated_status_tail_once(terminal, calls)
        self.assertEqual(calls.read_calls, 1)

    def test_allocated_status_data_and_eagain_never_mint_eof(self):
        for kind, data, classification in (
                ("DATA", b"X", "MODEL_ALLOCATED_STATUS_DATA_MISMATCH"),
                ("EAGAIN", b"",
                 "MODEL_ALLOCATED_STATUS_EAGAIN_NONQUALIFIED")):
            with self.subTest(kind=kind):
                self.reset_with_shared_descendant()
                terminal = self._complete_allocated_terminal_evidence()
                calls = AllocatedStatusCalls(
                    terminal._watermark_ns + 1,
                    {"kind": kind, "data": data})
                result = APROTO.observe_allocated_status_tail_once(
                    terminal, calls)
                self.assertEqual(result.state, classification)
                self.assertIsNone(result.eof_evidence)
                self.assertIsNone(
                    self.attempt.allocated_status_eof_evidence)
                with self.assertRaises(BaseException):
                    APROTO.observe_allocated_status_tail_once(
                        terminal, calls)
                self.assertEqual(calls.read_calls, 1)

    def test_allocated_status_malformed_custom_result_has_no_callback(self):
        self.reset_with_shared_descendant()
        terminal = self._complete_allocated_terminal_evidence()
        observed = []
        class EvilDict(dict):
            def __iter__(self):
                observed.append("iter")
                return super().__iter__()
            def items(self):
                observed.append("items")
                return super().items()
        calls = AllocatedStatusCalls(
            terminal._watermark_ns + 1,
            EvilDict({"kind": "EOF", "data": b""}))
        with self.assertRaises(BaseException):
            APROTO.observe_allocated_status_tail_once(terminal, calls)
        self.assertEqual(observed, [])
        self.assertEqual(calls.read_calls, 1)
        self.assertTrue(self.attempt.allocated_status_result.read_attempted)
        self.assertFalse(self.attempt.allocated_status_result.read_returned)
        self.assertEqual(self.attempt.allocated_status_result.state,
                         "UNKNOWN")

    def test_allocated_status_read_effect_then_drift_is_unknown_no_retry(self):
        self.reset_with_shared_descendant()
        terminal = self._complete_allocated_terminal_evidence()
        calls = AllocatedStatusCalls(terminal._watermark_ns + 1)
        def drift():
            self.attempt.handle.owned.remove("stdout_parent")
        calls.read_hook = drift
        with self.assertRaises(BaseException):
            APROTO.observe_allocated_status_tail_once(terminal, calls)
        result = self.attempt.allocated_status_result
        self.assertTrue(result.read_attempted)
        self.assertTrue(result.read_returned)
        self.assertEqual(result.read_kind, "EOF")
        self.assertEqual(result.read_data, b"")
        self.assertEqual(result.state, "UNKNOWN")
        with self.assertRaises(BaseException):
            APROTO.observe_allocated_status_tail_once(terminal, calls)
        self.assertEqual(calls.read_calls, 1)

    def test_allocated_status_deadline_after_eof_retains_unknown(self):
        self.reset_with_shared_descendant()
        terminal = self._complete_allocated_terminal_evidence()
        calls = AllocatedStatusCalls(terminal._watermark_ns + 1)
        calls.times[1] = terminal._deadline_ns
        with self.assertRaises(BaseException):
            APROTO.observe_allocated_status_tail_once(terminal, calls)
        result = self.attempt.allocated_status_result
        self.assertTrue(result.read_returned)
        self.assertEqual(result.read_kind, "EOF")
        self.assertIsNone(result.eof_evidence)
        self.assertEqual(result.state, "UNKNOWN")

    def test_allocated_status_pre_read_authority_drift_dispatches_no_read(self):
        self.reset_with_shared_descendant()
        terminal = self._complete_allocated_terminal_evidence()
        calls = AllocatedStatusCalls(terminal._watermark_ns + 1)
        calls.clock_hook = lambda count: (
            self.attempt.handle.owned.remove("stdout_parent")
            if count == 1 else None)
        with self.assertRaises(BaseException):
            APROTO.observe_allocated_status_tail_once(terminal, calls)
        self.assertEqual(calls.read_calls, 0)
        self.assertEqual(self.attempt.allocated_status_result.state,
                         "UNKNOWN")

    def test_allocated_status_data_cannot_be_rewritten_to_eof(self):
        self.reset_with_shared_descendant()
        terminal = self._complete_allocated_terminal_evidence()
        calls = AllocatedStatusCalls(
            terminal._watermark_ns + 1,
            {"kind": "DATA", "data": b"X"})
        def rewrite(count):
            if count == 2:
                operation = self.attempt.allocated_status_result
                operation.read_kind = "EOF"
                operation.read_data = b""
        calls.clock_hook = rewrite
        with self.assertRaises(BaseException):
            APROTO.observe_allocated_status_tail_once(terminal, calls)
        result = self.attempt.allocated_status_result
        self.assertEqual(result._read_observation, ("DATA", b"X"))
        self.assertIsNone(result.eof_evidence)
        self.assertEqual(result.state, "UNKNOWN")

    def test_allocated_status_swallowed_reentry_poisons_outer(self):
        for boundary in ("clock", "read"):
            with self.subTest(boundary=boundary):
                self.reset_with_shared_descendant()
                terminal = self._complete_allocated_terminal_evidence()
                calls = AllocatedStatusCalls(terminal._watermark_ns + 1)
                def reenter():
                    try:
                        APROTO.observe_allocated_status_tail_once(
                            terminal, calls)
                    except BaseException:
                        pass
                if boundary == "clock":
                    calls.clock_hook = lambda count: (
                        reenter() if count == 1 else None)
                else:
                    calls.read_hook = reenter
                with self.assertRaises(BaseException):
                    APROTO.observe_allocated_status_tail_once(
                        terminal, calls)
                result = self.attempt.allocated_status_result
                self.assertEqual(result.state, "UNKNOWN")
                self.assertIsNone(result.eof_evidence)
                self.assertLessEqual(calls.read_calls, 1)

    def test_allocated_status_custom_finished_time_has_no_callback(self):
        self.reset_with_shared_descendant()
        terminal = self._complete_allocated_terminal_evidence()
        observed = []
        class EvilInt(int):
            def __lt__(self, other):
                observed.append("lt")
                return int(self) < other
            def __gt__(self, other):
                observed.append("gt")
                return int(self) > other
        terminal._source.descendant_capability.finished_ns = EvilInt(
            terminal._source.descendant_capability.finished_ns)
        calls = AllocatedStatusCalls(terminal._watermark_ns + 1)
        with self.assertRaises(BaseException):
            APROTO.observe_allocated_status_tail_once(terminal, calls)
        self.assertEqual(observed, [])
        self.assertEqual(calls.clock_calls, 0)
        self.assertEqual(calls.read_calls, 0)

    def test_allocated_status_backend_custom_namespace_getter_not_run(self):
        self.reset_with_shared_descendant()
        terminal = self._complete_allocated_terminal_evidence()
        observed = []
        class EvilBackend:
            allocated_status_model_backend = True
            @property
            def __dict__(self):
                observed.append("dict")
                return {}
            def monotonic_ns(self): return terminal._watermark_ns + 1
            def read_status(self, _fd, _maximum):
                return {"kind": "EOF", "data": b""}
        with self.assertRaises(BaseException):
            APROTO.observe_allocated_status_tail_once(
                terminal, EvilBackend())
        self.assertEqual(observed, [])

    def test_allocated_status_eof_validator_custom_state_has_no_callback(self):
        self.reset_with_shared_descendant()
        terminal = self._complete_allocated_terminal_evidence()
        calls = AllocatedStatusCalls(terminal._watermark_ns + 1)
        result = APROTO.observe_allocated_status_tail_once(terminal, calls)
        observed = []
        class EvilStr(str):
            def __eq__(self, other):
                observed.append("eq")
                return str(self) == other
        result.state = EvilStr(result.state)
        with self.assertRaises(BaseException):
            APROTO.validate_allocated_status_eof(result.eof_evidence)
        self.assertEqual(observed, [])

    def test_allocated_status_read_latch_survives_callback_rewrite(self):
        self.reset_with_shared_descendant()
        terminal = self._complete_allocated_terminal_evidence()
        calls = AllocatedStatusCalls(terminal._watermark_ns + 1)
        def rewrite_then_fail():
            self.attempt.allocated_status_result.read_attempted = False
            raise OSError("ambiguous modeled read")
        calls.read_hook = rewrite_then_fail
        with self.assertRaises(OSError):
            APROTO.observe_allocated_status_tail_once(terminal, calls)
        result = self.attempt.allocated_status_result
        self.assertTrue(result._read_latched)
        self.assertTrue(result.read_attempted)
        self.assertFalse(result.read_returned)
        self.assertEqual(result.state, "UNKNOWN")

    def test_allocated_status_state_dodge_cannot_hide_reentry(self):
        self.reset_with_shared_descendant()
        terminal = self._complete_allocated_terminal_evidence()
        calls = AllocatedStatusCalls(terminal._watermark_ns + 1)
        def dodge_and_reenter():
            operation = self.attempt.allocated_status_result
            operation.state = "DONE"
            try:
                APROTO.observe_allocated_status_tail_once(terminal, calls)
            except BaseException:
                pass
            operation.state = "OBSERVING"
        calls.read_hook = dodge_and_reenter
        with self.assertRaises(BaseException):
            APROTO.observe_allocated_status_tail_once(terminal, calls)
        result = self.attempt.allocated_status_result
        self.assertTrue(result._poisoned)
        self.assertFalse(result._active)
        self.assertEqual(result.state, "UNKNOWN")
        self.assertIsNone(result.eof_evidence)

    def test_allocated_status_poison_avoids_custom_state_comparison(self):
        self.reset_with_shared_descendant()
        terminal = self._complete_allocated_terminal_evidence()
        calls = AllocatedStatusCalls(terminal._watermark_ns + 1)
        observed = []
        class EvilStr(str):
            def __eq__(self, other):
                observed.append("eq")
                return str(self) == other
            def __ne__(self, other):
                observed.append("ne")
                return str(self) != other
        def poison_state():
            self.attempt.allocated_status_result.state = EvilStr("BAD")
            raise OSError("modeled failure")
        calls.read_hook = poison_state
        with self.assertRaises(OSError):
            APROTO.observe_allocated_status_tail_once(terminal, calls)
        self.assertEqual(observed, [])
        self.assertEqual(self.attempt.allocated_status_result.state,
                         "UNKNOWN")

    def test_allocated_status_backend_class_swap_blocks_read(self):
        self.reset_with_shared_descendant()
        terminal = self._complete_allocated_terminal_evidence()
        calls = AllocatedStatusCalls(terminal._watermark_ns + 1)
        calls.clock_hook = lambda count: (
            setattr(calls, "__class__", SwappedAllocatedStatusCalls)
            if count == 1 else None)
        with self.assertRaises(BaseException):
            APROTO.observe_allocated_status_tail_once(terminal, calls)
        self.assertEqual(calls.read_calls, 0)
        self.assertEqual(self.attempt.allocated_status_result.state,
                         "UNKNOWN")

    def test_allocated_status_read_class_swap_retains_returned_eof(self):
        self.reset_with_shared_descendant()
        terminal = self._complete_allocated_terminal_evidence()
        calls = AllocatedStatusCalls(terminal._watermark_ns + 1)
        calls.read_hook = lambda: setattr(
            calls, "__class__", SwappedAllocatedStatusCalls)
        with self.assertRaises(BaseException):
            APROTO.observe_allocated_status_tail_once(terminal, calls)
        result = self.attempt.allocated_status_result
        self.assertTrue(result.read_attempted)
        self.assertTrue(result.read_returned)
        self.assertEqual(result._read_observation, ("EOF", b""))
        self.assertEqual(result.state, "UNKNOWN")
        self.assertIsNone(result.eof_evidence)
        self.assertEqual(calls.read_calls, 1)

    def test_allocated_status_watermark_custom_key_has_no_callback(self):
        self.reset_with_shared_descendant()
        terminal = self._complete_allocated_terminal_evidence()
        calls = AllocatedStatusCalls(terminal._watermark_ns + 1)
        result = APROTO.observe_allocated_status_tail_once(terminal, calls)
        observed = []
        class EvilStr(str):
            def __hash__(self):
                observed.append("hash")
                return hash(str(self))
            def __eq__(self, other):
                observed.append("eq")
                return str(self) == other
        result.watermark.__dict__[EvilStr("other")] = (
            result.watermark.value)
        observed.clear()
        with self.assertRaises(BaseException):
            APROTO.validate_allocated_status_eof(result.eof_evidence)
        self.assertEqual(observed, [])

    def test_allocated_protocol_exact_match_is_opaque_nonqualified(self):
        self.reset_with_shared_descendant()
        status_eof = self._complete_allocated_status_eof()
        attempt = self.attempt
        owned_before = set(attempt.handle.owned)
        watermark_before = status_eof._operation.watermark.value
        probe_before = attempt.settlement_handle.probe_used
        descendant_before = (
            attempt.settlement_handle.descendant_capability.used)
        evaluation = APROTO.match_allocated_abort_protocol_once(status_eof)
        self.assertEqual(
            evaluation.state,
            "MODEL_ALLOCATED_ABORT_PROTOCOL_MATCH_NONQUALIFIED")
        capability = evaluation.capability
        self.assertIsInstance(
            capability, APROTO.AllocatedAbortProtocolCapability)
        self.assertIs(attempt.allocated_protocol_capability, capability)
        self.assertTrue(
            APROTO.validate_allocated_protocol_capability(capability))
        snapshot = capability.snapshot()
        self.assertTrue(snapshot["abort_protocol_verified"])
        self.assertFalse(snapshot["descendants_qualified"])
        self.assertFalse(snapshot["resources_closed"])
        self.assertFalse(snapshot["runtime_authorized"])
        self.assertFalse(snapshot["storage_authorized"])
        self.assertFalse(snapshot["exec_proven"])
        self.assertEqual(attempt.handle.owned, owned_before)
        self.assertEqual(status_eof._operation.watermark.value,
                         watermark_before)
        self.assertIs(attempt.settlement_handle.probe_used, probe_before)
        self.assertIs(
            attempt.settlement_handle.descendant_capability.used,
            descendant_before)
        with self.assertRaises(BaseException):
            copy.copy(capability)
        with self.assertRaises(BaseException):
            APROTO.match_allocated_abort_protocol_once(status_eof)

    def test_allocated_protocol_exit_and_canary_mismatch_mint_no_capability(self):
        cases = (
            (74, LAB.STDOUT_CANARY, LAB.STDERR_CANARY,
             "TERMINAL_NOT_EXIT_73"),
            (LAB.EXIT_ABORT_EOF, b"wrong", LAB.STDERR_CANARY,
             "STDOUT_CANARY_MISMATCH"),
            (LAB.EXIT_ABORT_EOF, LAB.STDERR_CANARY, LAB.STDOUT_CANARY,
             "STDOUT_CANARY_MISMATCH"))
        for status, stdout, stderr, reason in cases:
            with self.subTest(status=status, reason=reason):
                self.reset_with_shared_descendant()
                status_eof = self._complete_allocated_status_eof(
                    status, stdout, stderr)
                evaluation = APROTO.match_allocated_abort_protocol_once(
                    status_eof)
                self.assertEqual(
                    evaluation.state,
                    "MODEL_ALLOCATED_ABORT_PROTOCOL_MISMATCH")
                self.assertIn(reason, evaluation.reasons)
                self.assertIsNone(evaluation.capability)
                self.assertIsNone(
                    self.attempt.allocated_protocol_capability)
                with self.assertRaises(BaseException):
                    APROTO.match_allocated_abort_protocol_once(status_eof)

    def test_allocated_protocol_current_raw_drift_is_terminal(self):
        self.reset_with_shared_descendant()
        status_eof = self._complete_allocated_status_eof()
        self.attempt.capture_handle.streams["stdout"].stored[:] = b"wrong"
        with self.assertRaises(BaseException):
            APROTO.match_allocated_abort_protocol_once(status_eof)
        evaluation = self.attempt.allocated_protocol_evaluation
        self.assertEqual(evaluation.state, "UNKNOWN")
        self.assertIsNone(evaluation.capability)

    def test_allocated_protocol_custom_policy_has_no_callback(self):
        self.reset_with_shared_descendant()
        status_eof = self._complete_allocated_status_eof()
        observed = []
        class EvilDict(dict):
            def items(self):
                observed.append("items")
                return super().items()
        self.attempt._protocol_policy["nested"] = EvilDict()
        with self.assertRaises(BaseException):
            APROTO.match_allocated_abort_protocol_once(status_eof)
        self.assertEqual(observed, [])
        self.assertIsNone(self.attempt.allocated_protocol_capability)

    def test_allocated_protocol_foreign_eof_and_legacy_claim_refuse(self):
        self.reset_with_shared_descendant()
        status_eof = self._complete_allocated_status_eof()
        with self.assertRaises(BaseException):
            self.attempt.claim_abort_terminal_release_once()
        with self.assertRaises(BaseException):
            APROTO.match_allocated_abort_protocol_once(status_eof)

        self.reset_with_shared_descendant()
        status_eof = self._complete_allocated_status_eof()
        evaluation = APROTO.match_allocated_abort_protocol_once(status_eof)
        self.assertIsNotNone(evaluation.capability)
        with self.assertRaises(BaseException):
            self.attempt.claim_abort_terminal_release_once()

    def test_allocated_protocol_fake_monitor_getter_is_not_called(self):
        self.reset_with_shared_descendant()
        status_eof = self._complete_allocated_status_eof()
        original = self.attempt.monitor_capability
        observed = []
        class FakeCapability:
            @property
            def completion(self):
                observed.append("completion")
                return original.completion
        self.attempt._set("monitor_capability", FakeCapability())
        with self.assertRaises(BaseException):
            APROTO.match_allocated_abort_protocol_once(status_eof)
        self.assertEqual(observed, [])
        self.assertIsNone(self.attempt.allocated_protocol_capability)

    def test_allocated_protocol_monitor_backlink_drift_is_refused(self):
        self.reset_with_shared_descendant()
        status_eof = self._complete_allocated_status_eof()
        cap = self.attempt.monitor_capability
        cap.child_adapter = object()
        with self.assertRaises(BaseException):
            APROTO.match_allocated_abort_protocol_once(status_eof)
        self.assertIsNone(self.attempt.allocated_protocol_capability)

        self.reset_with_shared_descendant()
        status_eof = self._complete_allocated_status_eof()
        evaluation = APROTO.match_allocated_abort_protocol_once(status_eof)
        capability = evaluation.capability
        self.attempt.monitor_capability.child_adapter = object()
        with self.assertRaises(BaseException):
            APROTO.validate_allocated_protocol_capability(capability)

    def test_allocated_protocol_validator_never_dispatches_snapshot(self):
        self.reset_with_shared_descendant()
        status_eof = self._complete_allocated_status_eof()
        evaluation = APROTO.match_allocated_abort_protocol_once(status_eof)
        capability = evaluation.capability
        called = []
        def poisoned_snapshot(_self):
            called.append(True)
            self.attempt.handle.owned.remove("stdout_parent")
            return APROTO._protocol_match_receipt()
        with mock.patch.object(
                APROTO.AllocatedAbortProtocolCapability, "snapshot",
                poisoned_snapshot):
            self.assertTrue(
                APROTO.validate_allocated_protocol_capability(capability))
        self.assertEqual(called, [])

    def test_allocated_protocol_capture_limit_drift_is_refused(self):
        self.reset_with_shared_descendant()
        status_eof = self._complete_allocated_status_eof()
        self.attempt.capture_handle.streams["stdout"].limit += 1
        with self.assertRaises(BaseException):
            APROTO.match_allocated_abort_protocol_once(status_eof)
        self.assertIsNone(self.attempt.allocated_protocol_capability)

        self.reset_with_shared_descendant()
        status_eof = self._complete_allocated_status_eof()
        evaluation = APROTO.match_allocated_abort_protocol_once(status_eof)
        capability = evaluation.capability
        self.attempt.capture_handle.streams["stderr"].limit += 1
        with self.assertRaises(BaseException):
            APROTO.validate_allocated_protocol_capability(capability)

    def test_allocated_protocol_fake_settlement_getter_is_not_called(self):
        self.reset_with_shared_descendant()
        status_eof = self._complete_allocated_status_eof()
        observed = []
        class FakeSettlement:
            @property
            def descendant_capability(self):
                observed.append("descendant_capability")
                return None
        self.attempt._set("settlement_handle", FakeSettlement())
        with self.assertRaises(BaseException):
            APROTO.match_allocated_abort_protocol_once(status_eof)
        self.assertEqual(observed, [])
        self.assertIsNone(self.attempt.allocated_protocol_capability)

    def test_allocated_protocol_completion_callback_is_not_called(self):
        self.reset_with_shared_descendant()
        status_eof = self._complete_allocated_status_eof()
        observed = []
        class EvilDict(dict):
            def items(self):
                observed.append("items")
                return super().items()
        cap = self.attempt.monitor_capability
        cap.completion = EvilDict(cap.completion)
        with self.assertRaises(BaseException):
            APROTO.match_allocated_abort_protocol_once(status_eof)
        self.assertEqual(observed, [])
        self.assertIsNone(self.attempt.allocated_protocol_capability)

    def test_allocated_protocol_descendant_completion_callback_is_not_called(self):
        self.reset_with_shared_descendant()
        status_eof = self._complete_allocated_status_eof()
        observed = []
        class EvilDict(dict):
            def items(self):
                observed.append("items")
                return super().items()
        descendant = self.attempt.settlement_handle.descendant_capability
        descendant.primary_completion = EvilDict(
            descendant.primary_completion)
        with self.assertRaises(BaseException):
            APROTO.match_allocated_abort_protocol_once(status_eof)
        self.assertEqual(observed, [])
        self.assertIsNone(self.attempt.allocated_protocol_capability)

    def test_allocated_protocol_completion_drift_after_match_is_inert(self):
        for target in ("monitor", "descendant"):
            with self.subTest(target=target):
                self.reset_with_shared_descendant()
                status_eof = self._complete_allocated_status_eof()
                evaluation = APROTO.match_allocated_abort_protocol_once(
                    status_eof)
                capability = evaluation.capability
                observed = []
                class EvilDict(dict):
                    def items(self):
                        observed.append("items")
                        return super().items()
                if target == "monitor":
                    cap = self.attempt.monitor_capability
                    cap.completion = EvilDict(cap.completion)
                else:
                    descendant = (
                        self.attempt.settlement_handle
                        .descendant_capability)
                    descendant.primary_completion = EvilDict(
                        descendant.primary_completion)
                with self.assertRaises(BaseException):
                    APROTO.validate_allocated_protocol_capability(
                        capability)
                self.assertEqual(observed, [])

    def test_allocated_protocol_adapter_digest_callback_is_not_called(self):
        for after_match in (False, True):
            with self.subTest(after_match=after_match):
                self.reset_with_shared_descendant()
                status_eof = self._complete_allocated_status_eof()
                capability = None
                if after_match:
                    capability = (
                        APROTO.match_allocated_abort_protocol_once(status_eof)
                        .capability)
                observed = []
                class EvilStr(str):
                    def __eq__(self, other):
                        observed.append("eq")
                        return super().__eq__(other)
                adapter = self.attempt.capture_adapter
                adapter.normal_completion_digest = EvilStr(
                    adapter.normal_completion_digest)
                with self.assertRaises(BaseException):
                    if after_match:
                        APROTO.validate_allocated_protocol_capability(
                            capability)
                    else:
                        APROTO.match_allocated_abort_protocol_once(status_eof)
                self.assertEqual(observed, [])

    def test_allocated_protocol_fake_persisted_getter_is_not_called(self):
        for after_match in (False, True):
            with self.subTest(after_match=after_match):
                self.reset_with_shared_descendant()
                status_eof = self._complete_allocated_status_eof()
                original = self.attempt.persisted
                capability = None
                if after_match:
                    capability = (
                        APROTO.match_allocated_abort_protocol_once(status_eof)
                        .capability)
                observed = []
                class FakePersisted:
                    @property
                    def bound(self):
                        observed.append("bound")
                        return original.bound
                self.attempt._set("persisted", FakePersisted())
                with self.assertRaises(BaseException):
                    if after_match:
                        APROTO.validate_allocated_protocol_capability(
                            capability)
                    else:
                        APROTO.match_allocated_abort_protocol_once(status_eof)
                self.assertEqual(observed, [])

    def test_allocated_protocol_identity_callbacks_are_not_called(self):
        for after_match in (False, True):
            for target in ("owner", "owned_child"):
                with self.subTest(after_match=after_match, target=target):
                    self.reset_with_shared_descendant()
                    status_eof = self._complete_allocated_status_eof()
                    capability = None
                    if after_match:
                        capability = (
                            APROTO.match_allocated_abort_protocol_once(
                                status_eof).capability)
                    observed = []
                    if target == "owner":
                        class EvilInt(int):
                            def __eq__(self, other):
                                observed.append("eq")
                                return super().__eq__(other)
                        self.attempt.capture_handle.origin.owner["pid"] = (
                            EvilInt(
                                self.attempt.capture_handle.origin.owner[
                                    "pid"]))
                    else:
                        class EvilStr(str):
                            __hash__ = str.__hash__
                            def __eq__(self, other):
                                observed.append("eq")
                                return super().__eq__(other)
                        lifecycle = self.attempt.owned_pidfd.lifecycle
                        child = lifecycle["owned_child"]
                        lifecycle["owned_child"] = {
                            EvilStr("pid"): child["pid"],
                            "starttime": child["starttime"]}
                    with self.assertRaises(BaseException):
                        if after_match:
                            APROTO.validate_allocated_protocol_capability(
                                capability)
                        else:
                            APROTO.match_allocated_abort_protocol_once(
                                status_eof)
                    self.assertEqual(observed, [])

    def test_allocated_protocol_bound_bytes_callbacks_are_not_called(self):
        for after_match in (False, True):
            for field in ("intent_bytes", "_request_bytes"):
                with self.subTest(after_match=after_match, field=field):
                    self.reset_with_shared_descendant()
                    status_eof = self._complete_allocated_status_eof()
                    capability = None
                    if after_match:
                        capability = (
                            APROTO.match_allocated_abort_protocol_once(
                                status_eof).capability)
                    observed = []
                    class EvilBytes(bytes):
                        def __eq__(self, other):
                            observed.append("eq")
                            return super().__eq__(other)
                    bound = self.attempt.persisted.bound
                    object.__setattr__(
                        bound, field, EvilBytes(getattr(bound, field)))
                    with self.assertRaises(BaseException):
                        if after_match:
                            APROTO.validate_allocated_protocol_capability(
                                capability)
                        else:
                            APROTO.match_allocated_abort_protocol_once(
                                status_eof)
                    self.assertEqual(observed, [])

    def test_allocated_release_inputs_are_one_shot_model_only(self):
        self.reset_with_shared_descendant()
        protocol = self._complete_allocated_protocol_match()
        attempt = self.attempt
        terminal = protocol._terminal_evidence
        status_eof = protocol._status_eof
        terminal_consumer = terminal._consumer
        status_consumer = status_eof._consumer
        owned_before = set(attempt.handle.owned)
        lost_before = set(attempt.handle.lost)
        capability = ARELEASE.qualify_allocated_release_inputs_once(
            protocol)
        self.assertTrue(
            ARELEASE.validate_allocated_release_inputs_capability(
                capability))
        receipt = capability.snapshot()
        self.assertEqual(
            receipt["classification"],
            "MODEL_ALLOCATED_RELEASE_INPUTS_QUALIFIED_ONLY")
        self.assertTrue(receipt["model_evidence_complete"])
        self.assertFalse(receipt["current_deadline_checked"])
        self.assertFalse(receipt["live_release_authorized"])
        self.assertFalse(receipt["descendants_qualified"])
        self.assertFalse(receipt["resources_closed"])
        self.assertFalse(receipt["runtime_authorized"])
        self.assertFalse(receipt["storage_authorized"])
        self.assertTrue(protocol._consumed)
        self.assertIs(protocol._consumer,
                      attempt.allocated_release_inputs_operation)
        self.assertIs(terminal._consumer, terminal_consumer)
        self.assertIs(status_eof._consumer, status_consumer)
        self.assertEqual(attempt.handle.owned, owned_before)
        self.assertEqual(attempt.handle.lost, lost_before)
        with self.assertRaises(BaseException):
            ARELEASE.qualify_allocated_release_inputs_once(protocol)
        with self.assertRaises(BaseException):
            APROTO.validate_allocated_protocol_capability(protocol)
        with self.assertRaises(BaseException):
            copy.copy(capability)

    def test_allocated_release_inputs_refuse_drift_without_successor(self):
        self.reset_with_shared_descendant()
        protocol = self._complete_allocated_protocol_match()
        protocol._terminal_evidence._domain.terminal_echild[
            "observed_ns"] += 1
        with self.assertRaises(BaseException):
            ARELEASE.qualify_allocated_release_inputs_once(protocol)
        self.assertFalse(protocol._consumed)
        self.assertFalse(self.attempt.allocated_release_inputs_started)
        self.assertIsNone(
            self.attempt.allocated_release_inputs_capability)

    def test_allocated_release_inputs_revalidate_current_chain(self):
        self.reset_with_shared_descendant()
        protocol = self._complete_allocated_protocol_match()
        capability = ARELEASE.qualify_allocated_release_inputs_once(
            protocol)
        watermark = protocol._status_eof._operation.watermark
        object.__setattr__(
            watermark, "value", protocol._status_eof._deadline_ns)
        with self.assertRaises(BaseException):
            ARELEASE.validate_allocated_release_inputs_capability(
                capability)

    def test_allocated_release_inputs_do_not_dispatch_snapshot(self):
        self.reset_with_shared_descendant()
        protocol = self._complete_allocated_protocol_match()
        called = []
        def poisoned_snapshot(_self):
            called.append(True)
            return {"classification": "forged"}
        with mock.patch.object(
                APROTO.AllocatedAbortProtocolCapability, "snapshot",
                poisoned_snapshot):
            capability = ARELEASE.qualify_allocated_release_inputs_once(
                protocol)
            self.assertTrue(
                ARELEASE.validate_allocated_release_inputs_capability(
                    capability))
        self.assertEqual(called, [])

    def test_allocated_release_inputs_legacy_claim_stays_refused(self):
        self.reset_with_shared_descendant()
        protocol = self._complete_allocated_protocol_match()
        capability = ARELEASE.qualify_allocated_release_inputs_once(
            protocol)
        with self.assertRaises(BaseException):
            self.attempt.claim_abort_terminal_release_once()
        self.assertTrue(self.attempt.poisoned)
        with self.assertRaises(BaseException):
            ARELEASE.validate_allocated_release_inputs_capability(
                capability)

    def test_allocated_release_inputs_operation_cannot_revive(self):
        self.reset_with_shared_descendant()
        protocol = self._complete_allocated_protocol_match()
        capability = ARELEASE.qualify_allocated_release_inputs_once(
            protocol)
        operation = capability._operation
        operation._poison("injected")
        self.assertFalse(hasattr(operation, "_set"))
        with self.assertRaises(BaseException):
            ARELEASE.validate_allocated_release_inputs_capability(
                capability)

    def test_allocated_release_inputs_fake_operation_getter_is_not_called(self):
        self.reset_with_shared_descendant()
        protocol = self._complete_allocated_protocol_match()
        capability = ARELEASE.qualify_allocated_release_inputs_once(
            protocol)
        observed = []
        class FakeOperation:
            @property
            def protocol(self):
                observed.append("protocol")
                return protocol
        object.__setattr__(capability, "_operation", FakeOperation())
        with self.assertRaises(BaseException):
            ARELEASE.validate_allocated_release_inputs_capability(
                capability)
        self.assertEqual(observed, [])

    def test_allocated_release_inputs_swallowed_reentry_is_terminal(self):
        self.reset_with_shared_descendant()
        protocol = self._complete_allocated_protocol_match()
        original = ARELEASE._RESERVE_PROTOCOL
        nested = []
        def reserve_then_reenter(capability, consumer):
            result = original(capability, consumer)
            try:
                ARELEASE.qualify_allocated_release_inputs_once(capability)
            except BaseException as exc:
                nested.append(type(exc).__name__)
            return result
        def untrusted_poison(_self, _reason):
            raise OSError("untrusted dynamic poison")
        set_calls = []
        def untrusted_set(_self, _name, _value):
            set_calls.append(_name)
            raise OSError("untrusted dynamic attempt set")
        with mock.patch.object(
                ARELEASE, "_RESERVE_PROTOCOL", reserve_then_reenter), \
                mock.patch.object(
                    ARELEASE.AllocatedReleaseInputsOperation, "_poison",
                    untrusted_poison), \
                mock.patch.object(
                    LAB.AbortForkAttempt, "_set", untrusted_set):
            with self.assertRaises(BaseException):
                ARELEASE.qualify_allocated_release_inputs_once(protocol)
        operation = self.attempt.allocated_release_inputs_operation
        self.assertTrue(protocol._consumed)
        self.assertIs(protocol._consumer, operation)
        self.assertEqual(operation.state, "UNKNOWN")
        self.assertTrue(operation.poisoned)
        self.assertTrue(self.attempt.poisoned)
        self.assertEqual(set_calls, [])
        self.assertIsNone(
            self.attempt.allocated_release_inputs_capability)
        self.assertTrue(nested)
        with self.assertRaises(BaseException):
            ARELEASE.qualify_allocated_release_inputs_once(protocol)

    def test_allocated_release_inputs_post_reservation_fault_is_terminal(self):
        self.reset_with_shared_descendant()
        protocol = self._complete_allocated_protocol_match()
        original = ARELEASE._RESERVE_PROTOCOL
        def reserve_then_fail(capability, consumer):
            original(capability, consumer)
            raise OSError("fault after protocol reservation")
        with mock.patch.object(
                ARELEASE, "_RESERVE_PROTOCOL", reserve_then_fail):
            with self.assertRaises(OSError):
                ARELEASE.qualify_allocated_release_inputs_once(protocol)
        operation = self.attempt.allocated_release_inputs_operation
        self.assertTrue(protocol._consumed)
        self.assertIs(protocol._consumer, operation)
        self.assertEqual(operation.state, "UNKNOWN")
        self.assertTrue(self.attempt.poisoned)
        self.assertIsNone(
            self.attempt.allocated_release_inputs_capability)

    def test_allocated_release_inputs_post_backlink_fault_is_terminal(self):
        self.reset_with_shared_descendant()
        protocol = self._complete_allocated_protocol_match()
        original = ARELEASE._ATTEMPT_SET
        captured = []
        def set_then_fail(attempt, name, value):
            original(attempt, name, value)
            if name == "allocated_release_inputs_capability":
                captured.append(value)
                raise OSError("fault after attempt backlink")
        with mock.patch.object(ARELEASE, "_ATTEMPT_SET", set_then_fail):
            with self.assertRaises(OSError):
                ARELEASE.qualify_allocated_release_inputs_once(protocol)
        operation = self.attempt.allocated_release_inputs_operation
        self.assertTrue(protocol._consumed)
        self.assertEqual(operation.state, "UNKNOWN")
        self.assertTrue(self.attempt.poisoned)
        self.assertEqual(len(captured), 1)
        with self.assertRaises(BaseException):
            ARELEASE.validate_allocated_release_inputs_capability(
                captured[0])

    def test_allocated_release_inputs_commit_state_callback_is_not_called(self):
        self.reset_with_shared_descendant()
        protocol = self._complete_allocated_protocol_match()
        original = ARELEASE._ATTEMPT_SET
        observed = []
        class EvilStr(str):
            def __eq__(self, other):
                observed.append("eq")
                object.__setattr__(
                    self.attempt.allocated_release_inputs_operation,
                    "state", "QUALIFYING")
                return super().__eq__(other)
        def set_then_inject(attempt, name, value):
            original(attempt, name, value)
            if name == "allocated_release_inputs_capability":
                object.__setattr__(
                    attempt.allocated_release_inputs_operation,
                    "state", EvilStr("QUALIFYING"))
        with mock.patch.object(ARELEASE, "_ATTEMPT_SET", set_then_inject):
            with self.assertRaises(BaseException):
                ARELEASE.qualify_allocated_release_inputs_once(protocol)
        self.assertEqual(observed, [])
        self.assertEqual(
            self.attempt.allocated_release_inputs_operation.state,
            "UNKNOWN")
        self.assertTrue(self.attempt.poisoned)

    def test_allocated_release_deadline_accepts_exact_bounds_model_only(self):
        for boundary in ("lower", "deadline_minus_one"):
            with self.subTest(boundary=boundary):
                self.reset_with_shared_descendant()
                inputs = self._complete_allocated_release_inputs()
                terminal = inputs._terminal
                status = inputs._status_eof
                lower = max(
                    terminal._watermark_ns, status._before_ns,
                    status._after_ns, status._operation.watermark.value,
                    self.attempt.last_clock_ns)
                now = (lower if boundary == "lower"
                       else terminal._deadline_ns - 1)
                calls = AllocatedDeadlineCalls(now)
                owned_before = set(self.attempt.handle.owned)
                result = ADEADLINE.admit_allocated_release_deadline_once(
                    inputs, calls)
                self.assertEqual(
                    result.state,
                    "MODEL_ALLOCATED_RELEASE_DEADLINE_ADMITTED_ONLY")
                capability = result.capability
                self.assertTrue(
                    ADEADLINE.validate_allocated_release_deadline_capability(
                        capability))
                receipt = capability.snapshot()
                self.assertTrue(
                    receipt["model_deadline_observation_verified"])
                self.assertFalse(receipt["live_clock_verified"])
                self.assertFalse(receipt["live_release_authorized"])
                self.assertFalse(receipt["resources_closed"])
                self.assertEqual(calls.clock_calls, 1)
                self.assertEqual(self.attempt.handle.owned, owned_before)
                with self.assertRaises(BaseException):
                    ADEADLINE.admit_allocated_release_deadline_once(
                        inputs, calls)
                self.assertEqual(calls.clock_calls, 1)

    def test_allocated_release_deadline_refuses_rollback_and_expiry(self):
        for kind in ("rollback", "deadline", "after"):
            with self.subTest(kind=kind):
                self.reset_with_shared_descendant()
                inputs = self._complete_allocated_release_inputs()
                terminal = inputs._terminal
                status = inputs._status_eof
                lower = max(
                    terminal._watermark_ns, status._before_ns,
                    status._after_ns, status._operation.watermark.value,
                    self.attempt.last_clock_ns)
                values = {"rollback": lower - 1,
                          "deadline": terminal._deadline_ns,
                          "after": terminal._deadline_ns + 1}
                calls = AllocatedDeadlineCalls(values[kind])
                result = ADEADLINE.admit_allocated_release_deadline_once(
                    inputs, calls)
                expected = (
                    "MODEL_ALLOCATED_RELEASE_DEADLINE_ROLLBACK_REFUSED"
                    if kind == "rollback" else
                    "MODEL_ALLOCATED_RELEASE_DEADLINE_EXPIRED")
                self.assertEqual(result.state, expected)
                self.assertIsNone(result.capability)
                self.assertTrue(inputs._consumed)
                self.assertEqual(calls.clock_calls, 1)
                with self.assertRaises(BaseException):
                    ADEADLINE.admit_allocated_release_deadline_once(
                        inputs, calls)
                self.assertEqual(calls.clock_calls, 1)

    def test_allocated_release_deadline_custom_time_has_no_callback(self):
        self.reset_with_shared_descendant()
        inputs = self._complete_allocated_release_inputs()
        observed = []
        class EvilInt(int):
            def __eq__(self, other):
                observed.append("eq")
                return super().__eq__(other)
            def __lt__(self, other):
                observed.append("lt")
                return super().__lt__(other)
        calls = AllocatedDeadlineCalls(EvilInt(inputs._terminal._watermark_ns))
        with self.assertRaises(BaseException):
            ADEADLINE.admit_allocated_release_deadline_once(inputs, calls)
        self.assertEqual(observed, [])
        operation = self.attempt.allocated_release_deadline_operation
        self.assertTrue(operation.clock_attempted)
        self.assertTrue(operation.clock_returned)
        self.assertIsNone(operation.observed_ns)
        self.assertEqual(operation.state, "UNKNOWN")
        self.assertEqual(calls.clock_calls, 1)

    def test_allocated_release_deadline_retains_time_on_graph_drift(self):
        self.reset_with_shared_descendant()
        inputs = self._complete_allocated_release_inputs()
        terminal = inputs._terminal
        now = terminal._watermark_ns
        calls = AllocatedDeadlineCalls(now)
        def drift():
            watermark = inputs._status_eof._operation.watermark
            object.__setattr__(watermark, "value", watermark.value + 1)
        calls.clock_hook = drift
        with self.assertRaises(BaseException):
            ADEADLINE.admit_allocated_release_deadline_once(inputs, calls)
        operation = self.attempt.allocated_release_deadline_operation
        self.assertTrue(operation.clock_returned)
        self.assertEqual(operation.observed_ns, now)
        self.assertEqual(operation.state, "UNKNOWN")
        self.assertIsNone(operation.capability)

    def test_allocated_release_deadline_swallowed_reentry_is_terminal(self):
        self.reset_with_shared_descendant()
        inputs = self._complete_allocated_release_inputs()
        terminal = inputs._terminal
        calls = AllocatedDeadlineCalls(terminal._watermark_ns)
        nested = []
        def reenter():
            try:
                ADEADLINE.admit_allocated_release_deadline_once(inputs, calls)
            except BaseException as exc:
                nested.append(type(exc).__name__)
        calls.clock_hook = reenter
        with self.assertRaises(BaseException):
            ADEADLINE.admit_allocated_release_deadline_once(inputs, calls)
        operation = self.attempt.allocated_release_deadline_operation
        self.assertTrue(nested)
        self.assertEqual(operation.state, "UNKNOWN")
        self.assertTrue(operation.poisoned)
        self.assertTrue(self.attempt.poisoned)
        self.assertEqual(calls.clock_calls, 1)

    def test_allocated_release_deadline_backend_swap_is_unknown(self):
        self.reset_with_shared_descendant()
        inputs = self._complete_allocated_release_inputs()
        calls = AllocatedDeadlineCalls(inputs._terminal._watermark_ns)
        calls.clock_hook = lambda: setattr(
            calls, "__class__", SwappedAllocatedDeadlineCalls)
        with self.assertRaises(BaseException):
            ADEADLINE.admit_allocated_release_deadline_once(inputs, calls)
        operation = self.attempt.allocated_release_deadline_operation
        self.assertTrue(operation.clock_returned)
        self.assertEqual(operation.state, "UNKNOWN")
        self.assertEqual(calls.clock_calls, 1)

    def test_allocated_release_deadline_time_drift_has_no_callback(self):
        for value_kind in ("float", "evil"):
            with self.subTest(value_kind=value_kind):
                self.reset_with_shared_descendant()
                inputs = self._complete_allocated_release_inputs()
                calls = AllocatedDeadlineCalls(
                    inputs._terminal._watermark_ns)
                original = self.attempt.last_clock_ns
                observed = []
                class EvilInt(int):
                    def __eq__(self, other):
                        observed.append("eq")
                        return super().__eq__(other)
                def drift():
                    self.attempt._set(
                        "last_clock_ns",
                        (float(original) if value_kind == "float"
                         else EvilInt(original)))
                calls.clock_hook = drift
                with self.assertRaises(BaseException):
                    ADEADLINE.admit_allocated_release_deadline_once(
                        inputs, calls)
                self.assertEqual(observed, [])
                operation = self.attempt.allocated_release_deadline_operation
                self.assertTrue(operation.clock_returned)
                self.assertEqual(operation.observed_ns,
                                 inputs._terminal._watermark_ns)
                self.assertEqual(operation.state, "UNKNOWN")

    def test_allocated_release_deadline_helper_swap_is_not_dispatched(self):
        for module, name in (
                (APROTO, "_STRICT_STREAM_SNAPSHOT"),
                (APROTO, "_protocol_match_receipt"),
                (CAPTURE, "_identity"),
                (CONSUMER.IDENTITY, "_descriptor")):
            with self.subTest(module=module.__name__, name=name):
                self.reset_with_shared_descendant()
                inputs = self._complete_allocated_release_inputs()
                calls = AllocatedDeadlineCalls(
                    inputs._terminal._watermark_ns)
                original = module.__dict__[name]
                observed = []
                def injected(*args, **kwargs):
                    observed.append("called")
                    return original(*args, **kwargs)
                calls.clock_hook = lambda: setattr(
                    module, name, injected)
                try:
                    with self.assertRaises(BaseException):
                        ADEADLINE.admit_allocated_release_deadline_once(
                            inputs, calls)
                finally:
                    setattr(module, name, original)
                self.assertEqual(observed, [])
                operation = (
                    self.attempt.allocated_release_deadline_operation)
                self.assertTrue(operation.clock_returned)
                self.assertEqual(operation.state, "UNKNOWN")

    def test_allocated_release_deadline_surface_equality_is_not_dispatched(self):
        self.reset_with_shared_descendant()
        inputs = self._complete_allocated_release_inputs()
        calls = AllocatedDeadlineCalls(inputs._terminal._watermark_ns)
        original = APROTO._STRICT_STREAM_SNAPSHOT
        observed = []
        class EvilCallable:
            def __eq__(self, other):
                observed.append("eq")
                return True
            def __call__(self, *args, **kwargs):
                observed.append("call")
                return original(*args, **kwargs)
        calls.clock_hook = lambda: setattr(
            APROTO, "_STRICT_STREAM_SNAPSHOT", EvilCallable())
        try:
            with self.assertRaises(BaseException):
                ADEADLINE.admit_allocated_release_deadline_once(
                    inputs, calls)
        finally:
            APROTO._STRICT_STREAM_SNAPSHOT = original
        self.assertEqual(observed, [])
        self.assertTrue(
            self.attempt.allocated_release_deadline_operation.clock_returned)

    def test_allocated_release_deadline_joint_backend_drift_is_unknown(self):
        self.reset_with_shared_descendant()
        inputs = self._complete_allocated_release_inputs()
        calls = AllocatedDeadlineCalls(inputs._terminal._watermark_ns)
        def drift():
            operation = self.attempt.allocated_release_deadline_operation
            object.__setattr__(
                operation.backend, "clock_method", lambda _delegate: 0)
        calls.clock_hook = drift
        with self.assertRaises(BaseException):
            ADEADLINE.admit_allocated_release_deadline_once(inputs, calls)
        operation = self.attempt.allocated_release_deadline_operation
        self.assertTrue(operation.clock_returned)
        self.assertEqual(operation.observed_ns,
                         inputs._terminal._watermark_ns)
        self.assertEqual(operation.state, "UNKNOWN")

    def test_allocated_release_deadline_namespace_key_has_no_callback(self):
        self.reset_with_shared_descendant()
        inputs = self._complete_allocated_release_inputs()
        calls = AllocatedDeadlineCalls(inputs._terminal._watermark_ns)
        observed = []
        class EvilStr(str):
            __hash__ = str.__hash__
            def __lt__(self, other):
                observed.append("lt")
                return super().__lt__(other)
            def __eq__(self, other):
                observed.append("eq")
                return super().__eq__(other)
        key = EvilStr("injected_deadline_surface")
        calls.clock_hook = lambda: APROTO.__dict__.__setitem__(
            key, lambda: None)
        try:
            with self.assertRaises(BaseException):
                ADEADLINE.admit_allocated_release_deadline_once(
                    inputs, calls)
        finally:
            APROTO.__dict__.pop(key, None)
        self.assertEqual(observed, [])
        operation = self.attempt.allocated_release_deadline_operation
        self.assertTrue(operation.clock_returned)
        self.assertEqual(operation.state, "UNKNOWN")

    def test_allocated_release_deadline_class_getter_is_not_dispatched(self):
        self.reset_with_shared_descendant()
        inputs = self._complete_allocated_release_inputs()
        calls = AllocatedDeadlineCalls(inputs._terminal._watermark_ns)
        observed = []
        def owner_getter(instance):
            observed.append("owner")
            return instance.__dict__["owner"]
        calls.clock_hook = lambda: setattr(
            CAPTURE._PipeOrigin, "owner", property(owner_getter))
        try:
            with self.assertRaises(BaseException):
                ADEADLINE.admit_allocated_release_deadline_once(
                    inputs, calls)
        finally:
            delattr(CAPTURE._PipeOrigin, "owner")
        self.assertEqual(observed, [])
        operation = self.attempt.allocated_release_deadline_operation
        self.assertTrue(operation.clock_returned)
        self.assertEqual(operation.state, "UNKNOWN")

    def test_allocated_release_deadline_module_name_has_no_callback(self):
        self.reset_with_shared_descendant()
        inputs = self._complete_allocated_release_inputs()
        calls = AllocatedDeadlineCalls(inputs._terminal._watermark_ns)
        observed = []
        original = CAPTURE.__name__
        class EvilStr(str):
            def __eq__(self, other):
                observed.append("eq")
                return super().__eq__(other)
        calls.clock_hook = lambda: setattr(
            CAPTURE, "__name__", EvilStr(original))
        try:
            with self.assertRaises(BaseException):
                ADEADLINE.admit_allocated_release_deadline_once(
                    inputs, calls)
        finally:
            CAPTURE.__name__ = original
        self.assertEqual(observed, [])
        operation = self.attempt.allocated_release_deadline_operation
        self.assertTrue(operation.clock_returned)
        self.assertEqual(operation.state, "UNKNOWN")

    def test_allocated_release_deadline_module_alias_getter_not_called(self):
        self.reset_with_shared_descendant()
        inputs = self._complete_allocated_release_inputs()
        calls = AllocatedDeadlineCalls(inputs._terminal._watermark_ns)
        observed = []
        original = CONSUMER.IDENTITY
        class FakeIdentity:
            @property
            def _descriptor(self):
                observed.append("descriptor")
                return original._descriptor
        calls.clock_hook = lambda: setattr(
            CONSUMER, "IDENTITY", FakeIdentity())
        try:
            with self.assertRaises(BaseException):
                ADEADLINE.admit_allocated_release_deadline_once(
                    inputs, calls)
        finally:
            CONSUMER.IDENTITY = original
        self.assertEqual(observed, [])
        operation = self.attempt.allocated_release_deadline_operation
        self.assertTrue(operation.clock_returned)
        self.assertEqual(operation.state, "UNKNOWN")

    def test_allocated_release_deadline_alias_name_key_has_no_callback(self):
        self.reset_with_shared_descendant()
        inputs = self._complete_allocated_release_inputs()
        calls = AllocatedDeadlineCalls(inputs._terminal._watermark_ns)
        observed = []
        namespace = CONSUMER.IDENTITY.__dict__
        original_name = namespace["__name__"]
        class EvilStr(str):
            __hash__ = str.__hash__
            def __eq__(self, other):
                observed.append("eq")
                return super().__eq__(other)
        evil_key = EvilStr("__name__")
        def drift():
            namespace.pop("__name__")
            namespace[evil_key] = original_name
        calls.clock_hook = drift
        try:
            with self.assertRaises(BaseException):
                ADEADLINE.admit_allocated_release_deadline_once(
                    inputs, calls)
        finally:
            namespace.pop(evil_key, None)
            namespace["__name__"] = original_name
        self.assertEqual(observed, [])
        operation = self.attempt.allocated_release_deadline_operation
        self.assertTrue(operation.clock_returned)
        self.assertEqual(operation.state, "UNKNOWN")

    def test_allocated_resource_plan_freezes_exact_original_owners_only(self):
        self.reset_with_shared_descendant()
        deadline = self._complete_allocated_release_deadline()
        self.assertIs(ADEADLINE, RPLAN.DEADLINE)
        self.assertIs(type(deadline),
                      ADEADLINE.AllocatedReleaseDeadlineCapability)
        plan = RPLAN.bind_allocated_resource_plan_once(deadline)
        self.assertTrue(RPLAN.validate_allocated_resource_plan(plan))
        receipt = plan.snapshot()
        self.assertEqual(receipt["release_order"], [
            "epoll", "status_parent", "stdout_parent", "stderr_parent",
            "pidfd"])
        self.assertTrue(receipt["acquisition_provenance_frozen"])
        self.assertTrue(receipt["ownership_binding_frozen"])
        self.assertFalse(receipt["ownership_transfer_modeled"])
        self.assertFalse(receipt["acquisition_time_ofd_proven"])
        self.assertFalse(receipt["live_identity_verified"])
        self.assertFalse(receipt["live_close_authorized"])
        self.assertFalse(receipt["anchors_acquired"])
        self.assertFalse(receipt["resources_closed"])
        self.assertFalse(receipt["runtime_authorized"])
        self.assertFalse(receipt["storage_authorized"])
        self.assertIs(plan._poller, self.attempt.capture_handle.poller)
        self.assertIs(plan._owned_pidfd, self.attempt.owned_pidfd)
        self.assertTrue(deadline._consumed)
        self.assertIs(deadline._consumer, plan)

    def test_allocated_resource_plan_is_one_shot_and_noncopyable(self):
        self.reset_with_shared_descendant()
        deadline = self._complete_allocated_release_deadline()
        plan = RPLAN.bind_allocated_resource_plan_once(deadline)
        with self.assertRaises(BaseException):
            RPLAN.bind_allocated_resource_plan_once(deadline)
        with self.assertRaises(BaseException):
            copy.copy(plan)
        with self.assertRaises(BaseException):
            copy.deepcopy(plan)

    def test_allocated_resource_plan_detects_wrapper_replacement(self):
        self.reset_with_shared_descendant()
        deadline = self._complete_allocated_release_deadline()
        plan = RPLAN.bind_allocated_resource_plan_once(deadline)
        original = self.attempt.capture_handle.poller
        replacement = select.epoll()
        try:
            object.__setattr__(
                self.attempt.capture_handle, "poller", replacement)
            with self.assertRaises(BaseException):
                RPLAN.validate_allocated_resource_plan(plan)
        finally:
            object.__setattr__(self.attempt.capture_handle, "poller", original)
            replacement.close()

    def test_allocated_resource_plan_detects_fd_authority_drift(self):
        self.reset_with_shared_descendant()
        deadline = self._complete_allocated_release_deadline()
        plan = RPLAN.bind_allocated_resource_plan_once(deadline)
        original = self.attempt.handle._allocation_authority
        altered = list(original)
        role, values = altered[0]
        altered[0] = (role, (values[0] + 1000, *values[1:]))
        try:
            self.attempt.handle._allocation_authority = tuple(altered)
            with self.assertRaises(BaseException):
                RPLAN.validate_allocated_resource_plan(plan)
        finally:
            self.attempt.handle._allocation_authority = original

    def test_allocated_resource_plan_foreign_thread_is_refused(self):
        self.reset_with_shared_descendant()
        deadline = self._complete_allocated_release_deadline()
        plan = RPLAN.bind_allocated_resource_plan_once(deadline)
        errors = []
        def validate():
            try:
                RPLAN.validate_allocated_resource_plan(plan)
            except BaseException as exc:
                errors.append(type(exc).__name__)
        worker = threading.Thread(target=validate)
        worker.start()
        worker.join()
        self.assertTrue(errors)

    def test_allocated_resource_plan_post_reservation_fault_is_poisoned(self):
        self.reset_with_shared_descendant()
        deadline = self._complete_allocated_release_deadline()
        original = RPLAN._RESERVE_DEADLINE
        def reserve_then_fail(capability, consumer):
            original(capability, consumer)
            raise OSError("fault after reservation")
        with mock.patch.object(
                RPLAN, "_RESERVE_DEADLINE", reserve_then_fail):
            with self.assertRaises(OSError):
                RPLAN.bind_allocated_resource_plan_once(deadline)
        self.assertTrue(deadline._consumed)
        retained = deadline._consumer
        self.assertEqual(retained._state, "UNKNOWN")
        self.assertTrue(retained._poisoned)
        with self.assertRaises(BaseException):
            RPLAN.validate_allocated_resource_plan(retained)
        with self.assertRaises(BaseException):
            RPLAN.bind_allocated_resource_plan_once(deadline)

    def test_allocated_resource_plan_swallowed_reentry_is_poisoned(self):
        self.reset_with_shared_descendant()
        deadline = self._complete_allocated_release_deadline()
        original = RPLAN._RESERVE_DEADLINE
        nested = []
        def reserve_then_reenter(capability, consumer):
            original(capability, consumer)
            try:
                RPLAN.bind_allocated_resource_plan_once(capability)
            except BaseException as exc:
                nested.append(type(exc).__name__)
        with mock.patch.object(
                RPLAN, "_RESERVE_DEADLINE", reserve_then_reenter):
            with self.assertRaises(BaseException):
                RPLAN.bind_allocated_resource_plan_once(deadline)
        self.assertTrue(nested)
        self.assertTrue(deadline._consumed)
        retained = deadline._consumer
        self.assertEqual(retained._state, "UNKNOWN")
        self.assertTrue(retained._poisoned)
        with self.assertRaises(BaseException):
            RPLAN.validate_allocated_resource_plan(retained)

    def test_allocated_resource_plan_custom_scalars_dispatch_no_equality(self):
        fields = ("_epoll_fd", "_pidfd", "_deadline_ns")
        for field in fields:
            with self.subTest(field=field):
                self.reset_with_shared_descendant()
                deadline = self._complete_allocated_release_deadline()
                plan = RPLAN.bind_allocated_resource_plan_once(deadline)
                observed = []
                class EvilInt(int):
                    def __eq__(self, other):
                        observed.append("eq")
                        return super().__eq__(other)
                object.__setattr__(plan, field, EvilInt(getattr(plan, field)))
                with self.assertRaises(BaseException):
                    RPLAN.validate_allocated_resource_plan(plan)
                self.assertEqual(observed, [])

    def test_allocated_resource_plan_stream_objects_use_identity_only(self):
        self.reset_with_shared_descendant()
        deadline = self._complete_allocated_release_deadline()
        plan = RPLAN.bind_allocated_resource_plan_once(deadline)
        observed = []
        class EvilStream:
            def __eq__(self, other):
                observed.append("eq")
                return True
        object.__setattr__(plan, "_stream_refs", (
            ("stdout", EvilStream()), plan._stream_refs[1]))
        with self.assertRaises(BaseException):
            RPLAN.validate_allocated_resource_plan(plan)
        self.assertEqual(observed, [])

    def test_allocated_resource_plan_disposable_wrapper_close_only(self):
        self.reset_with_shared_descendant()
        deadline = self._complete_allocated_release_deadline()
        plan = RPLAN.bind_allocated_resource_plan_once(deadline)
        capture = self.attempt.capture_handle
        poller = plan._poller
        pipe_fds = [fd for _, fd in plan._pipe_fds]
        before = [os.fstat(fd) for fd in pipe_fds]
        self.assertEqual(capture.state, "SETTLEMENT_FINISHED")
        CAPTURE.close_capture_epoll(capture, CAPTURE.LinuxCaptureSyscalls)
        self.assertTrue(poller.closed)
        self.assertIsNone(capture.poller)
        self.assertEqual(capture.state, "CLOSED")
        after = [os.fstat(fd) for fd in pipe_fds]
        self.assertEqual(
            [(item.st_dev, item.st_ino) for item in after],
            [(item.st_dev, item.st_ino) for item in before])
        with self.assertRaises(BaseException):
            RPLAN.validate_allocated_resource_plan(plan)

    def test_allocated_step_adopted_reap_retains_exact_effects(self):
        self.reset_with_shared_descendant()
        bridge, watermark = self._complete_allocated_drain_ready()
        calls = AllocatedStepCalls(self.owner, watermark + 1)
        child = self.owner["pid"] + 200
        terminal = types.SimpleNamespace(
            si_pid=child, si_code=os.CLD_EXITED, si_status=0)
        calls.discovery = [terminal]
        calls.identities[child] = {
            "pid": child, "starttime": 888,
            "boot_id": self.owner["boot_id"], "ppid": self.owner["pid"]}
        calls.pidfd_results = [terminal, terminal]
        result = DBRIDGE.advance_allocated_descendant_once(bridge, calls)
        self.assertEqual(result.state,
                         "ALLOCATED_STEP_ADOPTED_NONQUALIFIED")
        self.assertEqual(result.receipt["outcome"],
                         "ADOPTED_CHILD_REAPED_CONTINUE")
        self.assertTrue(result.pidfd_reap_attempted)
        self.assertTrue(result.pidfd_close_confirmed)
        self.assertEqual(calls.closed, [child + 1000])
        self.assertEqual(len(bridge.domain.records), 1)

    def test_allocated_step_callback_poison_blocks_later_effects(self):
        self.reset_with_shared_descendant()
        bridge, watermark = self._complete_allocated_drain_ready()
        calls = AllocatedStepCalls(self.owner, watermark + 1)
        calls.discovery = [None]
        def poison_owner():
            bridge._poison("injected")
        calls.owner_hook = poison_owner
        with self.assertRaises(BaseException):
            DBRIDGE.advance_allocated_descendant_once(bridge, calls)
        result = self.attempt.descendant_step_result
        self.assertEqual(result.state, "UNKNOWN")
        self.assertEqual(result.receipt["outcome"], "UNKNOWN")
        self.assertEqual(calls.wait_all_calls, 0)
        self.assertFalse(result.pidfd_reap_attempted)

    def test_allocated_step_nested_domain_override_has_zero_callbacks(self):
        self.reset_with_shared_descendant()
        bridge, watermark = self._complete_allocated_drain_ready()
        calls = AllocatedStepCalls(self.owner, watermark + 1)
        calls.discovery = [None]
        injected = []
        bridge.domain._receipt = lambda *args: injected.append(True)
        with self.assertRaises(BaseException):
            DBRIDGE.advance_allocated_descendant_once(bridge, calls)
        self.assertEqual(injected, [])
        self.assertEqual(calls.wait_all_calls, 0)

    def test_allocated_step_rejects_adopted_pidfd_alias(self):
        self.reset_with_shared_descendant()
        bridge, watermark = self._complete_allocated_drain_ready()
        calls = AllocatedStepCalls(self.owner, watermark + 1)
        child = self.owner["pid"] + 200
        terminal = types.SimpleNamespace(
            si_pid=child, si_code=os.CLD_EXITED, si_status=0)
        calls.discovery = [terminal]
        calls.identities[child] = {
            "pid": child, "starttime": 888,
            "boot_id": self.owner["boot_id"], "ppid": self.owner["pid"]}
        calls.pidfd_value = bridge.attachment.owned_handle.pidfd
        result = DBRIDGE.advance_allocated_descendant_once(bridge, calls)
        self.assertEqual(result.state, "UNKNOWN")
        self.assertTrue(result.pidfd_open_attempted)
        self.assertFalse(result.pidfd_open_returned)
        self.assertEqual(calls.wait_pidfd_calls, 0)
        self.assertEqual(calls.closed, [])

    def test_allocated_step_outer_drift_retains_returned_unknown_receipt(self):
        self.reset_with_shared_descendant()
        bridge, watermark = self._complete_allocated_drain_ready()
        calls = AllocatedStepCalls(self.owner, watermark + 1)
        calls.discovery = [None]
        def drift():
            bridge.attachment.owned_handle.pidfd += 1
        calls.owner_hook = drift
        with self.assertRaises(BaseException):
            DBRIDGE.advance_allocated_descendant_once(bridge, calls)
        result = self.attempt.descendant_step_result
        self.assertEqual(result.state, "UNKNOWN")
        self.assertIsNotNone(result.receipt)
        self.assertEqual(result.receipt["outcome"], "UNKNOWN")
        self.assertEqual(calls.wait_all_calls, 0)

    def test_allocated_step_reap_after_effect_retains_evidence(self):
        self.reset_with_shared_descendant()
        bridge, watermark = self._complete_allocated_drain_ready()
        calls = AllocatedStepCalls(self.owner, watermark + 1)
        child = self.owner["pid"] + 200
        terminal = types.SimpleNamespace(
            si_pid=child, si_code=os.CLD_EXITED, si_status=0)
        calls.discovery = [terminal]
        calls.identities[child] = {
            "pid": child, "starttime": 888,
            "boot_id": self.owner["boot_id"], "ppid": self.owner["pid"]}
        calls.pidfd_results = [terminal, terminal]
        calls.wait_pidfd_hook = (
            lambda count: bridge._poison("after-reap")
            if count == 2 else None)
        with self.assertRaises(BaseException):
            DBRIDGE.advance_allocated_descendant_once(bridge, calls)
        result = self.attempt.descendant_step_result
        self.assertTrue(result.pidfd_reap_attempted)
        self.assertIsNotNone(result.pidfd_reap_result)
        self.assertFalse(result.pidfd_close_confirmed)
        self.assertEqual(result.state, "UNKNOWN")

    def test_allocated_step_close_after_effect_is_unknown_no_retry(self):
        self.reset_with_shared_descendant()
        bridge, watermark = self._complete_allocated_drain_ready()
        calls = AllocatedStepCalls(self.owner, watermark + 1)
        child = self.owner["pid"] + 200
        terminal = types.SimpleNamespace(
            si_pid=child, si_code=os.CLD_EXITED, si_status=0)
        calls.discovery = [terminal]
        calls.identities[child] = {
            "pid": child, "starttime": 888,
            "boot_id": self.owner["boot_id"], "ppid": self.owner["pid"]}
        calls.pidfd_results = [terminal, terminal]
        calls.close_hook = lambda: bridge._poison("after-close")
        with self.assertRaises(BaseException):
            DBRIDGE.advance_allocated_descendant_once(bridge, calls)
        result = self.attempt.descendant_step_result
        self.assertTrue(result.pidfd_close_attempted)
        self.assertTrue(result.pidfd_close_returned)
        self.assertTrue(result.pidfd_close_unknown)
        self.assertFalse(result.pidfd_close_confirmed)
        self.assertEqual(calls.closed, [child + 1000])

    def test_allocated_step_joint_snapshot_drift_is_terminal(self):
        self.reset_with_shared_descendant()
        bridge, watermark = self._complete_allocated_drain_ready()
        calls = AllocatedStepCalls(self.owner, watermark + 1)
        calls.discovery = [None]
        def drift():
            result = self.attempt.descendant_step_result
            bridge.attachment.owned_handle.pidfd += 1
            result.authority.owned_pidfd = (
                bridge.attachment.owned_handle.pidfd)
        calls.owner_hook = drift
        with self.assertRaises(BaseException):
            DBRIDGE.advance_allocated_descendant_once(bridge, calls)
        self.assertEqual(calls.wait_all_calls, 0)
        self.assertEqual(self.attempt.descendant_step_result.state, "UNKNOWN")

    def test_allocated_step_fd_custom_scalar_runs_no_comparator(self):
        self.reset_with_shared_descendant()
        bridge, watermark = self._complete_allocated_drain_ready()
        calls = AllocatedStepCalls(self.owner, watermark + 1)
        calls.discovery = [None]
        observed = []
        class EvilInt(int):
            def __eq__(self, other):
                observed.append("eq")
                return int(self) == other
        def drift():
            role = "status_parent"
            bridge.attempt.handle.bundle[role]["fd"] = EvilInt(
                bridge.attempt.handle.bundle[role]["fd"])
        calls.owner_hook = drift
        with self.assertRaises(BaseException):
            DBRIDGE.advance_allocated_descendant_once(bridge, calls)
        self.assertEqual(observed, [])
        self.assertEqual(calls.wait_all_calls, 0)

    def test_allocated_step_rejects_epoll_fd_alias(self):
        self.reset_with_shared_descendant()
        bridge, watermark = self._complete_allocated_drain_ready()
        calls = AllocatedStepCalls(self.owner, watermark + 1)
        child = self.owner["pid"] + 200
        terminal = types.SimpleNamespace(
            si_pid=child, si_code=os.CLD_EXITED, si_status=0)
        calls.discovery = [terminal]
        calls.identities[child] = {
            "pid": child, "starttime": 888,
            "boot_id": self.owner["boot_id"], "ppid": self.owner["pid"]}
        calls.pidfd_value = bridge.attempt._capture_poller.fileno()
        result = DBRIDGE.advance_allocated_descendant_once(bridge, calls)
        self.assertEqual(result.state, "UNKNOWN")
        self.assertFalse(result.pidfd_open_returned)
        self.assertEqual(calls.wait_pidfd_calls, 0)
        self.assertEqual(calls.closed, [])

    def test_allocated_step_malformed_close_result_is_unknown(self):
        self.reset_with_shared_descendant()
        bridge, watermark = self._complete_allocated_drain_ready()
        calls = AllocatedStepCalls(self.owner, watermark + 1)
        child = self.owner["pid"] + 200
        terminal = types.SimpleNamespace(
            si_pid=child, si_code=os.CLD_EXITED, si_status=0)
        calls.discovery = [terminal]
        calls.identities[child] = {
            "pid": child, "starttime": 888,
            "boot_id": self.owner["boot_id"], "ppid": self.owner["pid"]}
        calls.pidfd_results = [terminal, terminal]
        calls.close_result = False
        result = DBRIDGE.advance_allocated_descendant_once(bridge, calls)
        self.assertEqual(result.state, "UNKNOWN")
        self.assertTrue(result.pidfd_close_attempted)
        self.assertTrue(result.pidfd_close_unknown)
        self.assertFalse(result.pidfd_close_confirmed)
        self.assertEqual(calls.closed, [child + 1000])

    def test_allocated_step_result_seal_cannot_be_disabled(self):
        self.reset_with_shared_descendant()
        bridge, watermark = self._complete_allocated_drain_ready()
        calls = AllocatedStepCalls(self.owner, watermark + 1)
        calls.discovery = [None]
        def bypass():
            result = self.attempt.descendant_step_result
            result._fixed = False
            result.authority = object()
        calls.owner_hook = bypass
        result = DBRIDGE.advance_allocated_descendant_once(bridge, calls)
        self.assertEqual(result.state, "UNKNOWN")
        self.assertEqual(calls.wait_all_calls, 0)
        self.assertIsInstance(result.authority,
                              DBRIDGE._AllocatedStepAuthority)

    def test_allocated_step_watermark_seal_cannot_be_deleted(self):
        self.reset_with_shared_descendant()
        bridge, watermark = self._complete_allocated_drain_ready()
        calls = AllocatedStepCalls(self.owner, watermark + 1)
        calls.discovery = [None]
        def bypass():
            result = self.attempt.descendant_step_result
            del result.watermark._sealed
            result.watermark.value = 0
        calls.owner_hook = bypass
        result = DBRIDGE.advance_allocated_descendant_once(bridge, calls)
        self.assertEqual(result.state, "UNKNOWN")
        self.assertEqual(result.watermark.value, watermark)
        self.assertEqual(calls.wait_all_calls, 0)

    def test_allocated_step_joint_stream_replacement_is_detected(self):
        self.reset_with_shared_descendant()
        bridge, watermark = self._complete_allocated_drain_ready()
        calls = AllocatedStepCalls(self.owner, watermark + 1)
        calls.discovery = [None]
        def drift():
            replacement = object()
            bridge.attempt.capture_handle.streams["stdout"] = replacement
            bridge.attempt._capture_stream_refs["stdout"] = replacement
        calls.owner_hook = drift
        with self.assertRaises(BaseException):
            DBRIDGE.advance_allocated_descendant_once(bridge, calls)
        self.assertEqual(calls.wait_all_calls, 0)
        self.assertEqual(self.attempt.descendant_step_result.state, "UNKNOWN")

    def test_allocated_step_domain_identity_drift_blocks_discovery(self):
        self.reset_with_shared_descendant()
        bridge, watermark = self._complete_allocated_drain_ready()
        calls = AllocatedStepCalls(self.owner, watermark + 1)
        calls.discovery = [None]
        def drift():
            bridge.domain.request_id = "foreign-request"
            bridge.domain.domain_id = "f" * 32
        calls.owner_hook = drift
        with self.assertRaises(BaseException):
            DBRIDGE.advance_allocated_descendant_once(bridge, calls)
        self.assertEqual(calls.wait_all_calls, 0)

    def test_allocated_step_custom_capability_leader_has_no_callback(self):
        self.reset_with_shared_descendant()
        bridge, watermark = self._complete_allocated_drain_ready()
        calls = AllocatedStepCalls(self.owner, watermark + 1)
        calls.discovery = [None]
        observed = []
        class EvilDict(dict):
            def __eq__(self, other):
                observed.append("eq")
                return dict(self) == other
        def drift():
            bridge.leader_capability.leader = EvilDict(
                bridge.leader_capability.leader)
        calls.owner_hook = drift
        with self.assertRaises(BaseException):
            DBRIDGE.advance_allocated_descendant_once(bridge, calls)
        self.assertEqual(observed, [])
        self.assertEqual(calls.wait_all_calls, 0)

    def test_allocated_step_fake_capability_runs_no_getter(self):
        self.reset_with_shared_descendant()
        bridge, watermark = self._complete_allocated_drain_ready()
        calls = AllocatedStepCalls(self.owner, watermark + 1)
        calls.discovery = [None]
        observed = []
        class FakeCapability:
            @property
            def used(self):
                observed.append("used")
                return True
        def drift():
            fake = FakeCapability()
            bridge.leader_capability = fake
            bridge.attachment.adapter.reap_capability = fake
        calls.owner_hook = drift
        with self.assertRaises(BaseException):
            DBRIDGE.advance_allocated_descendant_once(bridge, calls)
        self.assertEqual(observed, [])
        self.assertEqual(calls.wait_all_calls, 0)

    def test_allocated_deadline_binding_expiry_is_terminal_no_retry(self):
        self.reset_with_shared_descendant()
        attachment = self._complete_attached_linux_reap()
        settlement = self.attempt.settlement_handle
        hard_limit = self.shared_descendant.cleanup_hard_limit_ns
        with self.assertRaises(BaseException):
            DBRIDGE.bind_allocated_settlement_deadline(
                attachment, settlement,
                types.SimpleNamespace(
                    monotonic_ns=lambda: self.attempt.cleanup_deadline_ns))
        self.assertIsNone(self.attempt.descendant_deadline_binding)
        self.assertTrue(self.attempt.descendant_deadline_binding_started)
        self.assertEqual(attachment.domain.deadline_ns, hard_limit)
        self.assertEqual(self.shared_descendant.state, "UNKNOWN")
        with self.assertRaises(BaseException):
            DBRIDGE.bind_allocated_settlement_deadline(
                attachment, settlement,
                types.SimpleNamespace(monotonic_ns=lambda: 0))

    def test_allocated_deadline_binding_rejects_settlement_drift(self):
        self.reset_with_shared_descendant()
        attachment = self._complete_attached_linux_reap()
        settlement = self.attempt.settlement_handle
        settlement.deadline_ns += 1
        calls = []
        with self.assertRaises(BaseException):
            DBRIDGE.bind_allocated_settlement_deadline(
                attachment, settlement,
                types.SimpleNamespace(
                    monotonic_ns=lambda: calls.append(True)))
        self.assertEqual(calls, [])
        self.assertIsNone(self.attempt.descendant_deadline_binding)
        self.assertEqual(self.shared_descendant.state, "UNKNOWN")

    def test_allocated_deadline_binding_rejects_clock_capability_drift(self):
        mutations = (
            lambda cap: setattr(cap, "deadline_ns", cap.deadline_ns + 1),
            lambda cap: setattr(cap, "finished_ns", cap.finished_ns + 1),
            lambda cap: setattr(cap, "capture_handle", object()),
            lambda cap: setattr(cap, "child_adapter", object()),
            lambda cap: setattr(cap, "owner_thread", object()),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.reset_with_shared_descendant()
                attachment = self._complete_attached_linux_reap()
                settlement = self.attempt.settlement_handle
                hard_limit = self.shared_descendant.cleanup_hard_limit_ns
                def clock():
                    mutate(settlement.descendant_capability)
                    return settlement.receipt["finished_ns"] + 1
                with self.assertRaises(BaseException):
                    DBRIDGE.bind_allocated_settlement_deadline(
                        attachment, settlement,
                        types.SimpleNamespace(monotonic_ns=clock))
                self.assertIsNone(self.attempt.descendant_deadline_binding)
                self.assertEqual(attachment.domain.deadline_ns, hard_limit)
                self.assertEqual(self.shared_descendant.state, "UNKNOWN")

    def test_allocated_deadline_binding_rejects_clock_watermark_drift(self):
        self.reset_with_shared_descendant()
        attachment = self._complete_attached_linux_reap()
        settlement = self.attempt.settlement_handle
        hard_limit = self.shared_descendant.cleanup_hard_limit_ns
        def clock():
            self.attempt._set("last_clock_ns",
                              self.attempt.last_clock_ns - 1)
            return settlement.receipt["finished_ns"]
        with self.assertRaises(BaseException):
            DBRIDGE.bind_allocated_settlement_deadline(
                attachment, settlement,
                types.SimpleNamespace(monotonic_ns=clock))
        self.assertIsNone(self.attempt.descendant_deadline_binding)
        self.assertEqual(attachment.domain.deadline_ns, hard_limit)

    def test_allocated_deadline_binding_custom_cleanup_start_no_callback(self):
        self.reset_with_shared_descendant()
        attachment = self._complete_attached_linux_reap()
        settlement = self.attempt.settlement_handle
        calls = []
        class EvilInt(int):
            def __add__(self, other):
                calls.append("add")
                return int(self) + other
        self.attempt._set(
            "cleanup_started_ns", EvilInt(self.attempt.cleanup_started_ns))
        with self.assertRaises(BaseException):
            DBRIDGE.bind_allocated_settlement_deadline(
                attachment, settlement,
                types.SimpleNamespace(
                    monotonic_ns=lambda: calls.append("clock")))
        self.assertEqual(calls, [])
        self.assertIsNone(self.attempt.descendant_deadline_binding)

    def test_allocated_deadline_binding_custom_cap_scalars_no_callback(self):
        targets = ("settlement_deadline", "descendant_deadline",
                   "descendant_finished")
        for target in targets:
            with self.subTest(target=target):
                self.reset_with_shared_descendant()
                attachment = self._complete_attached_linux_reap()
                settlement = self.attempt.settlement_handle
                descendant = settlement.descendant_capability
                calls = []
                class EvilInt(int):
                    def __eq__(self, other):
                        calls.append("eq")
                        return int(self) == other
                if target == "settlement_deadline":
                    settlement.deadline_ns = EvilInt(settlement.deadline_ns)
                elif target == "descendant_deadline":
                    descendant.deadline_ns = EvilInt(descendant.deadline_ns)
                else:
                    descendant.finished_ns = EvilInt(descendant.finished_ns)
                with self.assertRaises(BaseException):
                    DBRIDGE.bind_allocated_settlement_deadline(
                        attachment, settlement,
                        types.SimpleNamespace(
                            monotonic_ns=lambda: calls.append("clock")))
                self.assertEqual(calls, [])
                self.assertIsNone(self.attempt.descendant_deadline_binding)

    def test_allocated_deadline_binding_rejects_monitor_time_drift(self):
        self.reset_with_shared_descendant()
        attachment = self._complete_attached_linux_reap()
        settlement = self.attempt.settlement_handle
        hard_limit = self.shared_descendant.cleanup_hard_limit_ns
        def clock():
            self.attempt.monitor_capability.completed_at_ns = (
                self.attempt.cleanup_started_ns + 1)
            return settlement.receipt["finished_ns"] + 1
        with self.assertRaises(BaseException):
            DBRIDGE.bind_allocated_settlement_deadline(
                attachment, settlement,
                types.SimpleNamespace(monotonic_ns=clock))
        self.assertIsNone(self.attempt.descendant_deadline_binding)
        self.assertEqual(attachment.domain.deadline_ns, hard_limit)
        self.assertEqual(self.shared_descendant.state, "UNKNOWN")

    def test_allocated_deadline_binding_rejects_settlement_owner_drift(self):
        self.reset_with_shared_descendant()
        attachment = self._complete_attached_linux_reap()
        settlement = self.attempt.settlement_handle
        hard_limit = self.shared_descendant.cleanup_hard_limit_ns
        def clock():
            settlement.owner["pid"] += 1
            return settlement.receipt["finished_ns"] + 1
        with self.assertRaises(BaseException):
            DBRIDGE.bind_allocated_settlement_deadline(
                attachment, settlement,
                types.SimpleNamespace(monotonic_ns=clock))
        self.assertIsNone(self.attempt.descendant_deadline_binding)
        self.assertEqual(attachment.domain.deadline_ns, hard_limit)
        self.assertEqual(self.shared_descendant.state, "UNKNOWN")

    def test_allocated_attachment_drift_blocks_close_before_effect(self):
        self.reset_with_shared_descendant()
        self._prepare_linux_capture_for_grant_close()
        attachment = DBRIDGE.attach_allocated_owned_domain(
            self.shared_descendant, self.attempt)
        object.__setattr__(attachment, "binding", object())
        with mock.patch.object(self.handle, "close_role_once",
                               wraps=self.handle.close_role_once) as close:
            with self.assertRaises(BaseException):
                self.attempt.close_linux_grant_zero_write_once()
        self.assertEqual(close.call_count, 0)
        self.assertEqual(self.shared_descendant.state, "UNKNOWN")
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")

    def test_allocated_attachment_custom_shared_state_has_no_callback(self):
        self.reset_with_shared_descendant()
        self._prepare_linux_capture_for_grant_close()
        DBRIDGE.attach_allocated_owned_domain(
            self.shared_descendant, self.attempt)
        calls = []
        class EvilStr(str):
            def __eq__(self, other):
                calls.append("eq")
                return super().__eq__(other)
        object.__setattr__(self.shared_descendant, "state",
                           EvilStr("ATTACHED_HARD_LIMIT_ONLY"))
        with self.assertRaises(BaseException):
            self.attempt.close_linux_grant_zero_write_once()
        self.assertEqual(calls, [])

    def test_allocated_attachment_custom_origin_key_has_no_callback(self):
        self.reset_with_shared_descendant()
        self._prepare_linux_capture_for_grant_close()
        attachment = DBRIDGE.attach_allocated_owned_domain(
            self.shared_descendant, self.attempt)
        calls = []
        class EvilStr(str):
            def __eq__(self, other):
                calls.append("eq")
                return super().__eq__(other)
            __hash__ = str.__hash__
        seal = attachment.domain_origin.__dict__.pop("seal")
        attachment.domain_origin.__dict__[EvilStr("seal")] = seal
        calls.clear()
        with self.assertRaises(BaseException):
            self.attempt.close_linux_grant_zero_write_once()
        self.assertEqual(calls, [])
        self.assertEqual(self.shared_descendant.state, "UNKNOWN")

    def test_allocated_attachment_rejects_shared_validator_override(self):
        self.reset_with_shared_descendant()
        self._prepare_linux_capture_for_grant_close()
        DBRIDGE.attach_allocated_owned_domain(
            self.shared_descendant, self.attempt)
        calls = []
        def fake_validate(*args, **kwargs):
            calls.append((args, kwargs))
        object.__setattr__(self.shared_descendant, "_validate_forked",
                           fake_validate)
        with self.assertRaises(BaseException):
            self.attempt.close_linux_grant_zero_write_once()
        self.assertEqual(calls, [])
        self.assertEqual(self.shared_descendant.state, "UNKNOWN")

    def test_allocated_attachment_replay_is_terminal(self):
        self.reset_with_shared_descendant()
        self._prepare_linux_capture_for_grant_close()
        DBRIDGE.attach_allocated_owned_domain(
            self.shared_descendant, self.attempt)
        with self.assertRaises(DBRIDGE.Refusal):
            DBRIDGE.attach_allocated_owned_domain(
                self.shared_descendant, self.attempt)
        self.assertEqual(self.shared_descendant.state, "UNKNOWN")
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")

    def test_allocated_attachment_rejects_callable_surface_tamper(self):
        self.reset_with_shared_descendant()
        self._prepare_linux_capture_for_grant_close()
        calls = []

        def fake_enroll(*args, **kwargs):
            calls.append((args, kwargs))

        with mock.patch.object(
                OWNED._CaptureChildAdapter, "enroll_descendant",
                fake_enroll):
            with self.assertRaises(DBRIDGE.Refusal):
                DBRIDGE.attach_allocated_owned_domain(
                    self.shared_descendant, self.attempt)
        self.assertEqual(calls, [])
        self.assertIsNone(self.attempt.descendant_attachment)
        self.assertEqual(self.shared_descendant.state, "UNKNOWN")
        self.assertEqual(self.shared_descendant.domain.state, "UNKNOWN")
        self.assertTrue(self.attempt.capture_adapter.state.startswith(
            "UNKNOWN"))
        with self.assertRaises(BaseException):
            DBRIDGE.attach_allocated_owned_domain(
                self.shared_descendant, self.attempt)

    def test_allocated_attachment_early_validation_poisons_original_adapter(self):
        self.reset_with_shared_descendant()
        self._prepare_linux_capture_for_grant_close()
        adapter = self.attempt.capture_adapter
        with mock.patch.object(
                LAB.AbortForkAttempt, "_validate_linux_capture_chain",
                side_effect=OSError("simulated capture validation fault")):
            with self.assertRaises(OSError):
                DBRIDGE.attach_allocated_owned_domain(
                    self.shared_descendant, self.attempt)
        self.assertIsNone(self.attempt.descendant_attachment)
        self.assertTrue(adapter.state.startswith("UNKNOWN"))
        self.assertEqual(self.shared_descendant.state, "UNKNOWN")

    def test_allocated_attachment_post_validation_domain_drift_is_terminal(self):
        self.reset_with_shared_descendant()
        self._prepare_linux_capture_for_grant_close()
        original = self.attempt._validate_linux_capture_chain
        calls = []
        def validate(*args, **kwargs):
            result = original(*args, **kwargs)
            calls.append(True)
            if len(calls) == 1:
                self.shared_descendant.domain._seal = object()
            return result
        with mock.patch.object(
                LAB.AbortForkAttempt, "_validate_linux_capture_chain",
                side_effect=validate):
            with self.assertRaises(BaseException):
                DBRIDGE.attach_allocated_owned_domain(
                    self.shared_descendant, self.attempt)
        self.assertIsNone(self.attempt.descendant_attachment)
        self.assertTrue(self.attempt.capture_adapter.state.startswith(
            "UNKNOWN"))

    def test_allocated_attachment_effect_then_drift_never_publishes(self):
        self.reset_with_shared_descendant()
        self._prepare_linux_capture_for_grant_close()
        original = self.attempt._validate_linux_capture_chain
        calls = []
        def validate(*args, **kwargs):
            result = original(*args, **kwargs)
            calls.append(True)
            if len(calls) == 2:
                self.shared_descendant.domain.deadline_ns += 1
            return result
        with mock.patch.object(
                LAB.AbortForkAttempt, "_validate_linux_capture_chain",
                side_effect=validate):
            with self.assertRaises(BaseException):
                DBRIDGE.attach_allocated_owned_domain(
                    self.shared_descendant, self.attempt)
        self.assertGreaterEqual(len(calls), 2)
        self.assertIs(self.attempt.capture_adapter.descendant_binding,
                      self.enrollment.binding)
        self.assertIsNone(self.attempt.descendant_attachment)
        self.assertEqual(self.shared_descendant.state, "UNKNOWN")

    def test_allocated_attachment_effect_then_capture_drift_never_publishes(self):
        self.reset_with_shared_descendant()
        self._prepare_linux_capture_for_grant_close()
        original = self.attempt._validate_linux_capture_chain
        calls = []
        def validate(*args, **kwargs):
            result = original(*args, **kwargs)
            calls.append(True)
            if len(calls) == 2:
                self.attempt.capture_handle.monitor_active = True
            return result
        with mock.patch.object(
                LAB.AbortForkAttempt, "_validate_linux_capture_chain",
                side_effect=validate):
            with self.assertRaises(BaseException):
                DBRIDGE.attach_allocated_owned_domain(
                    self.shared_descendant, self.attempt)
        self.assertGreaterEqual(len(calls), 2)
        self.assertIsNone(self.attempt.descendant_attachment)
        self.assertTrue(self.attempt.capture_adapter.state.startswith(
            "UNKNOWN"))
        self.assertEqual(self.shared_descendant.state, "UNKNOWN")

    def test_allocated_attachment_effect_then_stream_limit_drift_is_terminal(self):
        self.reset_with_shared_descendant()
        self._prepare_linux_capture_for_grant_close()
        original = self.attempt._validate_linux_capture_chain
        calls = []
        def validate(*args, **kwargs):
            result = original(*args, **kwargs)
            calls.append(True)
            if len(calls) == 2:
                self.attempt.capture_handle.streams["stdout"].limit += 1
            return result
        with mock.patch.object(
                LAB.AbortForkAttempt, "_validate_linux_capture_chain",
                side_effect=validate):
            with self.assertRaises(BaseException):
                DBRIDGE.attach_allocated_owned_domain(
                    self.shared_descendant, self.attempt)
        self.assertIsNone(self.attempt.descendant_attachment)
        self.assertEqual(self.shared_descendant.state, "UNKNOWN")

    def test_allocated_attachment_effect_then_float_fd_drift_is_terminal(self):
        self.reset_with_shared_descendant()
        self._prepare_linux_capture_for_grant_close()
        original = self.attempt._validate_linux_capture_chain
        calls = []
        def validate(*args, **kwargs):
            result = original(*args, **kwargs)
            calls.append(True)
            if len(calls) == 2:
                stream = self.attempt.capture_handle.streams["stdout"]
                stream.identity["fd"] = float(stream.identity["fd"])
            return result
        with mock.patch.object(
                LAB.AbortForkAttempt, "_validate_linux_capture_chain",
                side_effect=validate):
            with self.assertRaises(BaseException):
                DBRIDGE.attach_allocated_owned_domain(
                    self.shared_descendant, self.attempt)
        self.assertIsNone(self.attempt.descendant_attachment)
        self.assertEqual(self.shared_descendant.state, "UNKNOWN")

    def test_allocated_attachment_effect_then_owned_authority_drift_is_terminal(self):
        mutations = (
            lambda attempt: setattr(attempt.owned_pidfd, "pidfd", 999),
            lambda attempt: setattr(
                attempt.owned_pidfd, "state", "UNKNOWN_TEST"),
            lambda attempt: setattr(
                attempt.capture_adapter, "capture_authority", object()),
            lambda attempt: attempt.handle.owned.discard("grant_parent"),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.reset_with_shared_descendant()
                self._prepare_linux_capture_for_grant_close()
                original = self.attempt._validate_linux_capture_chain
                calls = []
                def validate(*args, **kwargs):
                    result = original(*args, **kwargs)
                    calls.append(True)
                    if len(calls) == 2:
                        mutate(self.attempt)
                    return result
                with mock.patch.object(
                        LAB.AbortForkAttempt, "_validate_linux_capture_chain",
                        side_effect=validate):
                    with self.assertRaises(BaseException):
                        DBRIDGE.attach_allocated_owned_domain(
                            self.shared_descendant, self.attempt)
                self.assertIsNone(self.attempt.descendant_attachment)
                self.assertEqual(self.shared_descendant.state, "UNKNOWN")
                self.assertTrue(self.attempt.capture_adapter.state.startswith(
                    "UNKNOWN"))

    def test_allocated_attachment_effect_then_stored_fd_graph_drift_is_terminal(self):
        mutations = (
            lambda attempt: attempt.handle.bundle["stdout_parent"].__setitem__(
                "fd", attempt.handle.bundle["stdout_parent"]["fd"] + 100),
            lambda attempt: attempt.handle.frozen_bundle[
                "status_parent"].__setitem__(
                    "flags", attempt.handle.frozen_bundle[
                        "status_parent"]["flags"] + 1),
            lambda attempt: setattr(attempt.handle, "active", "TEST_ACTIVE"),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.reset_with_shared_descendant()
                self._prepare_linux_capture_for_grant_close()
                original = self.attempt._validate_linux_capture_chain
                calls = []
                def validate(*args, **kwargs):
                    result = original(*args, **kwargs)
                    calls.append(True)
                    if len(calls) == 2:
                        mutate(self.attempt)
                    return result
                with mock.patch.object(
                        LAB.AbortForkAttempt, "_validate_linux_capture_chain",
                        side_effect=validate):
                    with self.assertRaises(BaseException):
                        DBRIDGE.attach_allocated_owned_domain(
                            self.shared_descendant, self.attempt)
                self.assertIsNone(self.attempt.descendant_attachment)
                self.assertEqual(self.shared_descendant.state, "UNKNOWN")
                self.assertTrue(self.attempt.capture_adapter.state.startswith(
                    "UNKNOWN"))

    def test_allocated_attachment_rejects_nested_fd_validator_override(self):
        self.reset_with_shared_descendant()
        self._prepare_linux_capture_for_grant_close()
        original = self.attempt._validate_linux_capture_chain
        validations = []
        custom_calls = []
        def fake_role(*args, **kwargs):
            custom_calls.append((args, kwargs))
        def validate(*args, **kwargs):
            result = original(*args, **kwargs)
            validations.append(True)
            if len(validations) == 2:
                self.attempt.handle._validate_stored_role = fake_role
            return result
        with mock.patch.object(
                LAB.AbortForkAttempt, "_validate_linux_capture_chain",
                side_effect=validate):
            with self.assertRaises(BaseException):
                DBRIDGE.attach_allocated_owned_domain(
                    self.shared_descendant, self.attempt)
        self.assertEqual(custom_calls, [])
        self.assertIsNone(self.attempt.descendant_attachment)
        self.assertEqual(self.shared_descendant.state, "UNKNOWN")

    def test_allocated_attachment_custom_fd_role_has_no_callback(self):
        self.reset_with_shared_descendant()
        self._prepare_linux_capture_for_grant_close()
        original = self.attempt._validate_linux_capture_chain
        custom_calls = []
        class EvilStr(str):
            def __eq__(self, other):
                custom_calls.append("eq")
                return super().__eq__(other)
        calls = []
        def validate(*args, **kwargs):
            result = original(*args, **kwargs)
            calls.append(True)
            if len(calls) == 2:
                fd = next(iter(self.attempt.capture_handle.fd_roles))
                self.attempt.capture_handle.fd_roles[fd] = EvilStr("stdout")
            return result
        with mock.patch.object(
                LAB.AbortForkAttempt, "_validate_linux_capture_chain",
                side_effect=validate):
            with self.assertRaises(BaseException):
                DBRIDGE.attach_allocated_owned_domain(
                    self.shared_descendant, self.attempt)
        self.assertEqual(custom_calls, [])
        self.assertIsNone(self.attempt.descendant_attachment)

    def test_allocated_attachment_rejects_validation_surface_tamper(self):
        self.reset_with_shared_descendant()
        self._prepare_linux_capture_for_grant_close()
        calls = []
        def fake_assert(*args, **kwargs):
            calls.append((args, kwargs))
        with mock.patch.object(
                OWNED._CaptureChildAdapter, "assert_current_no_callback",
                fake_assert):
            with self.assertRaises(DBRIDGE.Refusal):
                DBRIDGE.attach_allocated_owned_domain(
                    self.shared_descendant, self.attempt)
        self.assertEqual(calls, [])
        self.assertIsNone(self.attempt.descendant_attachment)
        self.assertTrue(self.attempt.capture_adapter.state.startswith(
            "UNKNOWN"))

    def test_allocated_attachment_custom_domain_state_has_no_callback(self):
        self.reset_with_shared_descendant()
        self._prepare_linux_capture_for_grant_close()
        original = self.attempt._validate_linux_capture_chain
        custom_calls = []
        class EvilStr(str):
            def __eq__(self, other):
                custom_calls.append("eq")
                return super().__eq__(other)
        def validate(*args, **kwargs):
            result = original(*args, **kwargs)
            self.shared_descendant.domain.state = EvilStr("PREPARED")
            return result
        with mock.patch.object(
                LAB.AbortForkAttempt, "_validate_linux_capture_chain",
                side_effect=validate):
            with self.assertRaises(BaseException):
                DBRIDGE.attach_allocated_owned_domain(
                    self.shared_descendant, self.attempt)
        self.assertEqual(custom_calls, [])
        self.assertIsNone(self.attempt.descendant_attachment)

    def test_grant_byte_never_classifies_as_abort_eof(self):
        with self.assertRaises(BaseException):
            self.run_mode("grant_data")
        self.assertEqual(self.kernel.exit_code, LAB.EXIT_GRANT_DATA)
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")

    def test_child_held_grant_writer_prevents_eof(self):
        with self.assertRaises(BaseException):
            self.run_mode("hold_grant_writer")
        self.assertIsNone(self.kernel.exit_code)
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")

    def test_short_child_writes_are_completed_exactly(self):
        result = self.run_mode("short_writes")
        self.assertEqual(result["captures"]["stdout_parent"],
                         LAB.STDOUT_CANARY)
        self.assertEqual(result["captures"]["stderr_parent"],
                         LAB.STDERR_CANARY)

    def test_fork_after_effect_error_is_unknown_and_never_reforked(self):
        self.attempt = LAB.prepare_abort_attempt(
            self.persisted, "4" * 32, self.started + 3)
        self.kernel = FakeForkKernel(
            self.handle, self.owner, "fork_after_effect_error")
        with self.assertRaises(OSError):
            self.attempt.run_mock_fork_split(self.kernel)
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")
        self.assertEqual(self.kernel.fork_calls, 1)
        with self.assertRaises(BaseException):
            self.attempt.run_mock_fork_split(self.kernel)
        self.assertEqual(self.kernel.fork_calls, 1)

    def test_integer_zero_is_not_accepted_as_boolean_reaped_state(self):
        with self.assertRaises(BaseException):
            self.run_mode("integer_reaped")
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")

    def test_parent_and_child_monotonic_authority_reject_external_rewrite(self):
        self.attempt = LAB.prepare_abort_attempt(
            self.persisted, "4" * 32, self.started + 3)
        with self.assertRaises(LAB.Refusal):
            self.attempt.deadline_ns += 1
        with self.assertRaises(LAB.Refusal):
            self.attempt.fork_attempted = False
        context = {"schema": 1, "parent": copy.deepcopy(self.owner),
                   "started_ns": self.started,
                   "deadline_ns": self.deadline,
                   "descriptor": copy.deepcopy(self.enrollment.descriptor),
                   "descriptor_digest": self.enrollment.graph_digest}
        child = LAB.AbortChildRoutine(LAB._CHILD_KEY, context)
        with self.assertRaises(LAB.Refusal):
            child.exit_attempted = False
        with self.assertRaises(LAB.Refusal):
            child.last_clock_ns = 0

    def test_linux_fork_pid_is_recorded_then_exact_pidfd_is_bound_unarmed(self):
        self.attempt = LAB.prepare_abort_attempt(
            self.persisted, "4" * 32, self.started + 3)
        pid = self.owner["pid"] + 1000
        identity = self.persisted.bound.request["launcher_identity"]
        launcher = {"kind": "UNARMED_LAUNCHER_NOT_PAYLOAD",
                    "exe_dev": identity["interpreter"]["dev"],
                    "exe_inode": identity["interpreter"]["inode"],
                    "cmdline_sha256":
                    identity["inherited"]["proc_cmdline_sha256"]}
        calls = FakeOwnedLinuxCalls(self.owner, launcher, pid)
        self.owned_calls = calls
        with mock.patch.object(FDS.LinuxFDCalls, "owner_identity",
                               return_value=copy.deepcopy(self.owner)), \
                mock.patch.object(OWNED, "LinuxSyscalls", return_value=calls), \
                mock.patch.object(LINUX, "fork_abort_child_once",
                                  return_value=pid) as fork, \
                mock.patch.object(LAB.time, "monotonic_ns",
                                  return_value=self.started + 4):
            result = self.attempt.fork_and_bind_linux_pidfd_once()
            os.write(self.handle.frozen_bundle["status_child"]["fd"],
                     LAB.STATUS_READY)
            capture_result = self.attempt.prepare_linux_capture_unarmed_once()
        self.assertEqual(result["classification"],
                         "LINUX_ABORT_PIDFD_BOUND_UNARMED")
        self.assertEqual(self.attempt.returned_pid, pid)
        self.assertEqual(self.attempt.owned_pidfd.pidfd, 55)
        self.assertTrue(self.attempt.ticket.used)
        self.assertEqual(fork.call_count, 1)
        self.assertEqual(capture_result["classification"],
                         "LINUX_ABORT_CAPTURE_BOUND_UNARMED")
        self.assertEqual(self.attempt.state, "LINUX_CAPTURE_BOUND_UNARMED")
        self.assertEqual(self.attempt.status_ready, LAB.STATUS_READY)
        self.assertEqual(self.handle.owned, {"grant_parent", "status_parent",
                                            "stdout_parent", "stderr_parent"})

    def test_linux_pidfd_bind_fault_is_unknown_survivor_and_never_reforks(self):
        self.attempt = LAB.prepare_abort_attempt(
            self.persisted, "4" * 32, self.started + 3)
        pid = self.owner["pid"] + 1000
        identity = self.persisted.bound.request["launcher_identity"]
        launcher = {"kind": "UNARMED_LAUNCHER_NOT_PAYLOAD",
                    "exe_dev": identity["interpreter"]["dev"],
                    "exe_inode": identity["interpreter"]["inode"],
                    "cmdline_sha256":
                    identity["inherited"]["proc_cmdline_sha256"]}
        calls = FakeOwnedLinuxCalls(self.owner, launcher, pid, True)
        with mock.patch.object(FDS.LinuxFDCalls, "owner_identity",
                               return_value=copy.deepcopy(self.owner)), \
                mock.patch.object(OWNED, "LinuxSyscalls", return_value=calls), \
                mock.patch.object(LINUX, "fork_abort_child_once",
                                  return_value=pid) as fork, \
                mock.patch.object(LAB.time, "monotonic_ns",
                                  return_value=self.started + 4):
            with self.assertRaises(OSError):
                self.attempt.fork_and_bind_linux_pidfd_once()
            self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")
            with self.assertRaises(BaseException):
                self.attempt.fork_and_bind_linux_pidfd_once()
        self.assertEqual(fork.call_count, 1)

    def _bind_linux_unarmed_for_capture(self):
        self.attempt = LAB.prepare_abort_attempt(
            self.persisted, "4" * 32, self.started + 3)
        pid = self.owner["pid"] + 1000
        identity = self.persisted.bound.request["launcher_identity"]
        launcher = {"kind": "UNARMED_LAUNCHER_NOT_PAYLOAD",
                    "exe_dev": identity["interpreter"]["dev"],
                    "exe_inode": identity["interpreter"]["inode"],
                    "cmdline_sha256":
                    identity["inherited"]["proc_cmdline_sha256"]}
        calls = FakeOwnedLinuxCalls(self.owner, launcher, pid)
        self.owned_calls = calls
        with mock.patch.object(FDS.LinuxFDCalls, "owner_identity",
                               return_value=copy.deepcopy(self.owner)), \
                mock.patch.object(OWNED, "LinuxSyscalls", return_value=calls), \
                mock.patch.object(LINUX, "fork_abort_child_once",
                                  return_value=pid), \
                mock.patch.object(LAB.time, "monotonic_ns",
                                  return_value=self.started + 4):
            self.attempt.fork_and_bind_linux_pidfd_once()
        os.write(self.handle.frozen_bundle["status_child"]["fd"],
                 LAB.STATUS_READY)
        return pid

    def _prepare_linux_capture_for_grant_close(self):
        self._bind_linux_unarmed_for_capture()
        with mock.patch.object(OWNED, "LinuxSyscalls",
                               return_value=self.owned_calls), \
                mock.patch.object(LAB.time, "monotonic_ns",
                               return_value=self.started + 5):
            self.attempt.prepare_linux_capture_unarmed_once()

    def _complete_linux_monitor(self, status=LAB.EXIT_ABORT_EOF,
                                stdout=LAB.STDOUT_CANARY,
                                stderr=LAB.STDERR_CANARY):
        self._bind_linux_unarmed_for_capture()
        os.write(self.handle.frozen_bundle["stdout_child"]["fd"],
                 stdout)
        os.write(self.handle.frozen_bundle["stderr_child"]["fd"],
                 stderr)
        with mock.patch.object(OWNED, "LinuxSyscalls",
                               return_value=self.owned_calls), \
                mock.patch.object(LAB.time, "monotonic_ns",
                                  return_value=self.started + 5):
            self.attempt.prepare_linux_capture_unarmed_once()
        self.owned_calls.waits = [types.SimpleNamespace(
            si_pid=self.attempt.returned_pid, si_code=os.CLD_EXITED,
            si_status=status)]
        ticks = iter(range(self.started + 6, self.started + 100))
        with mock.patch.object(LAB.time, "monotonic_ns",
                               side_effect=lambda: next(ticks)):
            self.attempt.close_linux_grant_zero_write_once()
            self.attempt.monitor_linux_abort_unreaped_once()
        return self.owned_calls

    def _complete_attached_linux_monitor(
            self, status=LAB.EXIT_ABORT_EOF,
            stdout=LAB.STDOUT_CANARY, stderr=LAB.STDERR_CANARY):
        self._bind_linux_unarmed_for_capture()
        os.write(self.handle.frozen_bundle["stdout_child"]["fd"],
                 stdout)
        os.write(self.handle.frozen_bundle["stderr_child"]["fd"],
                 stderr)
        with mock.patch.object(OWNED, "LinuxSyscalls",
                               return_value=self.owned_calls), \
                mock.patch.object(LAB.time, "monotonic_ns",
                                  return_value=self.started + 5):
            self.attempt.prepare_linux_capture_unarmed_once()
        attachment = DBRIDGE.attach_allocated_owned_domain(
            self.shared_descendant, self.attempt)
        observation = types.SimpleNamespace(
            si_pid=self.attempt.returned_pid, si_code=os.CLD_EXITED,
            si_status=status)
        self.owned_calls.waits = [observation]
        ticks = iter(range(self.started + 6, self.started + 100))
        with mock.patch.object(LAB.time, "monotonic_ns",
                               side_effect=lambda: next(ticks)):
            self.attempt.close_linux_grant_zero_write_once()
            self.attempt.monitor_linux_abort_unreaped_once()
        return attachment, observation

    def _complete_attached_linux_reap(
            self, reap_start=None, status=LAB.EXIT_ABORT_EOF,
            stdout=LAB.STDOUT_CANARY, stderr=LAB.STDERR_CANARY):
        attachment, observation = self._complete_attached_linux_monitor(
            status, stdout, stderr)
        self.owned_calls.waits = [observation]
        start = self.started + 100 if reap_start is None else reap_start
        ticks = iter(range(start, start + 100))
        with mock.patch.object(LAB.time, "monotonic_ns",
                               side_effect=lambda: next(ticks)):
            result = self.attempt.reap_linux_completion_once()
        self.assertEqual(result["classification"],
                         "LINUX_LEADER_REAPED_UNQUALIFIED")
        return attachment

    def _complete_allocated_drain_ready(
            self, status=LAB.EXIT_ABORT_EOF,
            stdout=LAB.STDOUT_CANARY, stderr=LAB.STDERR_CANARY):
        attachment = self._complete_attached_linux_reap(
            status=status, stdout=stdout, stderr=stderr)
        settlement = self.attempt.settlement_handle
        bind_now = settlement.receipt["finished_ns"] + 1
        binding = DBRIDGE.bind_allocated_settlement_deadline(
            attachment, settlement,
            types.SimpleNamespace(monotonic_ns=lambda: bind_now))
        bridge = DBRIDGE.transfer_allocated_descendant_drain(
            binding, settlement,
            types.SimpleNamespace(monotonic_ns=lambda: bind_now + 1))
        return bridge, bind_now + 1

    def _complete_allocated_terminal_evidence(
            self, status=LAB.EXIT_ABORT_EOF,
            stdout=LAB.STDOUT_CANARY, stderr=LAB.STDERR_CANARY):
        bridge, watermark = self._complete_allocated_drain_ready(
            status, stdout, stderr)
        calls = AllocatedStepCalls(self.owner, watermark + 1)
        calls.discovery = [ChildProcessError(errno.ECHILD, "none")]
        result = DBRIDGE.advance_allocated_descendant_once(bridge, calls)
        return result.terminal_evidence

    def _complete_allocated_status_eof(
            self, status=LAB.EXIT_ABORT_EOF,
            stdout=LAB.STDOUT_CANARY, stderr=LAB.STDERR_CANARY):
        terminal = self._complete_allocated_terminal_evidence(
            status, stdout, stderr)
        calls = AllocatedStatusCalls(terminal._watermark_ns + 1)
        result = APROTO.observe_allocated_status_tail_once(terminal, calls)
        return result.eof_evidence

    def _complete_allocated_protocol_match(self):
        status_eof = self._complete_allocated_status_eof()
        return APROTO.match_allocated_abort_protocol_once(
            status_eof).capability

    def _complete_allocated_release_inputs(self):
        protocol = self._complete_allocated_protocol_match()
        return ARELEASE.qualify_allocated_release_inputs_once(protocol)

    def _complete_allocated_release_deadline(self):
        inputs = self._complete_allocated_release_inputs()
        terminal = inputs._terminal
        status = inputs._status_eof
        lower = max(terminal._watermark_ns, status._before_ns,
                    status._after_ns, status._operation.watermark.value,
                    self.attempt.last_clock_ns)
        calls = AllocatedDeadlineCalls(lower)
        return ADEADLINE.admit_allocated_release_deadline_once(
            inputs, calls).capability

    def _complete_linux_reap(self, status=LAB.EXIT_ABORT_EOF,
                             stdout=LAB.STDOUT_CANARY,
                             stderr=LAB.STDERR_CANARY):
        calls = self._complete_linux_monitor(status, stdout, stderr)
        calls.waits = [types.SimpleNamespace(
            si_pid=self.attempt.returned_pid, si_code=os.CLD_EXITED,
            si_status=status)]
        ticks = iter(range(self.started + 100, self.started + 200))
        with mock.patch.object(LAB.time, "monotonic_ns",
                               side_effect=lambda: next(ticks)):
            result = self.attempt.reap_linux_completion_once()
        self.assertEqual(result["classification"],
                         "LINUX_LEADER_REAPED_UNQUALIFIED")
        return calls

    def _complete_linux_status(self, status=LAB.EXIT_ABORT_EOF,
                               stdout=LAB.STDOUT_CANARY,
                               stderr=LAB.STDERR_CANARY):
        calls = self._complete_linux_reap(status, stdout, stderr)
        ticks = iter(range(self.started + 200, self.started + 220))
        with mock.patch.object(LAB.time, "monotonic_ns",
                               side_effect=lambda: next(ticks)):
            result = self.attempt.observe_linux_status_tail_once()
        self.assertEqual(result["classification"],
                         "LINUX_STATUS_EOF_OBSERVED_UNQUALIFIED")
        return calls

    def _claim_terminal_release(self):
        calls = self._complete_linux_status()
        self.attempt.verify_linux_abort_protocol_once()
        with mock.patch.object(LAB.time, "monotonic_ns",
                               return_value=self.started + 300):
            claim = self.attempt.claim_abort_terminal_release_once()
        return claim, calls

    def _claim_with_epoll_release(self):
        claim, calls = self._claim_terminal_release()
        epoll = SourceEpollReleaseBackend(
            self.started + 301, finished_ns=self.started + 302)
        claim.release_epoll_model_once(epoll)
        return claim, calls, epoll

    def _claim_with_status_release(self):
        claim, calls, epoll = self._claim_with_epoll_release()
        status = SourceStatusReleaseBackend(
            self.started + 303, finished_ns=self.started + 304)
        claim.release_status_model_once(status)
        return claim, calls, epoll, status

    def _claim_with_stdout_release(self):
        claim, calls, epoll, status = self._claim_with_status_release()
        stdout = SourceStdoutReleaseBackend(
            self.started + 305, finished_ns=self.started + 306)
        claim.release_stdout_model_once(stdout)
        return claim, calls, epoll, status, stdout

    def _claim_with_stderr_release(self):
        claim, calls, epoll, status, stdout = (
            self._claim_with_stdout_release())
        stderr = SourceStderrReleaseBackend(
            self.started + 307, finished_ns=self.started + 308)
        claim.release_stderr_model_once(stderr)
        return claim, calls, epoll, status, stdout, stderr

    def _claim_with_pidfd_anchor(self):
        values = self._claim_with_stderr_release()
        claim = values[0]
        anchor_backend = SourcePidfdAnchorBackend(self.started + 309)
        capability = claim.acquire_pidfd_anchor_model_once(anchor_backend)
        return (*values, anchor_backend, capability)

    def test_linux_grant_eof_is_one_exact_zero_write_close(self):
        self._prepare_linux_capture_for_grant_close()
        with mock.patch.object(LAB.time, "monotonic_ns",
                               return_value=self.started + 6):
            result = self.attempt.close_linux_grant_zero_write_once()
        self.assertEqual(result["classification"],
                         "LINUX_ABORT_GRANT_WRITER_CLOSED_UNSETTLED")
        self.assertEqual(self.attempt.state,
                         "LINUX_GRANT_WRITER_CLOSED_UNSETTLED")
        self.assertFalse(result["child_eof_observed"])
        self.assertNotIn("grant_parent", self.handle.owned)
        self.assertEqual(self.handle.attempted_writes, set())
        self.assertEqual(self.handle.attempted_closes,
                         set(LAB.PARENT_CLOSE_ROLES) | {"grant_parent"})
        with self.assertRaises(BaseException):
            self.attempt.close_linux_grant_zero_write_once()
        self.assertEqual(self.handle.attempted_closes,
                         set(LAB.PARENT_CLOSE_ROLES) | {"grant_parent"})

    def test_linux_expired_before_grant_close_makes_zero_close_calls(self):
        self._prepare_linux_capture_for_grant_close()
        original = self.handle.calls.close
        with mock.patch.object(self.handle.calls, "close",
                               wraps=original) as close, \
                mock.patch.object(LAB.time, "monotonic_ns",
                                  return_value=self.deadline):
            with self.assertRaises(BaseException):
                self.attempt.close_linux_grant_zero_write_once()
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")
        self.assertIn("grant_parent", self.handle.owned)
        self.assertEqual(close.call_count, 0)
        self.assertEqual(self.handle.attempted_writes, set())
        self.assertEqual(self.handle.attempted_closes,
                         set(LAB.PARENT_CLOSE_ROLES))

    def test_linux_deadline_crossing_during_grant_close_is_one_close(self):
        self._prepare_linux_capture_for_grant_close()
        original = self.handle.calls.close
        with mock.patch.object(self.handle.calls, "close",
                               wraps=original) as close, \
                mock.patch.object(LAB.time, "monotonic_ns",
                                  side_effect=[self.started + 6,
                                               self.deadline]):
            with self.assertRaises(BaseException):
                self.attempt.close_linux_grant_zero_write_once()
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")
        self.assertNotIn("grant_parent", self.handle.owned)
        self.assertEqual(close.call_count, 1)
        self.assertEqual(self.handle.attempted_writes, set())

    def test_linux_extra_writer_never_turns_close_into_eof_claim(self):
        self._prepare_linux_capture_for_grant_close()
        duplicate = os.dup(
            self.handle.frozen_bundle["grant_parent"]["fd"])
        try:
            with mock.patch.object(LAB.time, "monotonic_ns",
                                   return_value=self.started + 6):
                result = self.attempt.close_linux_grant_zero_write_once()
            self.assertEqual(result["classification"],
                             "LINUX_ABORT_GRANT_WRITER_CLOSED_UNSETTLED")
            self.assertFalse(result["child_eof_observed"])
            os.fstat(duplicate)
        finally:
            os.close(duplicate)

    def test_linux_monitor_keeps_exit73_unreaped_and_not_abort_verified(self):
        self._bind_linux_unarmed_for_capture()
        os.write(self.handle.frozen_bundle["stdout_child"]["fd"],
                 LAB.STDOUT_CANARY)
        os.write(self.handle.frozen_bundle["stderr_child"]["fd"],
                 LAB.STDERR_CANARY)
        with mock.patch.object(OWNED, "LinuxSyscalls",
                               return_value=self.owned_calls), \
                mock.patch.object(LAB.time, "monotonic_ns",
                               return_value=self.started + 5):
            self.attempt.prepare_linux_capture_unarmed_once()
        calls = self.attempt.capture_adapter.syscalls
        calls.waits = [types.SimpleNamespace(
            si_pid=self.attempt.returned_pid, si_code=os.CLD_EXITED,
            si_status=LAB.EXIT_ABORT_EOF)]
        ticks = iter(range(self.started + 6, self.started + 100))
        with mock.patch.object(LAB.time, "monotonic_ns",
                               side_effect=lambda: next(ticks)):
            self.attempt.close_linux_grant_zero_write_once()
            result = self.attempt.monitor_linux_abort_unreaped_once()
        self.assertEqual(result["classification"],
                         "LINUX_ABORT_MONITOR_COMPLETE_UNREAPED")
        self.assertEqual(result["terminal_status"], LAB.EXIT_ABORT_EOF)
        self.assertFalse(result["abort_protocol_verified"])
        self.assertFalse(result["status_eof_verified"])
        self.assertFalse(result["leader_reaped"])
        self.assertEqual(self.attempt.state,
                         "LINUX_MONITOR_COMPLETE_UNREAPED")
        self.assertIs(self.attempt.monitor_capability,
                      self.attempt.capture_handle.normal_completion_capability)
        self.assertEqual(calls.wait_options,
                         [os.WEXITED | os.WNOHANG | os.WNOWAIT])
        calls.waits = [types.SimpleNamespace(
            si_pid=self.attempt.returned_pid, si_code=os.CLD_EXITED,
            si_status=LAB.EXIT_ABORT_EOF)]
        reap_ticks = iter(range(self.started + 100, self.started + 200))
        with mock.patch.object(LAB.time, "monotonic_ns",
                               side_effect=lambda: next(reap_ticks)):
            reaped = self.attempt.reap_linux_completion_once()
        self.assertEqual(reaped["classification"],
                         "LINUX_LEADER_REAPED_UNQUALIFIED")
        self.assertTrue(reaped["leader_reap_confirmed"])
        self.assertFalse(reaped["abort_protocol_verified"])
        self.assertEqual(self.attempt.state,
                         "LINUX_LEADER_REAPED_UNQUALIFIED")
        self.assertIsNotNone(self.attempt.settlement_handle)
        self.assertEqual(calls.wait_options,
                         [os.WEXITED | os.WNOHANG | os.WNOWAIT,
                          os.WEXITED | os.WNOHANG])
        with self.assertRaises(BaseException):
            self.attempt.reap_linux_completion_once()

    def test_linux_monitor_timeout_is_retained_and_never_retried(self):
        self._prepare_linux_capture_for_grant_close()
        ticks = [self.started + 6]
        def clock():
            value = ticks[-1]
            ticks.append(self.deadline if len(ticks) >= 6 else value + 1)
            return value
        with mock.patch.object(LAB.time, "monotonic_ns", side_effect=clock):
            self.attempt.close_linux_grant_zero_write_once()
            result = self.attempt.monitor_linux_abort_unreaped_once()
        self.assertEqual(result["classification"],
                         "LINUX_ABORT_MONITOR_FAILURE_UNSETTLED")
        self.assertEqual(result["monitor_classification"],
                         "EXECUTION_TIMEOUT_UNSETTLED")
        self.assertEqual(self.attempt.state,
                         "LINUX_MONITOR_FAILURE_UNSETTLED")
        self.assertIsNone(self.attempt.monitor_capability)
        self.assertEqual(self.attempt.monitor_result,
                         self.attempt.capture_handle.primary_failure)
        with self.assertRaises(BaseException):
            self.attempt.monitor_linux_abort_unreaped_once()

    def test_linux_monitor_first_clock_cannot_regress_from_admission(self):
        self._prepare_linux_capture_for_grant_close()
        with mock.patch.object(LAB.time, "monotonic_ns",
                               return_value=self.started + 6):
            self.attempt.close_linux_grant_zero_write_once()
        with mock.patch.object(LAB.time, "monotonic_ns",
                               side_effect=[self.started + 8,
                                            self.started + 7]):
            with self.assertRaises(BaseException):
                self.attempt.monitor_linux_abort_unreaped_once()
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")
        self.assertTrue(self.attempt.monitor_attempted)
        self.assertIsNone(self.attempt.monitor_capability)
        with self.assertRaises(BaseException):
            self.attempt.monitor_linux_abort_unreaped_once()

    def test_linux_post_monitor_fd_poison_retains_unauthorized_result(self):
        self._bind_linux_unarmed_for_capture()
        os.write(self.handle.frozen_bundle["stdout_child"]["fd"],
                 LAB.STDOUT_CANARY)
        os.write(self.handle.frozen_bundle["stderr_child"]["fd"],
                 LAB.STDERR_CANARY)
        with mock.patch.object(OWNED, "LinuxSyscalls",
                               return_value=self.owned_calls), \
                mock.patch.object(LAB.time, "monotonic_ns",
                                  return_value=self.started + 5):
            self.attempt.prepare_linux_capture_unarmed_once()
        self.owned_calls.waits = [types.SimpleNamespace(
            si_pid=self.attempt.returned_pid, si_code=os.CLD_EXITED,
            si_status=LAB.EXIT_ABORT_EOF)]
        original = self.owned_calls.waitid
        def waitid_then_poison(pidfd, options):
            result = original(pidfd, options)
            self.handle.poisoned = True
            self.handle.state = "UNKNOWN_TEST_POISON"
            return result
        ticks = iter(range(self.started + 6, self.started + 100))
        with mock.patch.object(self.owned_calls, "waitid",
                               side_effect=waitid_then_poison), \
                mock.patch.object(LAB.time, "monotonic_ns",
                                  side_effect=lambda: next(ticks)):
            self.attempt.close_linux_grant_zero_write_once()
            with self.assertRaises(BaseException):
                self.attempt.monitor_linux_abort_unreaped_once()
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")
        self.assertIsNotNone(self.attempt.monitor_result)
        self.assertIsNone(self.attempt.monitor_capability)

    def test_linux_post_monitor_origin_drift_retains_unauthorized_result(self):
        self._bind_linux_unarmed_for_capture()
        os.write(self.handle.frozen_bundle["stdout_child"]["fd"],
                 LAB.STDOUT_CANARY)
        os.write(self.handle.frozen_bundle["stderr_child"]["fd"],
                 LAB.STDERR_CANARY)
        with mock.patch.object(OWNED, "LinuxSyscalls",
                               return_value=self.owned_calls), \
                mock.patch.object(LAB.time, "monotonic_ns",
                                  return_value=self.started + 5):
            self.attempt.prepare_linux_capture_unarmed_once()
        self.owned_calls.waits = [types.SimpleNamespace(
            si_pid=self.attempt.returned_pid, si_code=os.CLD_EXITED,
            si_status=LAB.EXIT_ABORT_EOF)]
        original = self.owned_calls.waitid
        def waitid_then_drift(pidfd, options):
            result = original(pidfd, options)
            self.attempt._lifecycle_origin.claimed = False
            return result
        ticks = iter(range(self.started + 6, self.started + 100))
        with mock.patch.object(self.owned_calls, "waitid",
                               side_effect=waitid_then_drift), \
                mock.patch.object(LAB.time, "monotonic_ns",
                                  side_effect=lambda: next(ticks)):
            self.attempt.close_linux_grant_zero_write_once()
            with self.assertRaises(BaseException):
                self.attempt.monitor_linux_abort_unreaped_once()
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")
        self.assertIsNotNone(self.attempt.monitor_result)
        self.assertIsNone(self.attempt.monitor_capability)

    def test_linux_deadline_after_reap_retains_confirmed_unknown(self):
        calls = self._complete_linux_monitor()
        calls.waits = [types.SimpleNamespace(
            si_pid=self.attempt.returned_pid, si_code=os.CLD_EXITED,
            si_status=LAB.EXIT_ABORT_EOF)]
        admission = self.started + 100
        cleanup_deadline = admission + 100000000
        with mock.patch.object(LAB.time, "monotonic_ns", side_effect=[
                admission, admission + 1, admission + 2,
                cleanup_deadline]):
            result = self.attempt.reap_linux_completion_once()
        self.assertEqual(result["classification"],
                         "LINUX_REAP_UNKNOWN_RETAINED")
        self.assertTrue(result["leader_reap_confirmed"])
        self.assertTrue(self.attempt.leader_reap_confirmed)
        self.assertIsNotNone(self.attempt.settlement_handle)
        self.assertEqual(calls.wait_options,
                         [os.WEXITED | os.WNOHANG | os.WNOWAIT,
                          os.WEXITED | os.WNOHANG])
        with self.assertRaises(BaseException):
            self.attempt.reap_linux_completion_once()

    def test_linux_cleanup_clock_poison_dispatches_zero_reap_calls(self):
        calls = self._complete_linux_monitor()
        def poison_clock():
            self.handle.poisoned = True
            self.handle.state = "UNKNOWN_TEST_POISON"
            return self.started + 100
        with mock.patch.object(LAB.time, "monotonic_ns",
                               side_effect=poison_clock):
            with self.assertRaises(BaseException):
                self.attempt.reap_linux_completion_once()
        self.assertEqual(len(calls.wait_options), 1)
        self.assertTrue(self.attempt.reap_attempted)
        self.assertIsNone(self.attempt.settlement_handle)

    def test_linux_mutated_settlement_is_retained_but_never_success(self):
        calls = self._complete_linux_monitor()
        calls.waits = [types.SimpleNamespace(
            si_pid=self.attempt.returned_pid, si_code=os.CLD_EXITED,
            si_status=LAB.EXIT_ABORT_EOF)]
        original = CAPTURE.reap_attached_completion_once
        def mutate_after_reap(handle, deadline, syscalls, minimum):
            settlement = original(handle, deadline, syscalls, minimum)
            settlement.receipt["runtime_authorized"] = True
            return settlement
        ticks = iter(range(self.started + 100, self.started + 200))
        with mock.patch.object(CAPTURE, "reap_attached_completion_once",
                               side_effect=mutate_after_reap), \
                mock.patch.object(LAB.time, "monotonic_ns",
                                  side_effect=lambda: next(ticks)):
            with self.assertRaises(BaseException):
                self.attempt.reap_linux_completion_once()
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")
        self.assertIsNotNone(self.attempt.settlement_handle)
        self.assertFalse(self.attempt.leader_reap_confirmed)
        self.assertEqual(calls.wait_options,
                         [os.WEXITED | os.WNOHANG | os.WNOWAIT,
                          os.WEXITED | os.WNOHANG])

    def test_linux_admission_capture_rewrite_dispatches_zero_reap_calls(self):
        calls = self._complete_linux_monitor()
        cap = self.attempt.monitor_capability
        def rewrite_clock():
            stream = self.attempt.capture_handle.streams["stdout"]
            stream.stored[0] ^= 1
            digest = hashlib.sha256(bytes(stream.stored)).hexdigest()
            for value in (cap.completion,
                          self.attempt.capture_handle.normal_completion):
                value["streams"]["stdout"]["prefix_sha256"] = digest
            cap.completion_digest = CAPTURE._completion_digest(cap.completion)
            self.attempt.capture_adapter.normal_completion_digest = \
                cap.completion_digest
            return self.started + 100
        with mock.patch.object(LAB.time, "monotonic_ns",
                               side_effect=rewrite_clock):
            with self.assertRaises(BaseException):
                self.attempt.reap_linux_completion_once()
        self.assertEqual(len(calls.wait_options), 1)
        self.assertTrue(self.attempt.reap_attempted)
        self.assertIsNone(self.attempt.settlement_handle)

    def test_linux_reaped_status_tail_requires_real_empty_eof(self):
        calls = self._complete_linux_reap()
        ticks = iter(range(self.started + 200, self.started + 220))
        with mock.patch.object(LAB.time, "monotonic_ns",
                               side_effect=lambda: next(ticks)):
            result = self.attempt.observe_linux_status_tail_once()
        self.assertEqual(result["classification"],
                         "LINUX_STATUS_EOF_OBSERVED_UNQUALIFIED")
        self.assertTrue(result["status_eof_verified"])
        self.assertFalse(result["abort_protocol_verified"])
        self.assertTrue(self.attempt.status_eof_observed)
        self.assertEqual(self.attempt.state,
                         "LINUX_STATUS_EOF_OBSERVED_UNQUALIFIED")
        self.assertEqual(calls.wait_options,
                         [os.WEXITED | os.WNOHANG | os.WNOWAIT,
                          os.WEXITED | os.WNOHANG])
        with self.assertRaises(BaseException):
            self.attempt.observe_linux_status_tail_once()

    def test_linux_reaped_status_tail_rejects_any_extra_byte(self):
        self._complete_linux_reap()
        # Simulate an already-buffered protocol violation without manufacturing
        # a new status authority: the original writer was closed by the parent.
        read = self.handle.calls.read
        first = True
        def extra_once(fd, maximum):
            nonlocal first
            if (first and fd
                    == self.handle.frozen_bundle["status_parent"]["fd"]):
                first = False
                return b"R"
            return read(fd, maximum)
        ticks = iter(range(self.started + 200, self.started + 220))
        with mock.patch.object(self.handle.calls, "read",
                               side_effect=extra_once), \
                mock.patch.object(LAB.time, "monotonic_ns",
                                  side_effect=lambda: next(ticks)):
            result = self.attempt.observe_linux_status_tail_once()
        self.assertEqual(result["classification"],
                         "LINUX_STATUS_TAIL_NOT_EMPTY")
        self.assertFalse(result["status_eof_verified"])
        self.assertEqual(result["tail_hex"], "52")
        self.assertEqual(result["tail_bytes"], 1)
        self.assertFalse(self.attempt.status_eof_observed)
        self.assertEqual(self.handle.attempted_writes, set())

    def test_linux_status_eof_after_deadline_is_retained_unknown(self):
        self._complete_linux_reap()
        deadline = self.attempt.cleanup_deadline_ns
        with mock.patch.object(LAB.time, "monotonic_ns",
                               side_effect=[deadline - 2, deadline - 1,
                                            deadline]):
            with self.assertRaises(BaseException):
                self.attempt.observe_linux_status_tail_once()
        self.assertTrue(self.attempt.status_tail_attempted)
        self.assertTrue(self.attempt.status_eof_observed)
        self.assertIsNotNone(self.attempt.status_eof_receipt)
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")
        self.assertEqual(self.handle.attempted_writes, set())

    def test_linux_status_eof_at_post_read_deadline_is_retained(self):
        self._complete_linux_reap()
        deadline = self.attempt.cleanup_deadline_ns
        with mock.patch.object(LAB.time, "monotonic_ns",
                               side_effect=[deadline - 1, deadline]):
            with self.assertRaises(BaseException):
                self.attempt.observe_linux_status_tail_once()
        self.assertTrue(self.attempt.status_tail_attempted)
        self.assertTrue(self.attempt.status_eof_observed)
        self.assertEqual(self.attempt.status_read_observation["kind"], "EOF")
        self.assertFalse(
            self.attempt.status_read_observation["status_eof_verified"])
        self.assertIsNone(self.attempt.status_eof_receipt)
        self.assertTrue(self.attempt.leader_reap_confirmed)
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")

    def test_linux_status_custom_live_settlement_refuses_before_read(self):
        self._complete_linux_reap()
        class CustomDict(dict):
            pass
        self.attempt.settlement_handle.receipt = CustomDict(
            self.attempt.settlement_handle.receipt)
        with mock.patch.object(self.handle.calls, "read",
                               wraps=self.handle.calls.read) as read:
            with self.assertRaises(BaseException):
                self.attempt.observe_linux_status_tail_once()
        self.assertEqual(read.call_count, 0)
        self.assertFalse(self.attempt.status_tail_attempted)
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")

    def test_linux_status_post_read_clock_poison_blocks_data_result(self):
        self._complete_linux_reap()
        status_fd = self.handle.frozen_bundle["status_parent"]["fd"]
        original = self.handle.calls.read
        def extra(fd, maximum):
            return b"R" if fd == status_fd else original(fd, maximum)
        calls = 0
        def clock():
            nonlocal calls
            calls += 1
            if calls == 2:
                self.handle.poisoned = True
                self.handle.state = "UNKNOWN_TEST_POISON"
            return self.started + 200 + calls
        with mock.patch.object(self.handle.calls, "read", side_effect=extra), \
                mock.patch.object(LAB.time, "monotonic_ns",
                                  side_effect=clock):
            with self.assertRaises(BaseException):
                self.attempt.observe_linux_status_tail_once()
        self.assertIsNone(self.attempt.status_eof_receipt)
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")

    def test_linux_status_post_read_clock_poison_blocks_eagain_poll(self):
        self._complete_linux_reap()
        def eagain(fd, maximum):
            raise BlockingIOError(errno.EAGAIN, "again")
        calls = 0
        def clock():
            nonlocal calls
            calls += 1
            if calls == 2:
                self.handle.poisoned = True
                self.handle.state = "UNKNOWN_TEST_POISON"
            return self.started + 200 + calls
        poller = mock.Mock()
        with mock.patch.object(self.handle.calls, "read", side_effect=eagain), \
                mock.patch.object(LAB.time, "monotonic_ns",
                                  side_effect=clock), \
                mock.patch.object(LAB.select, "poll",
                                  return_value=poller) as poll:
            with self.assertRaises(BaseException):
                self.attempt.observe_linux_status_tail_once()
        self.assertEqual(poll.call_count, 0)
        self.assertIsNone(self.attempt.status_eof_receipt)
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")

    def test_linux_status_final_clock_custom_evidence_is_unknown(self):
        self._complete_linux_reap()
        class CustomDict(dict):
            pass
        calls = 0
        def clock():
            nonlocal calls
            calls += 1
            if calls == 3:
                self.attempt.status_eof_receipt["nested"] = CustomDict()
            return self.started + 200 + calls
        with mock.patch.object(LAB.time, "monotonic_ns",
                               side_effect=clock):
            with self.assertRaises(BaseException):
                self.attempt.observe_linux_status_tail_once()
        self.assertTrue(self.attempt.status_eof_observed)
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")

    def test_linux_status_final_clock_plain_evidence_drift_is_unknown(self):
        self._complete_linux_reap()
        calls = 0
        def clock():
            nonlocal calls
            calls += 1
            if calls == 3:
                self.attempt.status_eof_receipt["observed_ns"] += 1
            return self.started + 200 + calls
        with mock.patch.object(LAB.time, "monotonic_ns",
                               side_effect=clock):
            with self.assertRaises(BaseException):
                self.attempt.observe_linux_status_tail_once()
        self.assertTrue(self.attempt.status_eof_observed)
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")

    def test_linux_status_final_clock_origin_ticket_drift_is_unknown(self):
        self._complete_linux_reap()
        calls = 0
        def clock():
            nonlocal calls
            calls += 1
            if calls == 3:
                self.attempt._lifecycle_origin.pre_fork_enrollment = None
            return self.started + 200 + calls
        with mock.patch.object(LAB.time, "monotonic_ns",
                               side_effect=clock):
            with self.assertRaises(BaseException):
                self.attempt.observe_linux_status_tail_once()
        self.assertTrue(self.attempt.status_eof_observed)
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")

    def test_linux_status_final_clock_custom_lifecycle_preflights_helper(self):
        self._complete_linux_reap()
        class CustomDict(dict):
            def get(self, *args, **kwargs):
                raise AssertionError("custom lifecycle method executed")
        calls = 0
        def clock():
            nonlocal calls
            calls += 1
            if calls == 3:
                custom = CustomDict(self.attempt.lifecycle)
                self.attempt.owned_pidfd.lifecycle = custom
                self.attempt._set("lifecycle", custom)
            return self.started + 200 + calls
        with mock.patch.object(LAB.time, "monotonic_ns",
                               side_effect=clock):
            with self.assertRaises(BaseException) as raised:
                self.attempt.observe_linux_status_tail_once()
        self.assertNotIn("custom lifecycle method executed", str(raised.exception))
        self.assertTrue(self.attempt.status_eof_observed)
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")

    def test_linux_status_final_clock_owned_child_key_is_preflighted(self):
        self._complete_linux_reap()
        calls = 0
        def clock():
            nonlocal calls
            calls += 1
            if calls == 3:
                self.attempt.owned_pidfd.child[1] = "collision"
            return self.started + 200 + calls
        with mock.patch.object(LAB.time, "monotonic_ns",
                               side_effect=clock):
            with self.assertRaises(BaseException):
                self.attempt.observe_linux_status_tail_once()
        self.assertTrue(self.attempt.status_eof_observed)
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")

    def test_linux_status_final_clock_custom_cap_owner_is_preflighted(self):
        self._complete_linux_reap()
        class CustomValue:
            def __eq__(self, other):
                raise AssertionError("custom cap owner comparison executed")
        calls = 0
        def clock():
            nonlocal calls
            calls += 1
            if calls == 3:
                self.attempt.monitor_capability.owner["pid"] = CustomValue()
            return self.started + 200 + calls
        with mock.patch.object(LAB.time, "monotonic_ns",
                               side_effect=clock):
            with self.assertRaises(BaseException) as raised:
                self.attempt.observe_linux_status_tail_once()
        self.assertNotIn("custom cap owner comparison executed",
                         str(raised.exception))
        self.assertTrue(self.attempt.status_eof_observed)
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")

    def test_linux_status_final_clock_custom_streams_preflights_get(self):
        self._complete_linux_reap()
        class CustomDict(dict):
            def get(self, *args, **kwargs):
                raise AssertionError("custom streams get executed")
        calls = 0
        def clock():
            nonlocal calls
            calls += 1
            if calls == 3:
                self.attempt.capture_handle.streams = CustomDict(
                    self.attempt.capture_handle.streams)
            return self.started + 200 + calls
        with mock.patch.object(LAB.time, "monotonic_ns",
                               side_effect=clock):
            with self.assertRaises(BaseException) as raised:
                self.attempt.observe_linux_status_tail_once()
        self.assertNotIn("custom streams get executed", str(raised.exception))
        self.assertTrue(self.attempt.status_eof_observed)
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")

    def test_linux_status_final_clock_descendant_completion_preflight(self):
        self._complete_linux_reap()
        class CustomDict(dict):
            def __eq__(self, other):
                raise AssertionError("custom descendant comparison executed")
        calls = 0
        def clock():
            nonlocal calls
            calls += 1
            if calls == 3:
                desc = self.attempt.settlement_handle.descendant_capability
                desc.primary_completion = CustomDict(desc.primary_completion)
            return self.started + 200 + calls
        with mock.patch.object(LAB.time, "monotonic_ns",
                               side_effect=clock):
            with self.assertRaises(BaseException) as raised:
                self.attempt.observe_linux_status_tail_once()
        self.assertNotIn("custom descendant comparison executed",
                         str(raised.exception))
        self.assertTrue(self.attempt.status_eof_observed)
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")

    def test_linux_abort_protocol_matches_exact_exit_and_canaries(self):
        calls = self._complete_linux_status()
        result = self.attempt.verify_linux_abort_protocol_once()
        self.assertEqual(result["classification"],
                         "LINUX_ABORT_PROTOCOL_EVIDENCE_MATCH")
        self.assertTrue(result["abort_protocol_verified"])
        self.assertTrue(result["exit_73_verified"])
        self.assertTrue(result["status_eof_verified"])
        self.assertFalse(result["runtime_authorized"])
        self.assertFalse(result["storage_authorized"])
        self.assertFalse(result["resources_closed"])
        self.assertFalse(result["descendants_qualified"])
        self.assertEqual(self.attempt.state,
                         "LINUX_ABORT_PROTOCOL_VERIFIED")
        self.assertEqual(calls.wait_options,
                         [os.WEXITED | os.WNOHANG | os.WNOWAIT,
                          os.WEXITED | os.WNOHANG])
        with self.assertRaises(BaseException):
            self.attempt.verify_linux_abort_protocol_once()

    def test_terminal_release_claim_freezes_order_without_close(self):
        calls = self._complete_linux_status()
        self.attempt.verify_linux_abort_protocol_once()
        before_owned = set(self.handle.owned)
        before_closed = list(calls.closed)
        with mock.patch.object(LAB.time, "monotonic_ns",
                               return_value=self.started + 300):
            claim = self.attempt.claim_abort_terminal_release_once()
        receipt = claim.receipt()
        self.assertEqual(receipt["classification"],
                         "ABORT_TERMINAL_RELEASE_CLAIMED_SOURCE_ONLY")
        self.assertEqual(receipt["release_order"], ["epoll", "status_parent",
            "stdout_parent", "stderr_parent", "pidfd"])
        self.assertEqual(receipt["close_syscalls"], 0)
        self.assertFalse(receipt["resources_closed"])
        self.assertFalse(receipt["descendants_qualified"])
        self.assertEqual(self.handle.owned, before_owned)
        self.assertEqual(calls.closed, before_closed)
        self.assertEqual(self.attempt.state,
                         "LINUX_ABORT_TERMINAL_RELEASE_CLAIMED")
        with self.assertRaises(BaseException):
            self.attempt.claim_abort_terminal_release_once()

    def test_shared_descendant_branch_blocks_release_first_claim(self):
        self.reset_with_shared_descendant()
        self._complete_linux_status()
        self.attempt.verify_linux_abort_protocol_once()
        descendant = self.attempt.settlement_handle.descendant_capability
        with mock.patch.object(LAB.time, "monotonic_ns",
                               return_value=self.started + 300):
            with self.assertRaises(LAB.Refusal):
                self.attempt.claim_abort_terminal_release_once()
        self.assertIsNone(self.attempt.terminal_release_claim)
        self.assertFalse(descendant.used)
        self.assertFalse(self.attempt.settlement_handle.probe_used)
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")

    def test_terminal_release_claim_refuses_protocol_mismatch(self):
        self._complete_linux_status(status=LAB.EXIT_GRANT_DATA)
        self.attempt.verify_linux_abort_protocol_once()
        with self.assertRaises(BaseException):
            self.attempt.claim_abort_terminal_release_once()
        self.assertIsNone(self.attempt.terminal_release_claim)

    def test_terminal_release_claim_refuses_resource_drift(self):
        self._complete_linux_status()
        self.attempt.verify_linux_abort_protocol_once()
        self.handle.owned.remove("status_parent")
        with self.assertRaises(BaseException):
            self.attempt.claim_abort_terminal_release_once()
        self.assertIsNone(self.attempt.terminal_release_claim)

    def test_terminal_release_claim_deadline_has_no_new_budget(self):
        self._complete_linux_status()
        self.attempt.verify_linux_abort_protocol_once()
        with mock.patch.object(LAB.time, "monotonic_ns",
                               return_value=self.attempt.cleanup_deadline_ns):
            with self.assertRaises(BaseException):
                self.attempt.claim_abort_terminal_release_once()
        self.assertIsNone(self.attempt.terminal_release_claim)

    def test_terminal_release_claim_revalidates_after_clock_callback(self):
        self._complete_linux_status()
        self.attempt.verify_linux_abort_protocol_once()
        def drift():
            self.handle.owned.remove("status_parent")
            return self.started + 300
        with mock.patch.object(LAB.time, "monotonic_ns", side_effect=drift):
            with self.assertRaises(BaseException):
                self.attempt.claim_abort_terminal_release_once()
        self.assertIsNone(self.attempt.terminal_release_claim)

    def test_terminal_release_nested_clock_claim_publishes_nothing(self):
        self._complete_linux_status()
        self.attempt.verify_linux_abort_protocol_once()
        nested = []
        def reenter():
            try:
                self.attempt.claim_abort_terminal_release_once()
            except BaseException as exc:
                nested.append(type(exc).__name__)
            return self.started + 300
        with mock.patch.object(LAB.time, "monotonic_ns", side_effect=reenter):
            with self.assertRaises(BaseException):
                self.attempt.claim_abort_terminal_release_once()
        self.assertTrue(nested)
        self.assertIsNone(self.attempt.terminal_release_claim)

    def test_terminal_release_claim_receipt_is_content_immutable(self):
        self._complete_linux_status()
        self.attempt.verify_linux_abort_protocol_once()
        with mock.patch.object(LAB.time, "monotonic_ns",
                               return_value=self.started + 300):
            claim = self.attempt.claim_abort_terminal_release_once()
        first = claim.receipt()
        first["resources"][1]["fd"] = 999999
        self.assertNotEqual(claim.receipt()["resources"][1]["fd"], 999999)
        for action in (lambda: copy.copy(claim),
                       lambda: copy.deepcopy(claim),
                       lambda: setattr(claim, "_receipt_bytes", b"{}")):
            with self.assertRaises(BaseException):
                action()

    def test_terminal_release_claim_refuses_post_match_canary_drift(self):
        self._complete_linux_status()
        self.attempt.verify_linux_abort_protocol_once()
        self.attempt.capture_handle.streams["stdout"].stored[:] = b"changed"
        with self.assertRaises(BaseException):
            self.attempt.claim_abort_terminal_release_once()
        self.assertIsNone(self.attempt.terminal_release_claim)

    def test_terminal_release_and_descendant_claim_are_exclusive(self):
        self._complete_linux_status()
        self.attempt.verify_linux_abort_protocol_once()
        settlement = self.attempt.settlement_handle
        with mock.patch.object(LAB.time, "monotonic_ns",
                               return_value=self.started + 300):
            self.attempt.claim_abort_terminal_release_once()
        with self.assertRaises(BaseException):
            CAPTURE.take_descendant_settlement_capability(
                settlement, self.attempt.capture_handle,
                self.attempt.capture_adapter)

    def test_descendant_claim_blocks_terminal_release(self):
        self._complete_linux_status()
        self.attempt.verify_linux_abort_protocol_once()
        settlement = self.attempt.settlement_handle
        CAPTURE.take_descendant_settlement_capability(
            settlement, self.attempt.capture_handle,
            self.attempt.capture_adapter)
        with self.assertRaises(BaseException):
            self.attempt.claim_abort_terminal_release_once()
        self.assertIsNone(self.attempt.terminal_release_claim)

    def test_terminal_release_refuses_same_object_closed_epoll(self):
        self._complete_linux_status()
        self.attempt.verify_linux_abort_protocol_once()
        def close_poller():
            self.attempt.capture_handle.poller.close()
            return self.started + 300
        with mock.patch.object(LAB.time, "monotonic_ns",
                               side_effect=close_poller):
            with self.assertRaises(BaseException):
                self.attempt.claim_abort_terminal_release_once()
        self.assertIsNone(self.attempt.terminal_release_claim)

    def test_terminal_release_policy_custom_scalar_has_no_callback(self):
        self._complete_linux_status()
        self.attempt.verify_linux_abort_protocol_once()
        class EvilStr(str):
            def __eq__(self, other):
                raise AssertionError("custom policy comparison executed")
        def mutate_policy():
            self.attempt._protocol_policy["stdout_hex"] = EvilStr(
                self.attempt._protocol_policy["stdout_hex"])
            return self.started + 300
        with mock.patch.object(LAB.time, "monotonic_ns",
                               side_effect=mutate_policy):
            with self.assertRaises(BaseException) as raised:
                self.attempt.claim_abort_terminal_release_once()
        self.assertNotIn("custom policy comparison executed",
                         str(raised.exception))
        self.assertIsNone(self.attempt.terminal_release_claim)

    def test_minted_terminal_claim_refuses_resource_drift(self):
        self._complete_linux_status()
        self.attempt.verify_linux_abort_protocol_once()
        with mock.patch.object(LAB.time, "monotonic_ns",
                               return_value=self.started + 300):
            claim = self.attempt.claim_abort_terminal_release_once()
        original_pidfd = self.attempt.owned_pidfd.pidfd
        self.attempt.owned_pidfd.pidfd = original_pidfd + 1000
        try:
            with self.assertRaises(BaseException):
                claim.receipt()
        finally:
            self.attempt.owned_pidfd.pidfd = original_pidfd
        self.handle.owned.remove("status_parent")
        try:
            with self.assertRaises(BaseException):
                claim.receipt()
        finally:
            self.handle.owned.add("status_parent")
        self.handle.poisoned = True
        try:
            with self.assertRaises(BaseException):
                claim.receipt()
        finally:
            self.handle.poisoned = False
        self.attempt.settlement_handle.probe_used = False
        with self.assertRaises(BaseException):
            claim.receipt()

    def test_minted_terminal_claim_requires_exact_pidfd_type(self):
        self._complete_linux_status()
        self.attempt.verify_linux_abort_protocol_once()
        with mock.patch.object(LAB.time, "monotonic_ns",
                               return_value=self.started + 300):
            claim = self.attempt.claim_abort_terminal_release_once()
        original = self.attempt.owned_pidfd.pidfd
        class EvilInt(int):
            def __eq__(self, other):
                raise AssertionError("custom pidfd comparison executed")
        for replacement in (float(original), True, EvilInt(original)):
            self.attempt.owned_pidfd.pidfd = replacement
            try:
                with self.assertRaises(BaseException) as raised:
                    claim.receipt()
                self.assertNotIn("custom pidfd comparison executed",
                                 str(raised.exception))
            finally:
                self.attempt.owned_pidfd.pidfd = original

    def test_minted_terminal_claim_revalidates_lifecycle_backlink(self):
        self._complete_linux_status()
        self.attempt.verify_linux_abort_protocol_once()
        with mock.patch.object(LAB.time, "monotonic_ns",
                               return_value=self.started + 300):
            claim = self.attempt.claim_abort_terminal_release_once()
        lifecycle = self.attempt.owned_pidfd.lifecycle
        original = lifecycle["origin"]
        lifecycle["origin"] = object()
        try:
            with self.assertRaises(BaseException):
                claim.receipt()
        finally:
            lifecycle["origin"] = original

    def test_minted_claim_preflights_descendant_before_used_getter(self):
        self._complete_linux_status()
        self.attempt.verify_linux_abort_protocol_once()
        with mock.patch.object(LAB.time, "monotonic_ns",
                               return_value=self.started + 300):
            claim = self.attempt.claim_abort_terminal_release_once()
        settlement = self.attempt.settlement_handle
        original = settlement.descendant_capability
        calls = []
        class CustomDescendant:
            @property
            def used(self):
                calls.append("used")
                return True
        settlement.descendant_capability = CustomDescendant()
        try:
            with self.assertRaises(BaseException):
                claim.receipt()
            self.assertEqual(calls, [])
        finally:
            settlement.descendant_capability = original

    def test_linux_abort_protocol_exit_74_is_inert_mismatch(self):
        self._complete_linux_status(status=LAB.EXIT_GRANT_DATA)
        result = self.attempt.verify_linux_abort_protocol_once()
        self.assertEqual(result["classification"],
                         "LINUX_ABORT_PROTOCOL_EVIDENCE_MISMATCH")
        self.assertIn("TERMINAL_NOT_EXIT_73", result["reasons"])
        self.assertIn("REAP_NOT_EXIT_73", result["reasons"])
        self.assertFalse(result["abort_protocol_verified"])
        self.assertFalse(result["runtime_authorized"])
        self.assertEqual(self.attempt.state,
                         "LINUX_ABORT_PROTOCOL_MISMATCH")

    def test_linux_abort_protocol_wrong_stdout_is_inert_mismatch(self):
        self._complete_linux_status(stdout=b"wrong stdout")
        result = self.attempt.verify_linux_abort_protocol_once()
        self.assertEqual(result["classification"],
                         "LINUX_ABORT_PROTOCOL_EVIDENCE_MISMATCH")
        self.assertEqual(result["reasons"], ["STDOUT_CANARY_MISMATCH"])
        self.assertFalse(result["abort_protocol_verified"])
        self.assertFalse(result["storage_authorized"])

    def test_linux_abort_protocol_swapped_canaries_are_mismatch(self):
        self._complete_linux_status(stdout=LAB.STDERR_CANARY,
                                    stderr=LAB.STDOUT_CANARY)
        result = self.attempt.verify_linux_abort_protocol_once()
        self.assertEqual(result["reasons"], ["STDOUT_CANARY_MISMATCH",
                                              "STDERR_CANARY_MISMATCH"])
        self.assertFalse(result["abort_protocol_verified"])

    def test_linux_abort_protocol_empty_canary_is_mismatch(self):
        self._complete_linux_status(stderr=b"")
        result = self.attempt.verify_linux_abort_protocol_once()
        self.assertEqual(result["reasons"], ["STDERR_CANARY_MISMATCH"])
        self.assertFalse(result["abort_protocol_verified"])

    def test_linux_abort_protocol_ignores_mutated_module_globals(self):
        self._complete_linux_status(status=LAB.EXIT_GRANT_DATA,
                                    stdout=b"wrong stdout",
                                    stderr=b"wrong stderr")
        original = (LAB.EXIT_ABORT_EOF, LAB.STDOUT_CANARY,
                    LAB.STDERR_CANARY)
        try:
            LAB.EXIT_ABORT_EOF = LAB.EXIT_GRANT_DATA
            LAB.STDOUT_CANARY = b"wrong stdout"
            LAB.STDERR_CANARY = b"wrong stderr"
            result = self.attempt.verify_linux_abort_protocol_once()
        finally:
            (LAB.EXIT_ABORT_EOF, LAB.STDOUT_CANARY,
             LAB.STDERR_CANARY) = original
        self.assertEqual(result["classification"],
                         "LINUX_ABORT_PROTOCOL_EVIDENCE_MISMATCH")
        self.assertIn("TERMINAL_NOT_EXIT_73", result["reasons"])
        self.assertIn("STDOUT_CANARY_MISMATCH", result["reasons"])
        self.assertIn("STDERR_CANARY_MISMATCH", result["reasons"])
        self.assertFalse(result["abort_protocol_verified"])

    def test_linux_abort_protocol_current_stream_drift_refuses(self):
        self._complete_linux_status()
        self.attempt.capture_handle.streams["stdout"].eof = False
        with self.assertRaises(BaseException):
            self.attempt.verify_linux_abort_protocol_once()
        self.assertIsNone(self.attempt.protocol_evidence)
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")

    def test_linux_abort_protocol_custom_origin_preflights_index(self):
        self._complete_linux_status()
        class CustomDict(dict):
            def __getitem__(self, key):
                raise AssertionError("custom origin index executed")
        self.attempt.capture_handle.origin.identities = CustomDict(
            self.attempt.capture_handle.origin.identities)
        with self.assertRaises(BaseException) as raised:
            self.attempt.verify_linux_abort_protocol_once()
        self.assertNotIn("custom origin index executed", str(raised.exception))
        self.assertIsNone(self.attempt.protocol_evidence)
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")

    def test_linux_status_extra_writer_times_out_without_close_or_retry(self):
        self._bind_linux_unarmed_for_capture()
        duplicate = os.dup(
            self.handle.frozen_bundle["status_child"]["fd"])
        try:
            os.write(self.handle.frozen_bundle["stdout_child"]["fd"],
                     LAB.STDOUT_CANARY)
            os.write(self.handle.frozen_bundle["stderr_child"]["fd"],
                     LAB.STDERR_CANARY)
            with mock.patch.object(OWNED, "LinuxSyscalls",
                                   return_value=self.owned_calls), \
                    mock.patch.object(LAB.time, "monotonic_ns",
                                      return_value=self.started + 5):
                self.attempt.prepare_linux_capture_unarmed_once()
            self.owned_calls.waits = [types.SimpleNamespace(
                si_pid=self.attempt.returned_pid, si_code=os.CLD_EXITED,
                si_status=LAB.EXIT_ABORT_EOF)]
            ticks = iter(range(self.started + 6, self.started + 100))
            with mock.patch.object(LAB.time, "monotonic_ns",
                                   side_effect=lambda: next(ticks)):
                self.attempt.close_linux_grant_zero_write_once()
                self.attempt.monitor_linux_abort_unreaped_once()
            self.owned_calls.waits = [types.SimpleNamespace(
                si_pid=self.attempt.returned_pid, si_code=os.CLD_EXITED,
                si_status=LAB.EXIT_ABORT_EOF)]
            reap_ticks = iter(range(self.started + 100, self.started + 200))
            with mock.patch.object(LAB.time, "monotonic_ns",
                                   side_effect=lambda: next(reap_ticks)):
                self.attempt.reap_linux_completion_once()
            deadline = self.attempt.cleanup_deadline_ns
            fake_poller = mock.Mock()
            fake_poller.poll.return_value = []
            with mock.patch.object(LAB.select, "poll",
                                   return_value=fake_poller), \
                    mock.patch.object(LAB.time, "monotonic_ns",
                                      side_effect=[deadline - 3,
                                                   deadline - 2,
                                                   deadline]):
                with self.assertRaises(BaseException):
                    self.attempt.observe_linux_status_tail_once()
            self.assertFalse(self.attempt.status_eof_observed)
            self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")
            self.assertEqual(self.handle.attempted_writes, set())
            self.assertNotIn("status_parent", self.handle.attempted_closes)
        finally:
            os.close(duplicate)

    def test_linux_status_hup_event_never_substitutes_for_read_eof(self):
        self._bind_linux_unarmed_for_capture()
        duplicate = os.dup(
            self.handle.frozen_bundle["status_child"]["fd"])
        try:
            os.write(self.handle.frozen_bundle["stdout_child"]["fd"],
                     LAB.STDOUT_CANARY)
            os.write(self.handle.frozen_bundle["stderr_child"]["fd"],
                     LAB.STDERR_CANARY)
            with mock.patch.object(OWNED, "LinuxSyscalls",
                                   return_value=self.owned_calls), \
                    mock.patch.object(LAB.time, "monotonic_ns",
                                      return_value=self.started + 5):
                self.attempt.prepare_linux_capture_unarmed_once()
            self.owned_calls.waits = [types.SimpleNamespace(
                si_pid=self.attempt.returned_pid, si_code=os.CLD_EXITED,
                si_status=LAB.EXIT_ABORT_EOF)]
            ticks = iter(range(self.started + 6, self.started + 100))
            with mock.patch.object(LAB.time, "monotonic_ns",
                                   side_effect=lambda: next(ticks)):
                self.attempt.close_linux_grant_zero_write_once()
                self.attempt.monitor_linux_abort_unreaped_once()
            self.owned_calls.waits = [types.SimpleNamespace(
                si_pid=self.attempt.returned_pid, si_code=os.CLD_EXITED,
                si_status=LAB.EXIT_ABORT_EOF)]
            reap_ticks = iter(range(self.started + 100, self.started + 200))
            with mock.patch.object(LAB.time, "monotonic_ns",
                                   side_effect=lambda: next(reap_ticks)):
                self.attempt.reap_linux_completion_once()
            deadline = self.attempt.cleanup_deadline_ns
            status_fd = self.handle.frozen_bundle["status_parent"]["fd"]
            fake_poller = mock.Mock()
            fake_poller.poll.return_value = [(status_fd, select.POLLHUP)]
            with mock.patch.object(LAB.select, "poll",
                                   return_value=fake_poller), \
                    mock.patch.object(LAB.time, "monotonic_ns",
                                      side_effect=[deadline - 4,
                                                   deadline - 3,
                                                   deadline - 2,
                                                   deadline - 1,
                                                   deadline]):
                with self.assertRaises(BaseException):
                    self.attempt.observe_linux_status_tail_once()
            self.assertGreaterEqual(fake_poller.poll.call_count, 1)
            self.assertFalse(self.attempt.status_eof_observed)
            self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")
        finally:
            os.close(duplicate)

    def test_linux_status_read_callback_cannot_rewrite_settlement(self):
        self._complete_linux_reap()
        original = self.handle.calls.read
        def read_then_rewrite(fd, maximum):
            result = original(fd, maximum)
            self.attempt.settlement_receipt["finished_ns"] += 1
            return result
        ticks = iter(range(self.started + 200, self.started + 220))
        with mock.patch.object(self.handle.calls, "read",
                               side_effect=read_then_rewrite), \
                mock.patch.object(LAB.time, "monotonic_ns",
                                  side_effect=lambda: next(ticks)):
            with self.assertRaises(BaseException):
                self.attempt.observe_linux_status_tail_once()
        self.assertTrue(self.attempt.status_eof_observed)
        self.assertEqual(self.attempt.status_read_observation["kind"], "EOF")
        self.assertIsNone(self.attempt.status_eof_receipt)
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")

    def test_linux_grant_close_after_effect_error_is_unknown_no_retry(self):
        self._prepare_linux_capture_for_grant_close()
        original = self.handle.calls.close
        def close_then_error(fd):
            original(fd)
            raise OSError("simulated close acknowledgement loss")
        with mock.patch.object(self.handle.calls, "close",
                               side_effect=close_then_error), \
                mock.patch.object(LAB.time, "monotonic_ns",
                                  return_value=self.started + 6):
            with self.assertRaises(OSError):
                self.attempt.close_linux_grant_zero_write_once()
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")
        self.assertNotIn("grant_parent", self.handle.owned)
        self.assertEqual(self.handle.attempted_writes, set())
        self.assertEqual(self.handle.attempted_closes,
                         set(LAB.PARENT_CLOSE_ROLES) | {"grant_parent"})
        with self.assertRaises(BaseException):
            self.attempt.close_linux_grant_zero_write_once()

    def test_linux_ready_after_deadline_is_unknown_without_capture(self):
        self._bind_linux_unarmed_for_capture()
        with mock.patch.object(LAB.time, "monotonic_ns",
                               side_effect=[self.started + 5,
                                            self.deadline]):
            with self.assertRaises(BaseException):
                self.attempt.prepare_linux_capture_unarmed_once()
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")
        self.assertIsNone(self.attempt.capture_handle)
        if all(type(role) is str for role in self.handle.owned):
            self.assertIn("grant_parent", self.handle.owned)
        self.assertEqual(self.handle.attempted_writes, set())

    def test_linux_capture_attach_after_deadline_preserves_handles(self):
        self._bind_linux_unarmed_for_capture()
        with mock.patch.object(LAB.time, "monotonic_ns",
                               side_effect=[self.started + 5,
                                            self.started + 6,
                                            self.deadline]):
            with self.assertRaises(BaseException):
                self.attempt.prepare_linux_capture_unarmed_once()
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")
        self.assertIsNotNone(self.attempt.capture_handle)
        self.assertIsNotNone(self.attempt.capture_adapter)
        if all(type(role) is str for role in self.handle.owned):
            self.assertIn("grant_parent", self.handle.owned)
        self.assertEqual(self.handle.attempted_writes, set())

    def test_linux_capture_callback_mutation_refuses_with_original_pidfd(self):
        self._bind_linux_unarmed_for_capture()
        original = CAPTURE.attach_child_adapter
        def mutate_after_attach(capture, adapter, calls):
            result = original(capture, adapter, calls)
            self.attempt.owned_pidfd.pidfd = 99
            return result
        with mock.patch.object(CAPTURE, "attach_child_adapter",
                               side_effect=mutate_after_attach), \
                mock.patch.object(LAB.time, "monotonic_ns",
                                  return_value=self.started + 5):
            with self.assertRaises(BaseException):
                self.attempt.prepare_linux_capture_unarmed_once()
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")
        self.assertEqual(self.attempt._owned_pidfd_fd, 55)
        self.assertIsNotNone(self.attempt.capture_handle)
        self.assertIsNotNone(self.attempt.capture_adapter)
        self.assertIn("grant_parent", self.handle.owned)

    def _assert_post_attach_mutation_refuses(self, mutate):
        self._bind_linux_unarmed_for_capture()
        original = CAPTURE.attach_child_adapter
        def mutate_after_attach(capture, adapter, calls):
            result = original(capture, adapter, calls)
            mutate(capture, adapter)
            return result
        with mock.patch.object(CAPTURE, "attach_child_adapter",
                               side_effect=mutate_after_attach), \
                mock.patch.object(LAB.time, "monotonic_ns",
                                  return_value=self.started + 5):
            with self.assertRaises(BaseException):
                self.attempt.prepare_linux_capture_unarmed_once()
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")
        self.assertIsNotNone(self.attempt.capture_handle)
        self.assertIsNotNone(self.attempt.capture_adapter)
        if all(type(role) is str for role in self.handle.owned):
            self.assertIn("grant_parent", self.handle.owned)
        self.assertEqual(self.handle.attempted_writes, set())

    def test_linux_capture_origin_child_mutation_refuses(self):
        self._assert_post_attach_mutation_refuses(
            lambda capture, adapter:
            capture.origin.child.__setitem__("pid",
                                              capture.origin.child["pid"] + 1))

    def test_linux_capture_authority_mutation_refuses(self):
        self._assert_post_attach_mutation_refuses(
            lambda capture, adapter:
            setattr(adapter, "capture_authority", object()))

    def test_linux_capture_poller_replacement_refuses(self):
        self._assert_post_attach_mutation_refuses(
            lambda capture, adapter: setattr(capture, "poller", object()))
        self.attempt.capture_handle.poller = self.attempt._capture_poller

    def test_linux_capture_nonfresh_stream_refuses(self):
        self._assert_post_attach_mutation_refuses(
            lambda capture, adapter:
            capture.streams["stdout"].stored.extend(b"unexpected"))

    def test_linux_capture_custom_adapter_state_refuses_without_callback(self):
        class EvilState:
            calls = 0
            def __eq__(self, other):
                type(self).calls += 1
                return True
        self._assert_post_attach_mutation_refuses(
            lambda capture, adapter: setattr(adapter, "state", EvilState()))
        self.assertEqual(EvilState.calls, 0)

    def test_linux_capture_custom_role_refuses_without_equality_callback(self):
        class EvilRole:
            calls = 0
            def __hash__(self):
                return hash("grant_parent")
            def __eq__(self, other):
                type(self).calls += 1
                return True
        def mutate(capture, adapter):
            self.handle.owned.remove("grant_parent")
            self.handle.owned.add(EvilRole())
        self._assert_post_attach_mutation_refuses(mutate)
        self.assertEqual(EvilRole.calls, 0)

    def test_linux_last_clock_fd_graph_mutation_refuses(self):
        self._bind_linux_unarmed_for_capture()
        calls = []
        def clock():
            calls.append(True)
            if len(calls) == 3:
                self.handle.frozen_bundle["stdout_parent"]["inode"] += 1
            return self.started + 5
        with mock.patch.object(LAB.time, "monotonic_ns", side_effect=clock):
            with self.assertRaises(BaseException):
                self.attempt.prepare_linux_capture_unarmed_once()
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")
        self.assertIn("grant_parent", self.handle.owned)
        self.assertEqual(self.handle.attempted_writes, set())

    def test_linux_parent_launcher_must_match_persisted_intent_before_fork(self):
        self.attempt = LAB.prepare_abort_attempt(
            self.persisted, "4" * 32, self.started + 3)
        pid = self.owner["pid"] + 1000
        launcher = {"kind": "UNARMED_LAUNCHER_NOT_PAYLOAD",
                    "exe_dev": 999, "exe_inode": 998,
                    "cmdline_sha256": "f" * 64}
        calls = FakeOwnedLinuxCalls(self.owner, launcher, pid)
        with mock.patch.object(FDS.LinuxFDCalls, "owner_identity",
                               return_value=copy.deepcopy(self.owner)), \
                mock.patch.object(OWNED, "LinuxSyscalls", return_value=calls), \
                mock.patch.object(LINUX, "fork_abort_child_once") as fork:
            with self.assertRaises(LAB.Refusal):
                self.attempt.fork_and_bind_linux_pidfd_once()
        fork.assert_not_called()
        self.assertEqual(self.attempt.state, "UNKNOWN")

    def test_linux_post_bind_deadline_preserves_owned_pidfd_for_settlement(self):
        self.attempt = LAB.prepare_abort_attempt(
            self.persisted, "4" * 32, self.started + 3)
        pid = self.owner["pid"] + 1000
        identity = self.persisted.bound.request["launcher_identity"]
        launcher = {"kind": "UNARMED_LAUNCHER_NOT_PAYLOAD",
                    "exe_dev": identity["interpreter"]["dev"],
                    "exe_inode": identity["interpreter"]["inode"],
                    "cmdline_sha256":
                    identity["inherited"]["proc_cmdline_sha256"]}
        calls = FakeOwnedLinuxCalls(self.owner, launcher, pid)
        with mock.patch.object(FDS.LinuxFDCalls, "owner_identity",
                               return_value=copy.deepcopy(self.owner)), \
                mock.patch.object(OWNED, "LinuxSyscalls", return_value=calls), \
                mock.patch.object(LINUX, "fork_abort_child_once",
                                  return_value=pid), \
                mock.patch.object(LAB.time, "monotonic_ns",
                                  side_effect=[self.started + 4,
                                               self.deadline]):
            with self.assertRaises(LAB.Refusal):
                self.attempt.fork_and_bind_linux_pidfd_once()
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")
        self.assertIsNotNone(self.attempt.owned_pidfd)
        self.assertEqual(self.attempt.owned_pidfd.pidfd, 55)

    def test_linux_post_bind_clock_pidfd_mutation_preserves_original_evidence(self):
        self.attempt = LAB.prepare_abort_attempt(
            self.persisted, "4" * 32, self.started + 3)
        pid = self.owner["pid"] + 1000
        identity = self.persisted.bound.request["launcher_identity"]
        launcher = {"kind": "UNARMED_LAUNCHER_NOT_PAYLOAD",
                    "exe_dev": identity["interpreter"]["dev"],
                    "exe_inode": identity["interpreter"]["inode"],
                    "cmdline_sha256":
                    identity["inherited"]["proc_cmdline_sha256"]}
        calls = FakeOwnedLinuxCalls(self.owner, launcher, pid)
        ticks = []
        def clock():
            ticks.append(True)
            if len(ticks) == 2:
                self.attempt.owned_pidfd.pidfd = 99
            return self.started + 4 + len(ticks)
        with mock.patch.object(FDS.LinuxFDCalls, "owner_identity",
                               return_value=copy.deepcopy(self.owner)), \
                mock.patch.object(OWNED, "LinuxSyscalls", return_value=calls), \
                mock.patch.object(LINUX, "fork_abort_child_once",
                                  return_value=pid), \
                mock.patch.object(LAB.time, "monotonic_ns", side_effect=clock):
            with self.assertRaises(LAB.Refusal):
                self.attempt.fork_and_bind_linux_pidfd_once()
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")
        self.assertEqual(self.attempt._owned_pidfd_fd, 55)
        self.assertEqual(self.attempt.owned_pidfd.pidfd, 99)

    def test_linux_post_bind_clock_origin_pid_drift_is_refused(self):
        self.attempt = LAB.prepare_abort_attempt(
            self.persisted, "4" * 32, self.started + 3)
        pid = self.owner["pid"] + 1000
        identity = self.persisted.bound.request["launcher_identity"]
        launcher = {"kind": "UNARMED_LAUNCHER_NOT_PAYLOAD",
                    "exe_dev": identity["interpreter"]["dev"],
                    "exe_inode": identity["interpreter"]["inode"],
                    "cmdline_sha256":
                    identity["inherited"]["proc_cmdline_sha256"]}
        calls = FakeOwnedLinuxCalls(self.owner, launcher, pid)
        ticks = []
        def clock():
            ticks.append(True)
            if len(ticks) == 2:
                self.attempt.lifecycle["origin"].pid = float(pid)
            return self.started + 4 + len(ticks)
        with mock.patch.object(FDS.LinuxFDCalls, "owner_identity",
                               return_value=copy.deepcopy(self.owner)), \
                mock.patch.object(OWNED, "LinuxSyscalls", return_value=calls), \
                mock.patch.object(LINUX, "fork_abort_child_once",
                                  return_value=pid), \
                mock.patch.object(LAB.time, "monotonic_ns", side_effect=clock):
            with self.assertRaises(LAB.Refusal):
                self.attempt.fork_and_bind_linux_pidfd_once()
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")
        self.assertEqual(self.attempt._owned_pidfd_fd, 55)

    def test_linux_post_bind_custom_fd_state_is_rejected_without_callback(self):
        self.attempt = LAB.prepare_abort_attempt(
            self.persisted, "4" * 32, self.started + 3)
        pid = self.owner["pid"] + 1000
        identity = self.persisted.bound.request["launcher_identity"]
        launcher = {"kind": "UNARMED_LAUNCHER_NOT_PAYLOAD",
                    "exe_dev": identity["interpreter"]["dev"],
                    "exe_inode": identity["interpreter"]["inode"],
                    "cmdline_sha256":
                    identity["inherited"]["proc_cmdline_sha256"]}
        calls = FakeOwnedLinuxCalls(self.owner, launcher, pid)
        custom_calls = []
        class EvilStr(str):
            def __eq__(self, other):
                custom_calls.append(other)
                self_attempt.owned_pidfd.pidfd = 99
                return super().__eq__(other)
            __hash__ = str.__hash__
        self_attempt = self.attempt
        ticks = []
        def clock():
            ticks.append(True)
            if len(ticks) == 2:
                self.handle.state = EvilStr("FD_GRAPH_READY")
            return self.started + 4 + len(ticks)
        with mock.patch.object(FDS.LinuxFDCalls, "owner_identity",
                               return_value=copy.deepcopy(self.owner)), \
                mock.patch.object(OWNED, "LinuxSyscalls", return_value=calls), \
                mock.patch.object(LINUX, "fork_abort_child_once",
                                  return_value=pid), \
                mock.patch.object(LAB.time, "monotonic_ns", side_effect=clock):
            with self.assertRaises(LAB.Refusal):
                self.attempt.fork_and_bind_linux_pidfd_once()
        self.assertEqual(custom_calls, [])
        self.assertEqual(self.attempt.state, "UNKNOWN_SURVIVOR")
        self.assertEqual(self.attempt._owned_pidfd_fd, 55)

    def test_source_epoll_release_confirms_only_epoll_model(self):
        claim, calls = self._claim_terminal_release()
        before_closed = list(calls.closed)
        backend = SourceEpollReleaseBackend(self.started + 301)
        result = claim.release_epoll_model_once(backend)
        self.assertEqual(result["classification"],
                         "MODEL_EPOLL_CLOSE_CONFIRMED")
        self.assertTrue(result["epoll_close_attempted"])
        self.assertTrue(result["epoll_close_confirmed"])
        self.assertEqual(result["pipe_close_attempts"], 0)
        self.assertEqual(result["pidfd_close_attempts"], 0)
        self.assertFalse(result["resources_closed"])
        self.assertEqual(calls.closed, before_closed)
        self.assertEqual(backend.observations, 1)
        self.assertEqual(backend.closes, 1)
        with self.assertRaises(BaseException):
            claim.release_epoll_model_once(backend)
        self.assertEqual(backend.closes, 1)

    def test_source_epoll_release_expired_observation_dispatches_zero_close(self):
        claim, _ = self._claim_terminal_release()
        deadline = claim.receipt()["deadline_ns"]
        backend = SourceEpollReleaseBackend(deadline)
        with self.assertRaises(BaseException):
            claim.release_epoll_model_once(backend)
        result = claim.epoll_outcome()
        self.assertFalse(result["epoll_close_attempted"])
        self.assertFalse(result["epoll_close_confirmed"])
        self.assertEqual(backend.closes, 0)

    def test_source_epoll_close_after_effect_error_is_unknown_no_retry(self):
        claim, calls = self._claim_terminal_release()
        before_closed = list(calls.closed)
        def ambiguous():
            raise OSError(errno.EINTR, "close outcome unknown")
        backend = SourceEpollReleaseBackend(
            self.started + 301, close_hook=ambiguous)
        with self.assertRaises(OSError):
            claim.release_epoll_model_once(backend)
        result = claim.epoll_outcome()
        self.assertTrue(result["epoll_close_attempted"])
        self.assertFalse(result["epoll_close_confirmed"])
        self.assertEqual(result["classification"],
                         "MODEL_EPOLL_RELEASE_UNKNOWN")
        self.assertEqual(backend.closes, 1)
        self.assertEqual(calls.closed, before_closed)
        with self.assertRaises(BaseException):
            claim.release_epoll_model_once(backend)
        self.assertEqual(backend.closes, 1)

    def test_source_epoll_malformed_after_effect_result_is_unknown(self):
        claim, _ = self._claim_terminal_release()
        backend = SourceEpollReleaseBackend(
            self.started + 301, result={"closed": 1, "finished_ns": 2})
        with self.assertRaises(BaseException):
            claim.release_epoll_model_once(backend)
        result = claim.epoll_outcome()
        self.assertTrue(result["epoll_close_attempted"])
        self.assertFalse(result["epoll_close_confirmed"])
        self.assertEqual(backend.closes, 1)

    def test_source_epoll_swallowed_reentry_blocks_outer_close(self):
        claim, _ = self._claim_terminal_release()
        backend = None
        def reenter():
            try:
                claim.release_epoll_model_once(backend)
            except BaseException:
                pass
        backend = SourceEpollReleaseBackend(
            self.started + 301, observe_hook=reenter)
        with self.assertRaises(BaseException):
            claim.release_epoll_model_once(backend)
        self.assertEqual(backend.closes, 0)

    def test_source_epoll_close_callback_reentry_is_unknown(self):
        claim, _ = self._claim_terminal_release()
        backend = None
        def reenter():
            try:
                claim.release_epoll_model_once(backend)
            except BaseException:
                pass
        backend = SourceEpollReleaseBackend(
            self.started + 301, close_hook=reenter)
        with self.assertRaises(BaseException):
            claim.release_epoll_model_once(backend)
        result = claim.epoll_outcome()
        self.assertTrue(result["epoll_close_attempted"])
        self.assertFalse(result["epoll_close_confirmed"])
        self.assertEqual(result["classification"],
                         "MODEL_EPOLL_RELEASE_UNKNOWN")
        self.assertEqual(backend.closes, 1)

    def test_source_epoll_close_callback_resource_drift_is_unknown(self):
        claim, _ = self._claim_terminal_release()
        def drift():
            self.handle.owned.remove("status_parent")
        backend = SourceEpollReleaseBackend(
            self.started + 301, close_hook=drift)
        with self.assertRaises(BaseException):
            claim.release_epoll_model_once(backend)
        result = claim.epoll_outcome()
        self.assertTrue(result["epoll_close_attempted"])
        self.assertFalse(result["epoll_close_confirmed"])

    def test_source_epoll_observe_callback_class_swap_dispatches_zero_close(self):
        claim, _ = self._claim_terminal_release()
        backend = None
        def swap_class():
            backend.__class__ = SwappedSourceEpollReleaseBackend
        backend = SourceEpollReleaseBackend(
            self.started + 301, observe_hook=swap_class)
        with self.assertRaises(BaseException):
            claim.release_epoll_model_once(backend)
        result = claim.epoll_outcome()
        self.assertFalse(result["epoll_close_attempted"])
        self.assertFalse(result["epoll_close_confirmed"])
        self.assertEqual(backend.closes, 0)

    def test_source_epoll_close_callback_class_swap_is_unknown(self):
        claim, _ = self._claim_terminal_release()
        backend = None
        def swap_class():
            backend.__class__ = SwappedSourceEpollReleaseBackend
        backend = SourceEpollReleaseBackend(
            self.started + 301, close_hook=swap_class)
        with self.assertRaises(BaseException):
            claim.release_epoll_model_once(backend)
        result = claim.epoll_outcome()
        self.assertTrue(result["epoll_close_attempted"])
        self.assertFalse(result["epoll_close_confirmed"])
        self.assertEqual(result["classification"],
                         "MODEL_EPOLL_RELEASE_UNKNOWN")
        self.assertEqual(backend.closes, 1)

    def test_source_epoll_observation_rejects_numeric_aliases(self):
        for key, value in (("same_object", 1), ("closed", 0),
                           ("fd", 7.0)):
            with self.subTest(key=key):
                if key != "same_object":
                    self.tearDown(); self.setUp()
                claim, _ = self._claim_terminal_release()
                observation = {"same_object": True, "closed": False,
                    "fd": claim._resource_scalar[0],
                    "observed_ns": self.started + 301}
                observation[key] = value
                backend = SourceEpollReleaseBackend(
                    self.started + 301, observation=observation)
                with self.assertRaises(BaseException):
                    claim.release_epoll_model_once(backend)
                self.assertEqual(backend.closes, 0)

    def test_source_epoll_invalid_custom_result_retains_unknown(self):
        claim, _ = self._claim_terminal_release()
        backend = SourceEpollReleaseBackend(
            self.started + 301,
            result={"closed": True, "finished_ns": object()})
        with self.assertRaises(BaseException):
            claim.release_epoll_model_once(backend)
        result = claim.epoll_outcome()
        self.assertTrue(result["epoll_close_attempted"])
        self.assertFalse(result["epoll_close_confirmed"])
        self.assertIsNone(result["raw_result"])
        self.assertFalse(claim._epoll_active)

    def test_source_epoll_huge_builtin_result_retains_unknown(self):
        claim, _ = self._claim_terminal_release()
        backend = SourceEpollReleaseBackend(
            self.started + 301,
            result={"closed": True, "finished_ns": 1,
                    "junk": 10 ** 5000})
        with self.assertRaises(BaseException):
            claim.release_epoll_model_once(backend)
        result = claim.epoll_outcome()
        self.assertTrue(result["epoll_close_attempted"])
        self.assertFalse(result["epoll_close_confirmed"])
        self.assertIsNone(result["raw_result"])
        self.assertFalse(claim._epoll_active)
        with self.assertRaises(BaseException):
            claim.release_epoll_model_once(backend)
        self.assertEqual(backend.closes, 1)

    def test_source_epoll_close_uses_frozen_observation_time(self):
        claim, _ = self._claim_terminal_release()
        observation = {"same_object": True, "closed": False,
            "fd": claim._resource_scalar[0],
            "observed_ns": self.started + 301}
        def mutate_observation():
            observation["observed_ns"] = self.started
        backend = SourceEpollReleaseBackend(
            self.started + 301, finished_ns=self.started + 300,
            observation=observation, close_hook=mutate_observation)
        with self.assertRaises(BaseException):
            claim.release_epoll_model_once(backend)
        result = claim.epoll_outcome()
        self.assertTrue(result["epoll_close_attempted"])
        self.assertFalse(result["epoll_close_confirmed"])

    def test_source_epoll_late_confirmed_is_not_full_cleanup(self):
        claim, _ = self._claim_terminal_release()
        deadline = claim.receipt()["deadline_ns"]
        backend = SourceEpollReleaseBackend(
            deadline - 1, finished_ns=deadline)
        result = claim.release_epoll_model_once(backend)
        self.assertEqual(result["classification"],
                         "MODEL_EPOLL_CLOSE_CONFIRMED_LATE")
        self.assertTrue(result["epoll_close_confirmed"])
        self.assertFalse(result["resources_closed"])

    def test_source_status_release_confirms_only_status_model(self):
        claim, calls, _ = self._claim_with_epoll_release()
        before_closed = list(calls.closed)
        before_owned = set(self.handle.owned)
        backend = SourceStatusReleaseBackend(self.started + 303)
        result = claim.release_status_model_once(backend)
        self.assertEqual(result["classification"],
                         "MODEL_STATUS_CLOSE_CONFIRMED")
        self.assertTrue(result["status_close_attempted"])
        self.assertTrue(result["status_close_confirmed"])
        self.assertEqual(result["stdout_close_attempts"], 0)
        self.assertEqual(result["stderr_close_attempts"], 0)
        self.assertEqual(result["pidfd_close_attempts"], 0)
        self.assertFalse(result["resources_closed"])
        self.assertEqual(calls.closed, before_closed)
        self.assertEqual(self.handle.owned, before_owned)
        with self.assertRaises(BaseException):
            claim.release_status_model_once(backend)
        self.assertEqual(backend.closes, 1)

    def test_source_status_before_epoll_dispatches_zero_callbacks(self):
        claim, _, = self._claim_terminal_release()
        backend = SourceStatusReleaseBackend(self.started + 303)
        with self.assertRaises(BaseException):
            claim.release_status_model_once(backend)
        self.assertEqual(backend.observations, 0)
        self.assertEqual(backend.closes, 0)
        self.assertFalse(claim._status_attempted)

    def test_source_status_after_unknown_epoll_dispatches_zero_callbacks(self):
        claim, _ = self._claim_terminal_release()
        def ambiguous():
            raise OSError(errno.EINTR, "epoll close outcome unknown")
        epoll = SourceEpollReleaseBackend(
            self.started + 301, close_hook=ambiguous)
        with self.assertRaises(BaseException):
            claim.release_epoll_model_once(epoll)
        backend = SourceStatusReleaseBackend(self.started + 303)
        with self.assertRaises(BaseException):
            claim.release_status_model_once(backend)
        self.assertEqual(backend.observations, 0)
        self.assertEqual(backend.closes, 0)

    def test_source_status_after_late_epoll_dispatches_zero_callbacks(self):
        claim, _ = self._claim_terminal_release()
        deadline = claim.receipt()["deadline_ns"]
        epoll = SourceEpollReleaseBackend(
            deadline - 1, finished_ns=deadline)
        claim.release_epoll_model_once(epoll)
        backend = SourceStatusReleaseBackend(deadline - 1)
        with self.assertRaises(BaseException):
            claim.release_status_model_once(backend)
        self.assertEqual(backend.observations, 0)
        self.assertEqual(backend.closes, 0)

    def test_source_status_rejects_identity_drift_before_close(self):
        claim, _, _ = self._claim_with_epoll_release()
        identity = self.handle._authority_record("status_parent")
        identity["inode"] += 1
        observation = {"role": "status_parent", "fd": identity["fd"],
            "identity": identity, "identity_matches": True,
            "closed": False, "observed_ns": self.started + 303}
        backend = SourceStatusReleaseBackend(
            self.started + 303, observation=observation)
        with self.assertRaises(BaseException):
            claim.release_status_model_once(backend)
        self.assertEqual(backend.closes, 0)

    def test_source_status_swallowed_nested_status_blocks_close(self):
        claim, _, _ = self._claim_with_epoll_release()
        backend = None
        def reenter():
            try:
                claim.release_status_model_once(backend)
            except BaseException:
                pass
        backend = SourceStatusReleaseBackend(
            self.started + 303, observe_hook=reenter)
        with self.assertRaises(BaseException):
            claim.release_status_model_once(backend)
        self.assertEqual(backend.closes, 0)

    def test_source_status_close_epoll_replay_is_unknown(self):
        claim, _, epoll = self._claim_with_epoll_release()
        def replay_epoll():
            try:
                claim.release_epoll_model_once(epoll)
            except BaseException:
                pass
        backend = SourceStatusReleaseBackend(
            self.started + 303, close_hook=replay_epoll)
        with self.assertRaises(BaseException):
            claim.release_status_model_once(backend)
        result = claim.status_outcome()
        self.assertTrue(result["status_close_attempted"])
        self.assertFalse(result["status_close_confirmed"])
        self.assertEqual(result["classification"],
                         "MODEL_STATUS_RELEASE_UNKNOWN")
        self.assertEqual(backend.closes, 1)

    def test_source_status_close_class_swap_is_unknown(self):
        claim, _, _ = self._claim_with_epoll_release()
        backend = None
        def swap_class():
            backend.__class__ = SwappedSourceStatusReleaseBackend
        backend = SourceStatusReleaseBackend(
            self.started + 303, close_hook=swap_class)
        with self.assertRaises(BaseException):
            claim.release_status_model_once(backend)
        result = claim.status_outcome()
        self.assertTrue(result["status_close_attempted"])
        self.assertFalse(result["status_close_confirmed"])

    def test_source_status_close_resource_drift_is_unknown(self):
        claim, _, _ = self._claim_with_epoll_release()
        def drift():
            self.handle.frozen_bundle["stdout_parent"]["inode"] += 1
        backend = SourceStatusReleaseBackend(
            self.started + 303, close_hook=drift)
        with self.assertRaises(BaseException):
            claim.release_status_model_once(backend)
        result = claim.status_outcome()
        self.assertTrue(result["status_close_attempted"])
        self.assertFalse(result["status_close_confirmed"])

    def test_source_status_ambiguous_close_is_unknown_no_retry(self):
        claim, _, _ = self._claim_with_epoll_release()
        def ambiguous():
            raise OSError(errno.EINTR, "status close outcome unknown")
        backend = SourceStatusReleaseBackend(
            self.started + 303, close_hook=ambiguous)
        with self.assertRaises(OSError):
            claim.release_status_model_once(backend)
        result = claim.status_outcome()
        self.assertTrue(result["status_close_attempted"])
        self.assertFalse(result["status_close_confirmed"])
        with self.assertRaises(BaseException):
            claim.release_status_model_once(backend)
        self.assertEqual(backend.closes, 1)

    def test_source_status_huge_builtin_result_retains_unknown(self):
        claim, _, _ = self._claim_with_epoll_release()
        backend = SourceStatusReleaseBackend(
            self.started + 303,
            result={"closed": True, "finished_ns": self.started + 304,
                    "junk": 10 ** 5000})
        with self.assertRaises(BaseException):
            claim.release_status_model_once(backend)
        result = claim.status_outcome()
        self.assertTrue(result["status_close_attempted"])
        self.assertFalse(result["status_close_confirmed"])
        self.assertIsNone(result["raw_result"])
        self.assertFalse(claim._status_active)

    def test_source_status_late_confirmed_cannot_advance(self):
        claim, _, _ = self._claim_with_epoll_release()
        deadline = claim.receipt()["deadline_ns"]
        backend = SourceStatusReleaseBackend(
            deadline - 1, finished_ns=deadline)
        result = claim.release_status_model_once(backend)
        self.assertEqual(result["classification"],
                         "MODEL_STATUS_CLOSE_CONFIRMED_LATE")
        self.assertTrue(result["status_close_confirmed"])
        self.assertFalse(result["resources_closed"])

    def test_source_stdout_release_confirms_only_stdout_model(self):
        claim, calls, _, _ = self._claim_with_status_release()
        before_closed = list(calls.closed)
        before_owned = set(self.handle.owned)
        backend = SourceStdoutReleaseBackend(self.started + 305)
        result = claim.release_stdout_model_once(backend)
        self.assertEqual(result["classification"],
                         "MODEL_STDOUT_CLOSE_CONFIRMED")
        self.assertTrue(result["stdout_close_attempted"])
        self.assertTrue(result["stdout_close_confirmed"])
        self.assertEqual(result["stderr_close_attempts"], 0)
        self.assertEqual(result["pidfd_close_attempts"], 0)
        self.assertFalse(result["resources_closed"])
        self.assertEqual(calls.closed, before_closed)
        self.assertEqual(self.handle.owned, before_owned)
        with self.assertRaises(BaseException):
            claim.release_stdout_model_once(backend)
        self.assertEqual(backend.closes, 1)

    def test_source_stdout_before_status_dispatches_zero_callbacks(self):
        claim, _, _ = self._claim_with_epoll_release()
        backend = SourceStdoutReleaseBackend(self.started + 305)
        with self.assertRaises(BaseException):
            claim.release_stdout_model_once(backend)
        self.assertEqual(backend.observations, 0)
        self.assertEqual(backend.closes, 0)
        self.assertFalse(claim._stdout_attempted)

    def test_source_stdout_after_late_status_dispatches_zero_callbacks(self):
        claim, _, _ = self._claim_with_epoll_release()
        deadline = claim.receipt()["deadline_ns"]
        status = SourceStatusReleaseBackend(
            deadline - 1, finished_ns=deadline)
        claim.release_status_model_once(status)
        backend = SourceStdoutReleaseBackend(deadline - 1)
        with self.assertRaises(BaseException):
            claim.release_stdout_model_once(backend)
        self.assertEqual(backend.observations, 0)
        self.assertEqual(backend.closes, 0)

    def test_source_stdout_canary_drift_dispatches_zero_callbacks(self):
        claim, _, _, _ = self._claim_with_status_release()
        self.attempt.capture_handle.streams["stdout"].stored[:] = b"wrong"
        backend = SourceStdoutReleaseBackend(self.started + 305)
        with self.assertRaises(BaseException):
            claim.release_stdout_model_once(backend)
        self.assertEqual(backend.observations, 0)
        self.assertEqual(backend.closes, 0)

    def test_source_stdout_identity_drift_dispatches_zero_callbacks(self):
        claim, _, _, _ = self._claim_with_status_release()
        self.attempt.capture_handle.origin.identities[
            "stdout"]["inode"] += 1
        backend = SourceStdoutReleaseBackend(self.started + 305)
        with self.assertRaises(BaseException):
            claim.release_stdout_model_once(backend)
        self.assertEqual(backend.observations, 0)
        self.assertEqual(backend.closes, 0)

    def test_source_stdout_custom_origin_container_invokes_no_getter(self):
        claim, _, _, _ = self._claim_with_status_release()
        calls = []
        class EvilDict(dict):
            def __getitem__(self, key):
                calls.append(key)
                raise AssertionError("custom origin getter invoked")
        self.attempt.capture_handle.origin.identities = EvilDict(
            self.attempt.capture_handle.origin.identities)
        backend = SourceStdoutReleaseBackend(self.started + 305)
        with self.assertRaises(BaseException) as raised:
            claim.release_stdout_model_once(backend)
        self.assertNotIn("custom origin getter invoked", str(raised.exception))
        self.assertEqual(calls, [])
        self.assertEqual(backend.observations, 0)
        self.assertEqual(backend.closes, 0)

    def test_source_stdout_close_custom_policy_key_invokes_no_equality(self):
        claim, _, _, _ = self._claim_with_status_release()
        equalities = []
        class EvilStr(str):
            def __eq__(self, other):
                equalities.append(other)
                self_attempt.owned_pidfd.pidfd = 999
                return super().__eq__(other)
            __hash__ = str.__hash__
        self_attempt = self.attempt
        evil = dict(self.attempt._protocol_policy)
        del evil["stdout_hex"]
        evil[EvilStr("stdout_hex")] = LAB.STDOUT_CANARY.hex()
        equalities.clear()
        def drift():
            self.attempt._protocol_policy = evil
        backend = SourceStdoutReleaseBackend(
            self.started + 305, close_hook=drift)
        with self.assertRaises(BaseException):
            claim.release_stdout_model_once(backend)
        result = claim.stdout_outcome()
        self.assertEqual(equalities, [])
        self.assertTrue(result["stdout_close_attempted"])
        self.assertFalse(result["stdout_close_confirmed"])

    def test_source_stdout_builtin_policy_drift_dispatches_zero_callbacks(self):
        claim, _, _, _ = self._claim_with_status_release()
        self.attempt._protocol_policy["stdout_hex"] = (
            self.attempt._protocol_policy["stdout_hex"].upper())
        backend = SourceStdoutReleaseBackend(self.started + 305)
        with self.assertRaises(BaseException):
            claim.release_stdout_model_once(backend)
        self.assertEqual(backend.observations, 0)
        self.assertEqual(backend.closes, 0)

    def test_source_stdout_swallowed_nested_stdout_blocks_close(self):
        claim, _, _, _ = self._claim_with_status_release()
        backend = None
        def reenter():
            try:
                claim.release_stdout_model_once(backend)
            except BaseException:
                pass
        backend = SourceStdoutReleaseBackend(
            self.started + 305, observe_hook=reenter)
        with self.assertRaises(BaseException):
            claim.release_stdout_model_once(backend)
        self.assertEqual(backend.closes, 0)

    def test_source_stdout_close_status_replay_is_unknown(self):
        claim, _, _, status = self._claim_with_status_release()
        def replay_status():
            try:
                claim.release_status_model_once(status)
            except BaseException:
                pass
        backend = SourceStdoutReleaseBackend(
            self.started + 305, close_hook=replay_status)
        with self.assertRaises(BaseException):
            claim.release_stdout_model_once(backend)
        result = claim.stdout_outcome()
        self.assertTrue(result["stdout_close_attempted"])
        self.assertFalse(result["stdout_close_confirmed"])
        self.assertEqual(result["classification"],
                         "MODEL_STDOUT_RELEASE_UNKNOWN")

    def test_source_stdout_close_class_swap_is_unknown(self):
        claim, _, _, _ = self._claim_with_status_release()
        backend = None
        def swap_class():
            backend.__class__ = SwappedSourceStdoutReleaseBackend
        backend = SourceStdoutReleaseBackend(
            self.started + 305, close_hook=swap_class)
        with self.assertRaises(BaseException):
            claim.release_stdout_model_once(backend)
        self.assertFalse(claim.stdout_outcome()["stdout_close_confirmed"])

    def test_source_stdout_close_stderr_drift_is_unknown(self):
        claim, _, _, _ = self._claim_with_status_release()
        def drift():
            self.handle.frozen_bundle["stderr_parent"]["inode"] += 1
        backend = SourceStdoutReleaseBackend(
            self.started + 305, close_hook=drift)
        with self.assertRaises(BaseException):
            claim.release_stdout_model_once(backend)
        result = claim.stdout_outcome()
        self.assertTrue(result["stdout_close_attempted"])
        self.assertFalse(result["stdout_close_confirmed"])

    def test_source_stdout_ambiguous_close_is_unknown_no_retry(self):
        claim, _, _, _ = self._claim_with_status_release()
        def ambiguous():
            raise OSError(errno.EINTR, "stdout close outcome unknown")
        backend = SourceStdoutReleaseBackend(
            self.started + 305, close_hook=ambiguous)
        with self.assertRaises(OSError):
            claim.release_stdout_model_once(backend)
        result = claim.stdout_outcome()
        self.assertTrue(result["stdout_close_attempted"])
        self.assertFalse(result["stdout_close_confirmed"])
        with self.assertRaises(BaseException):
            claim.release_stdout_model_once(backend)
        self.assertEqual(backend.closes, 1)

    def test_source_stdout_huge_builtin_result_retains_unknown(self):
        claim, _, _, _ = self._claim_with_status_release()
        backend = SourceStdoutReleaseBackend(
            self.started + 305,
            result={"closed": True, "finished_ns": self.started + 306,
                    "junk": 10 ** 5000})
        with self.assertRaises(BaseException):
            claim.release_stdout_model_once(backend)
        result = claim.stdout_outcome()
        self.assertTrue(result["stdout_close_attempted"])
        self.assertFalse(result["stdout_close_confirmed"])
        self.assertIsNone(result["raw_result"])

    def test_source_stdout_late_confirmed_cannot_advance(self):
        claim, _, _, _ = self._claim_with_status_release()
        deadline = claim.receipt()["deadline_ns"]
        backend = SourceStdoutReleaseBackend(
            deadline - 1, finished_ns=deadline)
        result = claim.release_stdout_model_once(backend)
        self.assertEqual(result["classification"],
                         "MODEL_STDOUT_CLOSE_CONFIRMED_LATE")
        self.assertTrue(result["stdout_close_confirmed"])
        self.assertFalse(result["resources_closed"])

    def test_source_stderr_release_confirms_only_stderr_model(self):
        claim, calls, _, _, _ = self._claim_with_stdout_release()
        before_closed = list(calls.closed)
        before_owned = set(self.handle.owned)
        pidfd = self.attempt.owned_pidfd.pidfd
        backend = SourceStderrReleaseBackend(self.started + 307)
        result = claim.release_stderr_model_once(backend)
        self.assertEqual(result["classification"],
                         "MODEL_STDERR_CLOSE_CONFIRMED")
        self.assertTrue(result["stderr_close_attempted"])
        self.assertTrue(result["stderr_close_confirmed"])
        self.assertEqual(result["pidfd_close_attempts"], 0)
        self.assertFalse(result["resources_closed"])
        self.assertEqual(calls.closed, before_closed)
        self.assertEqual(self.handle.owned, before_owned)
        self.assertEqual(self.attempt.owned_pidfd.pidfd, pidfd)
        with self.assertRaises(BaseException):
            claim.release_stderr_model_once(backend)
        self.assertEqual(backend.closes, 1)

    def test_source_stderr_before_stdout_dispatches_zero_callbacks(self):
        claim, _, _, _ = self._claim_with_status_release()
        backend = SourceStderrReleaseBackend(self.started + 307)
        with self.assertRaises(BaseException):
            claim.release_stderr_model_once(backend)
        self.assertEqual(backend.observations, 0)
        self.assertEqual(backend.closes, 0)

    def test_source_stderr_after_late_stdout_dispatches_zero_callbacks(self):
        claim, _, _, _ = self._claim_with_status_release()
        deadline = claim.receipt()["deadline_ns"]
        stdout = SourceStdoutReleaseBackend(
            deadline - 1, finished_ns=deadline)
        claim.release_stdout_model_once(stdout)
        backend = SourceStderrReleaseBackend(deadline - 1)
        with self.assertRaises(BaseException):
            claim.release_stderr_model_once(backend)
        self.assertEqual(backend.observations, 0)
        self.assertEqual(backend.closes, 0)

    def test_source_stderr_canary_drift_dispatches_zero_callbacks(self):
        claim, _, _, _, _ = self._claim_with_stdout_release()
        self.attempt.capture_handle.streams["stderr"].stored[:] = b"wrong"
        backend = SourceStderrReleaseBackend(self.started + 307)
        with self.assertRaises(BaseException):
            claim.release_stderr_model_once(backend)
        self.assertEqual(backend.observations, 0)
        self.assertEqual(backend.closes, 0)

    def test_source_stderr_swapped_origin_dispatches_zero_callbacks(self):
        claim, _, _, _, _ = self._claim_with_stdout_release()
        identities = self.attempt.capture_handle.origin.identities
        identities["stderr"], identities["stdout"] = (
            identities["stdout"], identities["stderr"])
        backend = SourceStderrReleaseBackend(self.started + 307)
        with self.assertRaises(BaseException):
            claim.release_stderr_model_once(backend)
        self.assertEqual(backend.observations, 0)
        self.assertEqual(backend.closes, 0)

    def test_source_stderr_swallowed_nested_stderr_blocks_close(self):
        claim, _, _, _, _ = self._claim_with_stdout_release()
        backend = None
        def reenter():
            try:
                claim.release_stderr_model_once(backend)
            except BaseException:
                pass
        backend = SourceStderrReleaseBackend(
            self.started + 307, observe_hook=reenter)
        with self.assertRaises(BaseException):
            claim.release_stderr_model_once(backend)
        self.assertEqual(backend.closes, 0)

    def test_source_stderr_close_stdout_replay_is_unknown(self):
        claim, _, _, _, stdout = self._claim_with_stdout_release()
        def replay_stdout():
            try:
                claim.release_stdout_model_once(stdout)
            except BaseException:
                pass
        backend = SourceStderrReleaseBackend(
            self.started + 307, close_hook=replay_stdout)
        with self.assertRaises(BaseException):
            claim.release_stderr_model_once(backend)
        result = claim.stderr_outcome()
        self.assertTrue(result["stderr_close_attempted"])
        self.assertFalse(result["stderr_close_confirmed"])

    def test_source_stderr_close_stdout_drift_is_unknown(self):
        claim, _, _, _, _ = self._claim_with_stdout_release()
        def drift():
            self.attempt.capture_handle.streams[
                "stdout"].stored[:] = b"wrong"
        backend = SourceStderrReleaseBackend(
            self.started + 307, close_hook=drift)
        with self.assertRaises(BaseException):
            claim.release_stderr_model_once(backend)
        self.assertFalse(claim.stderr_outcome()["stderr_close_confirmed"])

    def test_source_stderr_close_pidfd_drift_is_unknown(self):
        claim, _, _, _, _ = self._claim_with_stdout_release()
        def drift():
            self.attempt.owned_pidfd.pidfd = 999
        backend = SourceStderrReleaseBackend(
            self.started + 307, close_hook=drift)
        with self.assertRaises(BaseException):
            claim.release_stderr_model_once(backend)
        self.assertFalse(claim.stderr_outcome()["stderr_close_confirmed"])

    def test_source_stderr_close_class_swap_is_unknown(self):
        claim, _, _, _, _ = self._claim_with_stdout_release()
        backend = None
        def swap_class():
            backend.__class__ = SwappedSourceStderrReleaseBackend
        backend = SourceStderrReleaseBackend(
            self.started + 307, close_hook=swap_class)
        with self.assertRaises(BaseException):
            claim.release_stderr_model_once(backend)
        self.assertFalse(claim.stderr_outcome()["stderr_close_confirmed"])

    def test_source_stderr_ambiguous_close_is_unknown_no_retry(self):
        claim, _, _, _, _ = self._claim_with_stdout_release()
        def ambiguous():
            raise OSError(errno.EINTR, "stderr close outcome unknown")
        backend = SourceStderrReleaseBackend(
            self.started + 307, close_hook=ambiguous)
        with self.assertRaises(OSError):
            claim.release_stderr_model_once(backend)
        result = claim.stderr_outcome()
        self.assertTrue(result["stderr_close_attempted"])
        self.assertFalse(result["stderr_close_confirmed"])
        with self.assertRaises(BaseException):
            claim.release_stderr_model_once(backend)
        self.assertEqual(backend.closes, 1)

    def test_source_stderr_huge_builtin_result_retains_unknown(self):
        claim, _, _, _, _ = self._claim_with_stdout_release()
        backend = SourceStderrReleaseBackend(
            self.started + 307,
            result={"closed": True, "finished_ns": self.started + 308,
                    "junk": 10 ** 5000})
        with self.assertRaises(BaseException):
            claim.release_stderr_model_once(backend)
        result = claim.stderr_outcome()
        self.assertTrue(result["stderr_close_attempted"])
        self.assertFalse(result["stderr_close_confirmed"])
        self.assertIsNone(result["raw_result"])

    def test_source_stderr_late_confirmed_cannot_advance(self):
        claim, _, _, _, _ = self._claim_with_stdout_release()
        deadline = claim.receipt()["deadline_ns"]
        backend = SourceStderrReleaseBackend(
            deadline - 1, finished_ns=deadline)
        result = claim.release_stderr_model_once(backend)
        self.assertEqual(result["classification"],
                         "MODEL_STDERR_CLOSE_CONFIRMED_LATE")
        self.assertTrue(result["stderr_close_confirmed"])
        self.assertFalse(result["resources_closed"])

    def test_source_pidfd_anchor_model_is_assumed_and_creates_nothing(self):
        claim, calls, _, _, _, _ = self._claim_with_stderr_release()
        before_closed = list(calls.closed)
        before_owned = set(self.handle.owned)
        primary = self.attempt.owned_pidfd.pidfd
        backend = SourcePidfdAnchorBackend(self.started + 309)
        capability = claim.acquire_pidfd_anchor_model_once(backend)
        receipt = capability.receipt()
        self.assertEqual(receipt["classification"],
                         "MODEL_PIDFD_ANCHOR_ACQUIRED_ASSUMED")
        self.assertTrue(receipt["model_assumed"])
        self.assertFalse(receipt["kernel_ofd_proven"])
        self.assertFalse(receipt["real_descriptor_created"])
        self.assertEqual(receipt["primary_fd"], primary)
        self.assertEqual(calls.closed, before_closed)
        self.assertEqual(self.handle.owned, before_owned)
        self.assertEqual(self.attempt.owned_pidfd.pidfd, primary)
        outcome = claim.anchor_outcome()
        self.assertEqual(outcome["pidfd_close_attempts"], 0)
        self.assertFalse(outcome["resources_closed"])
        with self.assertRaises(BaseException):
            claim.acquire_pidfd_anchor_model_once(backend)
        self.assertEqual(backend.calls, 1)

    def test_source_pidfd_anchor_before_stderr_dispatches_zero_callbacks(self):
        claim, _, _, _, _ = self._claim_with_stdout_release()
        backend = SourcePidfdAnchorBackend(self.started + 309)
        with self.assertRaises(BaseException):
            claim.acquire_pidfd_anchor_model_once(backend)
        self.assertEqual(backend.calls, 0)

    def test_source_pidfd_anchor_after_late_stderr_dispatches_zero_callbacks(self):
        claim, _, _, _, _ = self._claim_with_stdout_release()
        deadline = claim.receipt()["deadline_ns"]
        stderr = SourceStderrReleaseBackend(
            deadline - 1, finished_ns=deadline)
        claim.release_stderr_model_once(stderr)
        backend = SourcePidfdAnchorBackend(deadline - 1)
        with self.assertRaises(BaseException):
            claim.acquire_pidfd_anchor_model_once(backend)
        self.assertEqual(backend.calls, 0)

    def test_source_pidfd_anchor_rejects_existing_resource_alias(self):
        claim, _, _, _, _, _ = self._claim_with_stderr_release()
        status_fd = dict(claim._resource_scalar[1])["status_parent"]
        backend = SourcePidfdAnchorBackend(
            self.started + 309, anchor_fd=status_fd)
        with self.assertRaises(BaseException):
            claim.acquire_pidfd_anchor_model_once(backend)
        outcome = claim.anchor_outcome()
        self.assertFalse(outcome["anchor_acquisition_confirmed"])
        self.assertEqual(outcome["pidfd_close_attempts"], 0)

    def test_source_pidfd_anchor_swallowed_reentry_is_unknown(self):
        claim, _, _, _, _, _ = self._claim_with_stderr_release()
        backend = None
        def reenter():
            try:
                claim.acquire_pidfd_anchor_model_once(backend)
            except BaseException:
                pass
        backend = SourcePidfdAnchorBackend(
            self.started + 309, hook=reenter)
        with self.assertRaises(BaseException):
            claim.acquire_pidfd_anchor_model_once(backend)
        outcome = claim.anchor_outcome()
        self.assertFalse(outcome["anchor_acquisition_confirmed"])
        self.assertEqual(backend.calls, 1)

    def test_source_pidfd_anchor_close_stage_replay_is_unknown(self):
        claim, _, _, _, _, stderr = self._claim_with_stderr_release()
        def replay_stderr():
            try:
                claim.release_stderr_model_once(stderr)
            except BaseException:
                pass
        backend = SourcePidfdAnchorBackend(
            self.started + 309, hook=replay_stderr)
        with self.assertRaises(BaseException):
            claim.acquire_pidfd_anchor_model_once(backend)
        self.assertFalse(
            claim.anchor_outcome()["anchor_acquisition_confirmed"])

    def test_source_pidfd_anchor_pidfd_drift_is_unknown(self):
        claim, _, _, _, _, _ = self._claim_with_stderr_release()
        def drift():
            self.attempt.owned_pidfd.pidfd = 999
        backend = SourcePidfdAnchorBackend(
            self.started + 309, hook=drift)
        with self.assertRaises(BaseException):
            claim.acquire_pidfd_anchor_model_once(backend)
        self.assertFalse(
            claim.anchor_outcome()["anchor_acquisition_confirmed"])

    def test_source_pidfd_anchor_class_swap_is_unknown(self):
        claim, _, _, _, _, _ = self._claim_with_stderr_release()
        backend = None
        def swap_class():
            backend.__class__ = SwappedSourcePidfdAnchorBackend
        backend = SourcePidfdAnchorBackend(
            self.started + 309, hook=swap_class)
        with self.assertRaises(BaseException):
            claim.acquire_pidfd_anchor_model_once(backend)
        self.assertFalse(
            claim.anchor_outcome()["anchor_acquisition_confirmed"])

    def test_source_pidfd_anchor_huge_result_retains_unknown(self):
        claim, _, _, _, _, _ = self._claim_with_stderr_release()
        primary = self.attempt.owned_pidfd.pidfd
        backend = SourcePidfdAnchorBackend(
            self.started + 309,
            result={"primary_fd": primary, "anchor_fd": 900,
                    "acquired_ns": self.started + 309,
                    "model_assumed": True, "junk": 10 ** 5000})
        with self.assertRaises(BaseException):
            claim.acquire_pidfd_anchor_model_once(backend)
        outcome = claim.anchor_outcome()
        self.assertFalse(outcome["anchor_acquisition_confirmed"])
        self.assertIsNone(outcome["raw_result"])

    def test_source_pidfd_anchor_deadline_is_refused(self):
        claim, _, _, _, _, _ = self._claim_with_stderr_release()
        deadline = claim.receipt()["deadline_ns"]
        backend = SourcePidfdAnchorBackend(deadline)
        with self.assertRaises(BaseException):
            claim.acquire_pidfd_anchor_model_once(backend)
        self.assertFalse(
            claim.anchor_outcome()["anchor_acquisition_confirmed"])

    def test_source_pidfd_anchor_capability_is_opaque_and_detached(self):
        claim, _, _, _, _, _ = self._claim_with_stderr_release()
        backend = SourcePidfdAnchorBackend(self.started + 309)
        capability = claim.acquire_pidfd_anchor_model_once(backend)
        first = capability.receipt()
        first["anchor_fd"] = 1
        self.assertEqual(capability.receipt()["anchor_fd"], 900)
        with self.assertRaises(BaseException):
            copy.copy(capability)
        with self.assertRaises(BaseException):
            capability._consumed = True

    def test_source_pidfd_release_confirms_model_only(self):
        values = self._claim_with_pidfd_anchor()
        claim, calls, capability = values[0], values[1], values[-1]
        before_closed = list(calls.closed)
        before_owned = set(self.handle.owned)
        primary = self.attempt.owned_pidfd.pidfd
        backend = SourcePidfdReleaseBackend(self.started + 310)
        result = claim.release_pidfd_model_once(capability, backend)
        self.assertEqual(result["classification"],
                         "MODEL_PIDFD_CLOSE_CONFIRMED")
        self.assertTrue(result["compare_attempted"])
        self.assertTrue(result["pidfd_close_attempted"])
        self.assertTrue(result["pidfd_close_confirmed"])
        self.assertTrue(result["capability_consumed"])
        self.assertFalse(result["primary_model_owned"])
        self.assertTrue(result["anchor_model_owned"])
        self.assertFalse(result["kernel_ofd_proven"])
        self.assertFalse(result["real_pidfd_closed"])
        self.assertFalse(result["resources_closed"])
        self.assertFalse(result["descendants_qualified"])
        self.assertEqual(calls.closed, before_closed)
        self.assertEqual(self.handle.owned, before_owned)
        self.assertEqual(self.attempt.owned_pidfd.pidfd, primary)
        with self.assertRaises(BaseException):
            claim.release_pidfd_model_once(capability, backend)
        self.assertEqual(backend.closes, 1)

    def test_source_pidfd_release_rejects_detached_receipt(self):
        values = self._claim_with_pidfd_anchor()
        claim, capability = values[0], values[-1]
        backend = SourcePidfdReleaseBackend(self.started + 310)
        with self.assertRaises(BaseException):
            claim.release_pidfd_model_once(capability.receipt(), backend)
        self.assertEqual(backend.compares, 0)
        self.assertEqual(backend.closes, 0)

    def test_source_pidfd_compare_false_quarantines_without_close(self):
        values = self._claim_with_pidfd_anchor()
        claim, capability = values[0], values[-1]
        backend = SourcePidfdReleaseBackend(
            self.started + 310, same=False)
        result = claim.release_pidfd_model_once(capability, backend)
        self.assertEqual(result["classification"],
                         "MODEL_PIDFD_IDENTITY_QUARANTINED")
        self.assertTrue(result["capability_consumed"])
        self.assertTrue(result["primary_model_owned"])
        self.assertEqual(backend.compares, 1)
        self.assertEqual(backend.closes, 0)
        with self.assertRaises(BaseException):
            claim.release_pidfd_model_once(capability, backend)
        self.assertEqual(backend.compares, 1)

    def test_source_pidfd_compare_error_consumes_without_close(self):
        values = self._claim_with_pidfd_anchor()
        claim, capability = values[0], values[-1]
        def fail_compare():
            raise OSError(errno.EIO, "comparison unavailable")
        backend = SourcePidfdReleaseBackend(
            self.started + 310, compare_hook=fail_compare)
        with self.assertRaises(OSError):
            claim.release_pidfd_model_once(capability, backend)
        result = claim.pidfd_outcome()
        self.assertTrue(result["compare_attempted"])
        self.assertTrue(result["capability_consumed"])
        self.assertFalse(result["pidfd_close_attempted"])
        self.assertEqual(backend.closes, 0)

    def test_source_pidfd_compare_numeric_alias_is_unknown_zero_close(self):
        values = self._claim_with_pidfd_anchor()
        claim, capability = values[0], values[-1]
        receipt = capability.receipt()
        comparison = {"primary_fd": receipt["primary_fd"],
            "anchor_fd": receipt["anchor_fd"],
            "provenance_sha256": receipt["provenance_sha256"],
            "same": 1, "checked_ns": self.started + 310,
            "model_assumed": True}
        backend = SourcePidfdReleaseBackend(
            self.started + 310, comparison=comparison)
        with self.assertRaises(BaseException):
            claim.release_pidfd_model_once(capability, backend)
        self.assertEqual(backend.closes, 0)
        self.assertTrue(claim.pidfd_outcome()["capability_consumed"])

    def test_source_pidfd_compare_swallowed_reentry_blocks_close(self):
        values = self._claim_with_pidfd_anchor()
        claim, capability = values[0], values[-1]
        backend = None
        def reenter():
            try:
                claim.release_pidfd_model_once(capability, backend)
            except BaseException:
                pass
        backend = SourcePidfdReleaseBackend(
            self.started + 310, compare_hook=reenter)
        with self.assertRaises(BaseException):
            claim.release_pidfd_model_once(capability, backend)
        self.assertEqual(backend.closes, 0)

    def test_source_pidfd_compare_pidfd_drift_blocks_close(self):
        values = self._claim_with_pidfd_anchor()
        claim, capability = values[0], values[-1]
        def drift():
            self.attempt.owned_pidfd.pidfd = 999
        backend = SourcePidfdReleaseBackend(
            self.started + 310, compare_hook=drift)
        with self.assertRaises(BaseException):
            claim.release_pidfd_model_once(capability, backend)
        self.assertEqual(backend.closes, 0)

    def test_source_pidfd_close_after_effect_is_unknown_no_retry(self):
        values = self._claim_with_pidfd_anchor()
        claim, capability = values[0], values[-1]
        def ambiguous():
            raise OSError(errno.EINTR, "pidfd close outcome unknown")
        backend = SourcePidfdReleaseBackend(
            self.started + 310, close_hook=ambiguous)
        with self.assertRaises(OSError):
            claim.release_pidfd_model_once(capability, backend)
        result = claim.pidfd_outcome()
        self.assertTrue(result["pidfd_close_attempted"])
        self.assertFalse(result["pidfd_close_confirmed"])
        self.assertFalse(result["primary_model_owned"])
        self.assertFalse(result["real_pidfd_closed"])
        with self.assertRaises(BaseException):
            claim.release_pidfd_model_once(capability, backend)
        self.assertEqual(backend.closes, 1)

    def test_source_pidfd_close_cannot_regress_frozen_compare_time(self):
        values = self._claim_with_pidfd_anchor()
        claim, capability = values[0], values[-1]
        receipt = capability.receipt()
        comparison = {"primary_fd": receipt["primary_fd"],
            "anchor_fd": receipt["anchor_fd"],
            "provenance_sha256": receipt["provenance_sha256"],
            "same": True, "checked_ns": self.started + 310,
            "model_assumed": True}
        def regress():
            comparison["checked_ns"] = 0
        close_result = {"primary_fd": receipt["primary_fd"],
            "anchor_fd": receipt["anchor_fd"],
            "provenance_sha256": receipt["provenance_sha256"],
            "closed": True, "finished_ns": self.started + 309}
        backend = SourcePidfdReleaseBackend(
            self.started + 310, comparison=comparison,
            close_hook=regress, close_result=close_result)
        with self.assertRaises(BaseException):
            claim.release_pidfd_model_once(capability, backend)
        self.assertFalse(claim.pidfd_outcome()["pidfd_close_confirmed"])

    def test_source_pidfd_close_ignores_custom_mutated_compare_time(self):
        values = self._claim_with_pidfd_anchor()
        claim, capability = values[0], values[-1]
        receipt = capability.receipt()
        comparison = {"primary_fd": receipt["primary_fd"],
            "anchor_fd": receipt["anchor_fd"],
            "provenance_sha256": receipt["provenance_sha256"],
            "same": True, "checked_ns": self.started + 310,
            "model_assumed": True}
        custom_calls = []
        class EvilInt(int):
            def __le__(self, other):
                custom_calls.append(other)
                raise AssertionError("custom compare time invoked")
        def mutate():
            comparison["checked_ns"] = EvilInt(0)
        backend = SourcePidfdReleaseBackend(
            self.started + 310, comparison=comparison, close_hook=mutate)
        with self.assertRaises(BaseException):
            claim.release_pidfd_model_once(capability, backend)
        self.assertEqual(custom_calls, [])
        result = claim.pidfd_outcome()
        self.assertTrue(result["pidfd_close_attempted"])
        self.assertFalse(result["pidfd_close_confirmed"])
        with self.assertRaises(BaseException):
            claim.release_pidfd_model_once(capability, backend)
        self.assertEqual(backend.closes, 1)

    def test_source_pidfd_close_class_swap_is_unknown(self):
        values = self._claim_with_pidfd_anchor()
        claim, capability = values[0], values[-1]
        backend = None
        def swap_class():
            backend.__class__ = SwappedSourcePidfdReleaseBackend
        backend = SourcePidfdReleaseBackend(
            self.started + 310, close_hook=swap_class)
        with self.assertRaises(BaseException):
            claim.release_pidfd_model_once(capability, backend)
        self.assertFalse(claim.pidfd_outcome()["pidfd_close_confirmed"])

    def test_source_pidfd_close_huge_result_retains_unknown(self):
        values = self._claim_with_pidfd_anchor()
        claim, capability = values[0], values[-1]
        receipt = capability.receipt()
        close_result = {"primary_fd": receipt["primary_fd"],
            "anchor_fd": receipt["anchor_fd"],
            "provenance_sha256": receipt["provenance_sha256"],
            "closed": True, "finished_ns": self.started + 311,
            "junk": 10 ** 5000}
        backend = SourcePidfdReleaseBackend(
            self.started + 310, close_result=close_result)
        with self.assertRaises(BaseException):
            claim.release_pidfd_model_once(capability, backend)
        result = claim.pidfd_outcome()
        self.assertTrue(result["pidfd_close_attempted"])
        self.assertFalse(result["pidfd_close_confirmed"])
        self.assertIsNone(result["close_result"])

    def test_source_pidfd_late_confirmed_is_not_full_cleanup(self):
        values = self._claim_with_pidfd_anchor()
        claim, capability = values[0], values[-1]
        deadline = claim.receipt()["deadline_ns"]
        backend = SourcePidfdReleaseBackend(
            deadline - 1, finished_ns=deadline)
        result = claim.release_pidfd_model_once(capability, backend)
        self.assertEqual(result["classification"],
                         "MODEL_PIDFD_CLOSE_CONFIRMED_LATE")
        self.assertTrue(result["pidfd_close_confirmed"])
        self.assertFalse(result["resources_closed"])
        self.assertFalse(result["descendants_qualified"])

    def test_full_release_model_cannot_reopen_descendant_consumer(self):
        values = self._claim_with_pidfd_anchor()
        claim, capability = values[0], values[-1]
        backend = SourcePidfdReleaseBackend(self.started + 310)
        result = claim.release_pidfd_model_once(capability, backend)
        self.assertEqual(result["classification"],
                         "MODEL_PIDFD_CLOSE_CONFIRMED")
        settlement = self.attempt.settlement_handle
        descendant = settlement.descendant_capability
        self.assertTrue(settlement.probe_used)
        self.assertTrue(descendant.used)
        with self.assertRaises(BaseException):
            CAPTURE.take_descendant_settlement_capability(
                settlement, self.attempt.capture_handle,
                self.attempt.capture_adapter)
        with self.assertRaises(BaseException):
            CAPTURE.probe_children_after_reap(settlement, object())
        self.assertFalse(result["descendants_qualified"])

    def test_source_has_no_live_fork_exec_or_storage_surface(self):
        source = (EXP / "prelive_abort_fork_split.py").read_text()
        for forbidden in ("os.fork", "execve", "subprocess", "dmsetup",
                          "lvcreate", "pvesm"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
