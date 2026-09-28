import importlib
import os
from pathlib import Path
import sys
import threading
import unittest

EXP = Path(__file__).resolve().parents[2] / "experiments/thick-generations"
sys.path.insert(0, str(EXP))
LAB = importlib.import_module("prelive_real_fd_adapter")


@unittest.skipUnless(os.name == "posix" and hasattr(os, "pipe2"),
                     "real Linux pipe semantics required")
class RealFDAdapterTests(unittest.TestCase):
    def setUp(self):
        self.handle = LAB.allocate_real_fd_graph(LAB.LinuxFDCalls())

    def tearDown(self):
        if getattr(self, "handle", None) is not None and self.handle.owned:
            self.handle.close_all_once()

    def test_exact_eight_endpoint_graph_and_flags(self):
        result = self.handle.validate()
        self.assertEqual(result["classification"], "FD_GRAPH_READY")
        self.assertEqual(len(result["bundle"]), 8)
        self.assertFalse(result["runtime_authorized"])
        self.assertFalse(result["exec_proven"])

    def test_real_nonblocking_status_data_eagain_and_eof(self):
        write_fd = self.handle.frozen_bundle["status_child"]["fd"]
        self.assertEqual(self.handle.read_nonblocking("status_parent", 16)["kind"],
                         "EAGAIN")
        os.write(write_fd, b"R")
        self.assertEqual(self.handle.read_nonblocking("status_parent", 16),
                         {"kind": "DATA", "data": b"R"})
        self.assertEqual(self.handle.read_nonblocking("status_parent", 16)["kind"],
                         "EAGAIN")
        self.handle.close_role_once("status_child")
        self.assertEqual(self.handle.read_nonblocking("status_parent", 16)["kind"],
                         "EOF")

    def test_exact_grant_byte_and_one_shot_replay_refusal(self):
        receipt = self.handle.write_grant_once(b"G")
        self.assertEqual(receipt["classification"],
                         "ONE_GRANT_BYTE_WRITE_CONFIRMED")
        grant_read = self.handle.frozen_bundle["grant_child"]["fd"]
        self.assertEqual(os.read(grant_read, 1), b"G")
        with self.assertRaisesRegex(LAB.Refusal, "one-shot"):
            self.handle.write_grant_once(b"G")
        self.assertEqual(self.handle.state, "UNKNOWN_WRITE_REPLAY")

    def test_close_only_policy_blocks_direct_and_captured_grant_alias(self):
        self.handle.close_all_once()
        self.handle = LAB.allocate_real_fd_graph(
            LAB.LinuxFDCalls(), LAB.CLOSE_ONLY_NO_WRITE)
        calls = self.handle.calls
        real_write = calls.write
        write_calls = []
        def observed_write(fd, data):
            write_calls.append((fd, data))
            return real_write(fd, data)
        calls.write = observed_write
        captured = self.handle.write_grant_once
        for writer in (captured, self.handle.write_grant_once,
                       lambda data: LAB.RealFDHandle.write_grant_once(
                           self.handle, data)):
            with self.assertRaisesRegex(LAB.Refusal,
                                        "forbidden by immutable policy"):
                writer(b"G")
        self.assertEqual(write_calls, [])
        self.assertEqual(self.handle.attempted_writes, set())
        self.assertEqual(self.handle.snapshot()["grant_policy"],
                         LAB.CLOSE_ONLY_NO_WRITE)

    def test_close_only_policy_cannot_be_upgraded(self):
        self.handle.close_all_once()
        self.handle = LAB.allocate_real_fd_graph(
            LAB.LinuxFDCalls(), LAB.CLOSE_ONLY_NO_WRITE)
        with self.assertRaisesRegex(LAB.Refusal, "immutable"):
            self.handle._grant_policy = LAB.GRANT_ONCE
        with self.assertRaisesRegex(LAB.Refusal, "immutable"):
            self.handle._grant_policy_sealed = False
        with self.assertRaisesRegex(LAB.Refusal, "immutable"):
            del self.handle._grant_policy
        with self.assertRaisesRegex(LAB.Refusal, "immutable"):
            del self.handle._grant_policy_sealed
        self.assertEqual(self.handle.snapshot()["grant_policy"],
                         LAB.CLOSE_ONLY_NO_WRITE)
        writes = []
        self.handle.calls.write = lambda fd, data: writes.append((fd, data))
        with self.assertRaisesRegex(LAB.Refusal,
                                    "forbidden by immutable policy"):
            self.handle.write_grant_once(b"G")
        self.assertEqual(writes, [])

    def test_graph_mutation_and_type_alias_fail_closed(self):
        for field, value in (("fd", 10.0), ("fd_flags", True)):
            handle = self.handle
            original = handle.bundle["grant_parent"][field]
            handle.bundle["grant_parent"][field] = value
            with self.assertRaises(LAB.Refusal):
                handle.validate()
            handle.bundle["grant_parent"][field] = original
            handle.close_all_once()
            # Each loop needs an independent real graph.
            self.handle = LAB.allocate_real_fd_graph(LAB.LinuxFDCalls())

    def test_cross_thread_use_refuses_without_io(self):
        errors = []
        worker = threading.Thread(target=lambda: self._capture_validate(errors))
        worker.start(); worker.join()
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], LAB.Refusal)
        self.assertIn("UNKNOWN_OWNER", self.handle.poison_reasons)
        self.assertEqual(self.handle.state, "UNKNOWN_OPERATION_ENTRY")

    def _capture_validate(self, errors):
        try:
            self.handle.validate()
        except BaseException as exc:
            errors.append(exc)

    def test_close_all_is_exact_and_terminal(self):
        result = self.handle.close_all_once()
        self.assertEqual(result["classification"], "FD_GRAPH_CLOSED")
        self.assertEqual(result["remaining"], [])
        self.handle = None

    def test_closed_or_reused_role_is_never_used_or_closed_again(self):
        closed_fd = self.handle.frozen_bundle["status_parent"]["fd"]
        self.handle.close_role_once("status_parent")
        replacement_read, replacement_write = os.pipe2(os.O_CLOEXEC)
        try:
            if replacement_read != closed_fd:
                os.dup2(replacement_read, closed_fd, inheritable=False)
            with self.assertRaises(LAB.Refusal):
                self.handle.read_nonblocking("status_parent", 1)
            with self.assertRaises(LAB.Refusal):
                self.handle.close_role_once("status_parent")
            os.write(replacement_write, b"X")
            self.assertEqual(os.read(closed_fd, 1), b"X")
        finally:
            for fd in {replacement_read, replacement_write, closed_fd}:
                try: os.close(fd)
                except OSError: pass

    def test_recycled_owned_fd_is_quarantined_not_closed(self):
        target = self.handle.frozen_bundle["grant_parent"]["fd"]
        replacement_read, replacement_write = os.pipe2(os.O_CLOEXEC)
        os.close(target)
        try:
            os.dup2(replacement_write, target, inheritable=False)
            with self.assertRaises(LAB.Refusal):
                self.handle.close_role_once("grant_parent")
            self.assertIn("grant_parent", self.handle.lost)
            os.write(target, b"Z")
            self.assertEqual(os.read(replacement_read, 1), b"Z")
        finally:
            for fd in {replacement_read, replacement_write, target}:
                try: os.close(fd)
                except OSError: pass

    def test_validation_poison_is_monotonic_after_record_repair(self):
        original = self.handle.bundle["grant_parent"]["fd_flags"]
        self.handle.bundle["grant_parent"]["fd_flags"] = True
        with self.assertRaises(LAB.Refusal): self.handle.validate()
        self.handle.bundle["grant_parent"]["fd_flags"] = original
        with self.assertRaisesRegex(LAB.Refusal, "poisoned"):
            self.handle.write_grant_once(b"G")
        self.assertEqual(self.handle.attempted_writes, set())

    def test_swallowed_reentrant_close_cannot_return_success(self):
        real_close = self.handle.calls.close
        target_role = "grant_parent"
        target_fd = self.handle.frozen_bundle[target_role]["fd"]
        fired = []
        def reentrant(fd):
            if fd == target_fd and not fired:
                fired.append(True)
                try: self.handle.validate()
                except LAB.Refusal: pass
            return real_close(fd)
        self.handle.calls.close = reentrant
        try:
            with self.assertRaises(LAB.Refusal):
                self.handle.close_role_once(target_role)
        finally:
            self.handle.calls.close = real_close
        self.assertTrue(self.handle.poisoned)
        self.assertNotIn(target_role, self.handle.owned)

    def test_close_after_effect_error_remains_unknown_after_release(self):
        real_close = self.handle.calls.close
        target = self.handle.frozen_bundle["grant_parent"]["fd"]
        fired = []
        def close_then_error(fd):
            result = real_close(fd)
            if fd == target and not fired:
                fired.append(True)
                raise OSError("close after effect")
            return result
        self.handle.calls.close = close_then_error
        with self.assertRaises(OSError):
            self.handle.close_role_once("grant_parent")
        self.handle.calls.close = real_close
        result = self.handle.close_all_once()
        self.assertEqual(result["classification"], "UNKNOWN_CLOSE")
        self.assertTrue(self.handle.poisoned)
        self.assertNotEqual(self.handle.state, "FD_GRAPH_CLOSED")
        self.handle = None

    def test_owner_callback_failure_permanently_blocks_grant(self):
        real_owner = self.handle.calls.owner_identity
        fired = []
        def fail_once():
            if not fired:
                fired.append(True)
                raise OSError("owner fault")
            return real_owner()
        self.handle.calls.owner_identity = fail_once
        with self.assertRaises(OSError): self.handle.validate()
        self.handle.calls.owner_identity = real_owner
        with self.assertRaisesRegex(LAB.Refusal, "poisoned"):
            self.handle.write_grant_once(b"G")
        self.assertEqual(self.handle.attempted_writes, set())

    def test_mutated_frozen_authority_never_closes_foreign_pipe(self):
        foreign_read, foreign_write = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
        original = dict(self.handle.frozen_bundle["grant_parent"])
        try:
            foreign = LAB._identity(self.handle.calls, foreign_write,
                                    "grant_parent")
            self.handle.frozen_bundle["grant_parent"] = foreign
            with self.assertRaises(LAB.Refusal):
                self.handle.close_role_once("grant_parent")
            os.write(foreign_write, b"Q")
            self.assertEqual(os.read(foreign_read, 1), b"Q")
        finally:
            self.handle.frozen_bundle["grant_parent"] = original
            for fd in (foreign_read, foreign_write):
                try: os.close(fd)
                except OSError: pass

    def test_callback_frozen_mutation_stops_before_close(self):
        calls = self.handle.calls
        real_fstat = calls.fstat
        original = dict(self.handle.frozen_bundle["grant_parent"])
        target = original["fd"]
        fired = []
        def mutate(fd):
            result = real_fstat(fd)
            if fd == target and not fired:
                fired.append(True)
                self.handle.frozen_bundle["grant_parent"]["inode"] += 1
            return result
        calls.fstat = mutate
        try:
            with self.assertRaises(LAB.Refusal):
                self.handle.close_role_once("grant_parent")
        finally:
            calls.fstat = real_fstat
            self.handle.frozen_bundle["grant_parent"] = original
        os.write(target, b"K")
        read_fd = self.handle.frozen_bundle["grant_child"]["fd"]
        self.assertEqual(os.read(read_fd, 1), b"K")
        os.close(target)

    def test_last_validation_callback_cannot_redirect_write_or_read(self):
        for operation in ("write", "read"):
            handle = self.handle
            foreign_read, foreign_write = os.pipe2(
                os.O_CLOEXEC | os.O_NONBLOCK)
            calls = handle.calls
            real_fstat = calls.fstat
            last_fd = handle._authority_record("stdout_parent")["fd"]
            target_role = "grant_parent" if operation == "write" else "status_parent"
            foreign_fd = foreign_write if operation == "write" else foreign_read
            foreign = LAB._identity(calls, foreign_fd, target_role)
            original = dict(handle.frozen_bundle[target_role])
            fired = []
            def mutate(fd):
                result = real_fstat(fd)
                if fd == last_fd and not fired:
                    fired.append(True)
                    handle.frozen_bundle[target_role] = foreign
                return result
            calls.fstat = mutate
            try:
                with self.assertRaises(LAB.Refusal):
                    if operation == "write": handle.write_grant_once(b"G")
                    else: handle.read_nonblocking("status_parent", 1)
                with self.assertRaises(BlockingIOError): os.read(foreign_read, 1)
            finally:
                calls.fstat = real_fstat
                handle.frozen_bundle[target_role] = original
                for fd in (foreign_read, foreign_write):
                    try: os.close(fd)
                    except OSError: pass
            handle.close_all_once()
            if operation == "write":
                self.handle = LAB.allocate_real_fd_graph(LAB.LinuxFDCalls())


