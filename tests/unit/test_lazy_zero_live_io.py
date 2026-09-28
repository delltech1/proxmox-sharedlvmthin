import importlib.util
import io
import os
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "experiments" / "thick-generations" / "lazy-zero-live-io.py"
SPEC = importlib.util.spec_from_file_location("lazy_zero_live_io", SOURCE)
LAB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAB)
WRITER_SOURCE = ROOT / "experiments" / "thick-generations" / "lazy-zero-concurrent-writer.py"
WRITER_SPEC = importlib.util.spec_from_file_location("lazy_zero_concurrent_writer", WRITER_SOURCE)
WRITER = importlib.util.module_from_spec(WRITER_SPEC)
WRITER_SPEC.loader.exec_module(WRITER)
LAUNCHER_SOURCE = ROOT / "experiments" / "thick-generations" / "lazy-zero-controller-crash-launch.py"
LAUNCHER_SPEC = importlib.util.spec_from_file_location("lazy_zero_controller_crash_launch",
                                                       LAUNCHER_SOURCE)
LAUNCHER = importlib.util.module_from_spec(LAUNCHER_SPEC)
LAUNCHER_SPEC.loader.exec_module(LAUNCHER)


class LazyZeroLiveIOTests(unittest.TestCase):
    def test_pattern_is_deterministic_and_block_specific(self):
        first = LAB.pattern_block("fixture-a", 0)
        self.assertEqual(len(first), 1048576)
        self.assertEqual(first, LAB.pattern_block("fixture-a", 0))
        self.assertNotEqual(first, LAB.pattern_block("fixture-a", 1))
        self.assertNotEqual(first, LAB.pattern_block("fixture-b", 0))

    def test_pattern_file_has_exact_independent_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "canary")
            result = LAB.fill_pattern_file(path, 2 * 1048576, "fixture-a")
            self.assertEqual(result["bytes"], 2 * 1048576)
            self.assertTrue(result["full_readback"])
            self.assertTrue(result["logical_holes_excluded_by_pattern"])
            with open(path, "rb") as stream:
                self.assertEqual(stream.read(1048576), LAB.pattern_block("fixture-a", 0))
                self.assertEqual(stream.read(1048576), LAB.pattern_block("fixture-a", 1))
                self.assertEqual(stream.read(1), b"")

    def test_wrong_devno_refuses_before_any_ioctl(self):
        fake = types.SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(7, 1))
        with mock.patch.object(LAB.os, "open", return_value=19), \
                mock.patch.object(LAB.os, "close"), \
                mock.patch.object(LAB.os, "fstat", return_value=fake), \
                mock.patch.object(LAB.fcntl, "ioctl") as ioctl:
            with self.assertRaisesRegex(RuntimeError, "devno mismatch"):
                LAB.discard("/dev/wrong", 0, 4096, "success", 7, 2, 10, 1048576)
            ioctl.assert_not_called()

    def test_wrong_diskseq_refuses_before_discard_ioctl(self):
        fake = types.SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(7, 1))
        calls = []

        def fake_ioctl(_fd, request, _buffer, _mutate=True):
            calls.append(request)
            if request == LAB.BLKGETDISKSEQ:
                _buffer[:] = (9).to_bytes(8, "little")
                return 0
            raise AssertionError("discard or later identity ioctl was reached")

        with mock.patch.object(LAB.os, "open", return_value=19), \
                mock.patch.object(LAB.os, "close"), \
                mock.patch.object(LAB.os, "fstat", return_value=fake), \
                mock.patch.object(LAB.fcntl, "ioctl", side_effect=fake_ioctl):
            with self.assertRaisesRegex(RuntimeError, "diskseq mismatch"):
                LAB.discard("/dev/reused", 0, 4096, "success", 7, 1, 10, 1048576)
        self.assertEqual(calls, [LAB.BLKGETDISKSEQ])

    def test_direct_hash_progress_interval_is_explicit_and_aligned(self):
        with mock.patch.object(LAB.os, "open") as opened:
            with self.assertRaisesRegex(RuntimeError, "MiB aligned"):
                LAB.direct_hash("/dev/never-opened", 7, 1, 10, 1048576,
                                progress_bytes=4096)
            opened.assert_not_called()

        runner = (ROOT / "experiments" / "thick-generations" /
                  "lazy-zero-shared-hydrate-lab.sh").read_text(encoding="utf-8")
        self.assertEqual(runner.count("--progress-bytes 1073741824"), 6)

    def test_direct_zero_rejects_bad_geometry_before_open(self):
        for size in (0, 1048577, 513 * 1024 * 1024 * 1024):
            with mock.patch.object(LAB.os, "open") as opened:
                with self.assertRaisesRegex(RuntimeError, "zero geometry"):
                    LAB.direct_verify_zero("/dev/never-opened", 7, 1, 10, size)
                opened.assert_not_called()
        with mock.patch.object(LAB.os, "open") as opened:
            with self.assertRaisesRegex(RuntimeError, "MiB aligned"):
                LAB.direct_verify_zero("/dev/never-opened", 7, 1, 10,
                                       1048576, progress_bytes=4096)
            opened.assert_not_called()

    def test_direct_zero_full_scan_receipt_and_progress_are_separate(self):
        fake = types.SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(7, 1))
        reads = []

        def fake_preadv(_fd, views, offset):
            views[0][:] = bytes(len(views[0]))
            reads.append(offset)
            return len(views[0])

        with mock.patch.object(LAB.os, "open", return_value=19) as opened, \
                mock.patch.object(LAB.os, "close"), \
                mock.patch.object(LAB.os, "fstat", return_value=fake), \
                mock.patch.object(LAB, "block_number", side_effect=[10, 2097152, 10, 2097152]), \
                mock.patch.object(LAB.os, "preadv", side_effect=fake_preadv), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            result = LAB.direct_verify_zero("/dev/test", 7, 1, 10, 2097152,
                                            progress_bytes=1048576)
        self.assertEqual(reads, [0, 1048576])
        self.assertEqual(result, {"bytes": 2097152, "all_zero": True,
                                  "direct": True, "full_scan": True})
        self.assertEqual(stderr.getvalue().count("direct-verify-zero-progress"), 2)
        self.assertEqual(opened.call_args.args[1],
                         os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECT)

    def test_direct_zero_refuses_nonzero_and_short_reads_without_receipt(self):
        fake = types.SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(7, 1))

        def nonzero(_fd, views, _offset):
            views[0][:] = bytes(len(views[0]))
            views[0][len(views[0]) // 2] = 1
            return len(views[0])

        common = [mock.patch.object(LAB.os, "open", return_value=19),
                  mock.patch.object(LAB.os, "close"),
                  mock.patch.object(LAB.os, "fstat", return_value=fake)]
        with common[0], common[1], common[2], \
                mock.patch.object(LAB, "block_number", side_effect=[10, 1048576]), \
                mock.patch.object(LAB.os, "preadv", side_effect=nonzero):
            with self.assertRaisesRegex(RuntimeError, "nonzero byte"):
                LAB.direct_verify_zero("/dev/test", 7, 1, 10, 1048576)
        with mock.patch.object(LAB.os, "open", return_value=19), \
                mock.patch.object(LAB.os, "close"), \
                mock.patch.object(LAB.os, "fstat", return_value=fake), \
                mock.patch.object(LAB, "block_number", side_effect=[10, 1048576]), \
                mock.patch.object(LAB.os, "preadv", return_value=4096):
            with self.assertRaisesRegex(RuntimeError, "short O_DIRECT zero read"):
                LAB.direct_verify_zero("/dev/test", 7, 1, 10, 1048576)

    def test_direct_zero_refuses_post_scan_identity_drift(self):
        fake = types.SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(7, 1))

        def all_zero(_fd, views, _offset):
            views[0][:] = bytes(len(views[0]))
            return len(views[0])

        with mock.patch.object(LAB.os, "open", return_value=19), \
                mock.patch.object(LAB.os, "close"), \
                mock.patch.object(LAB.os, "fstat", return_value=fake), \
                mock.patch.object(LAB, "block_number",
                                  side_effect=[10, 1048576, 11]), \
                mock.patch.object(LAB.os, "preadv", side_effect=all_zero):
            with self.assertRaisesRegex(RuntimeError, "diskseq mismatch"):
                LAB.direct_verify_zero("/dev/test", 7, 1, 10, 1048576)

    def test_direct_zero_refuses_nonzero_final_byte_of_later_chunk(self):
        fake = types.SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(7, 1))

        def second_chunk_tail(_fd, views, offset):
            views[0][:] = bytes(len(views[0]))
            if offset == 1048576:
                views[0][-1] = 1
            return len(views[0])

        with mock.patch.object(LAB.os, "open", return_value=19), \
                mock.patch.object(LAB.os, "close"), \
                mock.patch.object(LAB.os, "fstat", return_value=fake), \
                mock.patch.object(LAB, "block_number", side_effect=[10, 2097152]), \
                mock.patch.object(LAB.os, "preadv", side_effect=second_chunk_tail):
            with self.assertRaisesRegex(RuntimeError, "nonzero byte"):
                LAB.direct_verify_zero("/dev/test", 7, 1, 10, 2097152)

    def test_live_harness_keeps_safety_boundaries(self):
        source = (ROOT / "experiments" / "thick-generations" /
                  "lazy-zero-l1-l3-live.sh").read_text(encoding="utf-8")
        self.assertIn("clone_block_id[$role]", source)
        self.assertIn("data_loop_id[$role]=$(loop_snapshot", source)
        self.assertIn("TERMINAL=UNKNOWN_RETAIN_EXACT_OBJECTS", source)
        self.assertIn("PRODUCTION_AUTHORIZED=NO", source)
        self.assertNotIn("dmsetup remove --retry", source)
        self.assertNotIn("dmsetup remove --force", source)
        self.assertNotIn("rm -rf", source)

    def test_fault_writer_uses_a_bounded_prefix_of_the_qualified_plan(self):
        size = 8 * 1024 * 1024 * 1024
        short = WRITER.build_plan(32, size)
        complete = WRITER.build_plan(1027, size)
        self.assertEqual(len(short), 32)
        self.assertEqual(len(complete), 1027)
        self.assertEqual(short, complete[:32])
        self.assertEqual(short[0], (1048576 - 4096, 8192, 0x7B))
        self.assertEqual(complete[-2], (size - 1048576 - 4096, 8192, 0xD3))
        self.assertEqual(complete[-1], (size - 4096, 4096, 0xE7))
        large = WRITER.build_plan(1027, 500 * 1024 * 1024 * 1024)
        self.assertEqual(large[-1][0], 500 * 1024 * 1024 * 1024 - 4096)
        with self.assertRaisesRegex(RuntimeError, "outside the qualified plan"):
            WRITER.build_plan(0, size)
        with self.assertRaisesRegex(RuntimeError, "outside the qualified plan"):
            WRITER.build_plan(1028, size)
        for bad_size in (0, 1024 * 1024, 513 * 1024 * 1024 * 1024):
            with self.assertRaisesRegex(RuntimeError, "outside the qualified geometry"):
                WRITER.build_plan(1, bad_size)

    def test_controller_crash_recovery_keeps_fail_closed_admission_order(self):
        recovery = (ROOT / "experiments" / "thick-generations" /
                    "lazy-zero-controller-crash-recover.sh").read_text(encoding="utf-8")
        launcher = (ROOT / "experiments" / "thick-generations" /
                    "lazy-zero-controller-crash-launch.py").read_text(encoding="utf-8")
        self.assertIn("terminated_by_sigkill(returncode)", launcher)
        self.assertIn("CONTROLLER_SIGKILL_OBSERVED", launcher)
        self.assertIn('$(wf MANIFEST_SHA256) == "$manifest_sha"', recovery)
        self.assertIn('recovery_rc -eq 2', recovery)
        self.assertIn("OPEN DM_PIVOT intent blocks mutation", recovery)
        claim = recovery.index('mkdir -m 0700 "$claim"')
        first_effect = recovery.index('dmsetup message "$clone" 0 enable_hydration')
        self.assertLess(claim, first_effect)
        self.assertNotIn('rmdir "$claim"', recovery)
        self.assertNotIn('rm -rf', recovery)

    def test_scale_runner_rejects_overflow_and_unqualified_fault_geometry(self):
        runner = (ROOT / "experiments" / "thick-generations" /
                  "lazy-zero-shared-hydrate-lab.sh").read_text(encoding="utf-8")
        self.assertIn("[1-4][0-9]{2}", runner)
        self.assertIn("$hydration_budget_sec =~ ^[1-9][0-9]{1,4}$", runner)
        self.assertIn("$fault_mode == none || $data_gib == 8", runner)
        self.assertIn("scale fault recovery is not yet geometry-qualified", runner)
        recovery = (ROOT / "experiments" / "thick-generations" /
                    "lazy-zero-controller-crash-recover.sh").read_text(encoding="utf-8")
        happy = (ROOT / "experiments" / "thick-generations" /
                 "lazy-zero-shared-hydrate-lab.sh").read_text(encoding="utf-8")
        for cleanup in (happy, recovery):
            self.assertIn('wait_open_zero "$name"', cleanup)
            self.assertLess(cleanup.rindex('wait_open_zero "$name"'),
                            cleanup.rindex('dmsetup remove "$name"'))
            self.assertNotIn("dmsetup remove --retry", cleanup)
            self.assertNotIn("dmsetup remove --force", cleanup)

    def test_controller_witness_distinguishes_exit_137_from_sigkill(self):
        self.assertFalse(LAUNCHER.terminated_by_sigkill(137))
        self.assertTrue(LAUNCHER.terminated_by_sigkill(-9))

    def test_controlled_reboot_prepare_is_quiesced_and_never_dispatches_reboot(self):
        runner = (ROOT / "experiments" / "thick-generations" /
                  "lazy-zero-shared-hydrate-lab.sh").read_text(encoding="utf-8")
        launcher = (ROOT / "experiments" / "thick-generations" /
                    "lazy-zero-reboot-prepare.py").read_text(encoding="utf-8")
        mode = runner.index("controlled-reboot-prepare")
        disable = runner.index('dmsetup message "$clone" 0 disable_hydration', mode)
        partial = runner.index('controlled-reboot-partial.manifest', disable)
        remove = runner.index("CONTROLLED_REBOOT_REMOVE_EXACT_DM_GRAPH", partial)
        metadata = runner.index("metadata_sha_result=", remove)
        deactivate = runner.index('"$lvm_helper" deactivate', metadata)
        ready = runner.index('controlled-reboot-ready.manifest', deactivate)
        self.assertLess(disable, partial)
        self.assertLess(partial, remove)
        self.assertLess(remove, metadata)
        self.assertLess(metadata, deactivate)
        self.assertLess(deactivate, ready)
        self.assertIn("CONTROLLED_REBOOT_PREPARE_EXIT0_OBSERVED", launcher)
        self.assertIn("if returncode != 0", launcher)
        self.assertIn("write_durable_exclusive", launcher)
        for field in ("PV_UUID", "PV_DEVICE", "WWID", "DATA_SKIP_ACTIVATION",
                      "META_SKIP_ACTIVATION", "DATA_AUTOACTIVATION",
                      "META_AUTOACTIVATION", "METADATA_SHA256"):
            self.assertIn(field, runner)
        for source in (runner, launcher):
            for forbidden in ("/sbin/reboot", "/sbin/shutdown", "systemctl reboot",
                              "shutdown -r", "reboot -f"):
                self.assertNotIn(forbidden, source)

    def test_controlled_reboot_prepare_does_not_weaken_controller_crash_mode(self):
        runner = (ROOT / "experiments" / "thick-generations" /
                  "lazy-zero-shared-hydrate-lab.sh").read_text(encoding="utf-8")
        controller_branch = runner.index("if [[ $fault_mode == controller-kill-after-partial ]]")
        kill = runner.index('kill -KILL "$$"', controller_branch)
        controlled_teardown = runner.index("CONTROLLED_REBOOT_REMOVE_EXACT_DM_GRAPH", kill)
        self.assertLess(kill, controlled_teardown)
        self.assertIn("CONTROLLER_CRASH_READY", runner[:controlled_teardown])
        self.assertNotIn("--allow-different-boot", runner)

    def test_controlled_reboot_recovery_claim_precedes_every_storage_effect(self):
        recovery = (ROOT / "experiments" / "thick-generations" /
                    "lazy-zero-reboot-recover.sh").read_text(encoding="utf-8")
        module_preflight = recovery.index('for module in dm_clone dm_zero dm_delay')
        target_preflight = recovery.index('pre-claim device-mapper target inventory',
                                          module_preflight)
        claim = recovery.index('mkdir -m 0700 "$claim"')
        peer_one = recovery.index('establish_peer_hold "$peer_1"', claim)
        peer_two = recovery.index('establish_peer_hold "$peer_2"', peer_one)
        activate = recovery.index('"$lvm_helper" activate', claim)
        delay = recovery.index('dmsetup create "$delay"', activate)
        zero = recovery.index('dmsetup create "$zero"', delay)
        clone = recovery.index('dmsetup create "$clone"', zero)
        resume = recovery.index('dmsetup message "$clone" 0 enable_hydration', clone)
        self.assertLess(module_preflight, target_preflight)
        self.assertLess(target_preflight, claim)
        self.assertLess(claim, activate)
        self.assertLess(claim, peer_one)
        self.assertLess(peer_one, peer_two)
        self.assertLess(peer_two, activate)
        self.assertLess(activate, delay)
        self.assertLess(delay, zero)
        self.assertLess(zero, clone)
        self.assertLess(clone, resume)
        self.assertIn('old_boot != "$new_boot"', recovery)
        self.assertIn("controlled-reboot-admission.manifest", recovery)
        self.assertIn("PEER_1_LAB_ABSENT", recovery)
        self.assertIn("PEER_2_LAB_ABSENT", recovery)
        self.assertIn("PEER_1_BOOT_ID", recovery)
        self.assertIn("PEER_2_BOOT_ID", recovery)
        self.assertIn("PEER_HOLD_ACTIVE_EXACT_AUDIT_PASS", recovery)
        self.assertIn("PEER_UUID_ACTIVE", recovery)
        self.assertIn("OPEN DM_PIVOT intent blocks mutation", recovery)
        self.assertIn("METADATA_SHA256", recovery)
        self.assertIn('timeout --foreground --kill-after=2s 15s modprobe "$module"',
                      recovery)
        self.assertNotIn("fill-block", recovery)
        self.assertNotIn("lvcreate", recovery)
        self.assertNotIn("--allow-different-boot", recovery)


if __name__ == "__main__":
    unittest.main()