class MockFaultCalls:
    def __init__(self):
        self.closed = []
        self.next_fd = 10
        self.fail_pipe = 2
    def owner_identity(self):
        return {"pid": 1, "starttime": 2,
                "boot_id": "12345678-1234-1234-1234-123456789abc"}
    def fstat(self, fd):
        class Info: pass
        value = Info()
        value.st_dev = 1
        value.st_ino = fd // 2
        value.st_mode = LAB.stat.S_IFIFO | 0o600
        return value
    def fcntl(self, fd, command, argument=None):
        if command == LAB.fcntl.F_GETFL:
            return os.O_RDONLY if fd % 2 == 0 else os.O_WRONLY
        if command == LAB.fcntl.F_GETFD: return LAB.fcntl.FD_CLOEXEC
        if command == LAB.fcntl.F_SETFL: return 0
        raise AssertionError("fcntl")
    def pipe2(self, _flags):
        self.fail_pipe -= 1
        if self.fail_pipe == 0:
            raise OSError("pipe fault")
        pair = (self.next_fd, self.next_fd + 1)
        self.next_fd += 2
        return pair
    def close(self, fd): self.closed.append(fd)


class AllocationFaultTests(unittest.TestCase):
    def test_partial_allocation_closes_each_known_fd_once(self):
        calls = MockFaultCalls()
        with self.assertRaises(LAB.AllocationRefusal) as caught:
            LAB.allocate_real_fd_graph(calls)
        self.assertEqual(calls.closed, [11, 10])
        self.assertEqual(caught.exception.cause, "OSError")
        self.assertEqual([item["outcome"] for item in caught.exception.cleanup],
                         ["CLOSE_CONFIRMED", "CLOSE_CONFIRMED"])

    def test_second_endpoint_identity_failure_closes_known_peer_once(self):
        class SecondIdentityFault(MockFaultCalls):
            def __init__(self):
                super().__init__()
                self.fail_pipe = -1
            def fstat(self, fd):
                if fd == 11:
                    raise OSError("write endpoint identity fault")
                return super().fstat(fd)
        calls = SecondIdentityFault()
        with self.assertRaises(LAB.AllocationRefusal) as caught:
            LAB.allocate_real_fd_graph(calls)
        self.assertEqual(calls.closed, [10])
        by_fd = {item["fd"]: item["outcome"]
                 for item in caught.exception.cleanup}
        self.assertEqual(by_fd[10], "CLOSE_CONFIRMED")
        self.assertEqual(by_fd[11], "QUARANTINED_UNKNOWN")

    def test_multithreaded_allocator_refuses_before_pipe(self):
        calls = MockFaultCalls()
        old = LAB.threading.active_count
        LAB.threading.active_count = lambda: 2
        try:
            with self.assertRaisesRegex(LAB.Refusal, "single-thread"):
                LAB.allocate_real_fd_graph(calls)
        finally:
            LAB.threading.active_count = old
        self.assertEqual(calls.closed, [])

    def test_unsafe_returned_std_fd_is_still_cleanup_owned(self):
        calls = MockFaultCalls()
        calls.fail_pipe = -1
        calls.pipe2 = lambda _flags: (0, 3)
        with self.assertRaises(LAB.AllocationRefusal) as caught:
            LAB.allocate_real_fd_graph(calls)
        self.assertEqual(calls.closed, [3, 0])
        self.assertEqual(len(caught.exception.cleanup), 2)

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "pipe2"),
                         "real Linux pipe semantics required")
    def test_replaced_endpoint_during_setup_is_quarantined(self):
        class ReplacingCalls(LAB.LinuxFDCalls):
            def __init__(self):
                self.replaced = None
                self.real_fcntl = LAB.fcntl.fcntl
            def fcntl(self, fd, command, argument=None):
                if command == LAB.fcntl.F_SETFL and self.replaced is None:
                    foreign_read, foreign_write = os.pipe2(os.O_CLOEXEC)
                    os.close(fd)
                    os.dup2(foreign_write, fd, inheritable=False)
                    self.replaced = (fd, foreign_read, foreign_write)
                    raise OSError("flag setup after replacement")
                if argument is None: return self.real_fcntl(fd, command)
                return self.real_fcntl(fd, command, argument)
        calls = ReplacingCalls()
        with self.assertRaises(LAB.AllocationRefusal) as caught:
            LAB.allocate_real_fd_graph(calls)
        target, foreign_read, foreign_write = calls.replaced
        try:
            quarantined = [item for item in caught.exception.cleanup
                           if item["fd"] == target]
            self.assertEqual(quarantined[0]["outcome"], "QUARANTINED_UNKNOWN")
            os.write(target, b"V")
            self.assertEqual(os.read(foreign_read, 1), b"V")
        finally:
            for fd in {target, foreign_read, foreign_write}:
                try: os.close(fd)
                except OSError: pass

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "pipe2"),
                         "real Linux pipe semantics required")
    def test_late_setup_failure_closes_authorized_nonblock_fds_once(self):
        class LateFailureCalls(LAB.LinuxFDCalls):
            def __init__(self):
                self.allocated = []
                self.closed = []
                self.setfl_count = 0
                self.fail_once = True
            def pipe2(self, flags):
                pair = os.pipe2(flags)
                self.allocated.extend(pair)
                return pair
            def fcntl(self, fd, command, argument=None):
                if command == LAB.fcntl.F_SETFL:
                    self.setfl_count += 1
                if argument is None:
                    return LAB.fcntl.fcntl(fd, command)
                return LAB.fcntl.fcntl(fd, command, argument)
            def fstat(self, fd):
                if (self.setfl_count == 4 and self.fail_once
                        and fd in self.allocated):
                    self.fail_once = False
                    raise OSError("late identity fault")
                return os.fstat(fd)
            def close(self, fd):
                self.closed.append(fd)
                return os.close(fd)
        calls = LateFailureCalls()
        with self.assertRaises(LAB.AllocationRefusal) as caught:
            LAB.allocate_real_fd_graph(calls)
        self.assertEqual(sorted(calls.closed), sorted(calls.allocated))
        self.assertEqual(len(calls.closed), 8)
        self.assertEqual(len(set(calls.closed)), 8)
        self.assertTrue(all(item["outcome"] == "CLOSE_CONFIRMED"
                            for item in caught.exception.cleanup))

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "pipe2"),
                         "real Linux pipe semantics required")
    def test_successful_flag_callback_cannot_replace_whole_pipe(self):
        class ReplacingPairCalls(LAB.LinuxFDCalls):
            def __init__(self):
                self.pairs = []
                self.foreign = None
                self.real_fcntl = LAB.fcntl.fcntl
            def pipe2(self, flags):
                pair = os.pipe2(flags)
                self.pairs.append(pair)
                return pair
            def fcntl(self, fd, command, argument=None):
                if command == LAB.fcntl.F_SETFL and self.foreign is None:
                    pair = next(pair for pair in self.pairs if fd in pair)
                    new_read, new_write = os.pipe2(os.O_CLOEXEC)
                    os.close(pair[0]); os.close(pair[1])
                    os.dup2(new_read, pair[0], inheritable=False)
                    os.dup2(new_write, pair[1], inheritable=False)
                    self.foreign = (pair, new_read, new_write)
                if argument is None: return self.real_fcntl(fd, command)
                return self.real_fcntl(fd, command, argument)
        calls = ReplacingPairCalls()
        with self.assertRaises(LAB.AllocationRefusal) as caught:
            LAB.allocate_real_fd_graph(calls)
        pair, source_read, source_write = calls.foreign
        try:
            for target in pair:
                item = next(value for value in caught.exception.cleanup
                            if value["fd"] == target)
                self.assertEqual(item["outcome"], "QUARANTINED_UNKNOWN")
            os.write(pair[1], b"W")
            self.assertEqual(os.read(pair[0], 1), b"W")
        finally:
            for fd in set(pair + (source_read, source_write)):
                try: os.close(fd)
                except OSError: pass


if __name__ == "__main__":
    unittest.main()
