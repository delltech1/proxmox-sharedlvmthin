import pathlib
import re
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
COLLECTOR = ROOT / "experiments/thick-generations/recovery-evidence.sh"
CLASSIFIER = ROOT / "experiments/thick-generations/classify-evidence.pl"
DELETE_FAULT_DRIVER = ROOT / "experiments/thick-generations/snapshot-delete-fault-driver.pl"
PLUGIN = ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
FAULT_DRIVER = ROOT / "experiments/thick-generations/fault-driver.pl"
PREPARE_RECOVERY = ROOT / "experiments/thick-generations/recover-prepare-incomplete.pl"
UNRECORDED_RECOVERY = ROOT / "experiments/thick-generations/recover-unrecorded-prepare.pl"
PRIOR_RECOVERY = ROOT / "experiments/thick-generations/recover-prepared-from-prior-evidence.pl"
COEXISTENCE = ROOT / "experiments/thick-generations/same-vg-coexistence-qualification.sh"
LONG_SOAK = ROOT / "experiments/thick-generations/long-soak-health-monitor.sh"


class ThickRecoveryHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = COLLECTOR.read_text(encoding="utf-8")

    def test_shell_syntax(self):
        result = subprocess.run(
            ["sh", "-n", str(COLLECTOR)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_requires_exact_mapper_device(self):
        self.assertIn('/dev/mapper/*', self.source)
        self.assertIn('pinned mapper device is not a block device', self.source)

    def test_lvm_inventory_is_single_scoped_and_read_only(self):
        self.assertEqual(len(re.findall(r"run_guarded vgs ", self.source)), 1)
        self.assertEqual(len(re.findall(r"run_guarded lvs ", self.source)), 1)
        self.assertEqual(self.source.count('--devices "$device"'), 2)
        self.assertGreaterEqual(self.source.count("--readonly"), 2)
        self.assertIn("upper_name=", self.source)

    def test_dm_table_and_status_are_noflush(self):
        self.assertIn('dmsetup table --noflush "$mapper"', self.source)
        self.assertIn('dmsetup status --noflush "$mapper"', self.source)

    def test_collector_contains_no_mutating_storage_commands(self):
        forbidden = (
            "lvcreate", "lvremove", "lvchange", "lvextend", "lvreduce",
            "pvcreate", "vgcreate", "wipefs", "dmsetup create", "dmsetup load",
            "dmsetup reload", "dmsetup suspend", "dmsetup resume", "dmsetup remove",
        )
        for command in forbidden:
            self.assertNotIn(command, self.source)

    def test_existing_output_is_never_overwritten(self):
        self.assertIn('[ ! -e "$output_dir" ]', self.source)

    def test_classifier_is_read_only_and_uses_authoritative_library(self):
        source = CLASSIFIER.read_text(encoding="utf-8")
        self.assertIn("classify_recovery(", source)
        self.assertIn("decode_anchor_tags", source)
        self.assertIn("decode_generation_tags", source)
        self.assertIn("decode_vg_intent_tags", source)
        self.assertIn("frontend mapper does not contain an exact", source)
        for command in ("lvcreate", "lvremove", "dmsetup create", "dmsetup load"):
            self.assertNotIn(command, source)

    def test_snapshot_delete_fault_driver_is_disposable_and_explicit(self):
        source = DELETE_FAULT_DRIVER.read_text(encoding="utf-8")
        self.assertIn("DISPOSABLE-SNAPSHOT-WILL-BE-LEFT-INCOMPLETE", source)
        self.assertIn("_thick_volume_snapshot_delete", source)
        self.assertIn("POSIX::_exit(137)", source)
        self.assertNotIn("unlink", source)
        self.assertNotIn("rm -", source)
        self.assertIn("/^D[0-4]$/", source)

    def test_classifier_syntax(self):
        result = subprocess.run(
            [
                "perl", f"-I{ROOT / 'usr/share/perl5'}", "-c", str(CLASSIFIER),
            ], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fault_boundaries_are_explicit_and_production_inert(self):
        source = PLUGIN.read_text(encoding="utf-8")
        for point in range(10):
            self.assertEqual(
                source.count(f"_thick_fault_point('C{point}'"), 1,
                f"C{point} must identify exactly one crash boundary",
            )
        body = re.search(
            r"sub _thick_fault_point \{(?P<body>.*?)\n\}", source, re.DOTALL
        )
        self.assertIsNotNone(body)
        self.assertEqual(body.group("body").strip(), "return;")
        self.assertNotIn("SLT_FAULT", source)

    def test_fault_driver_requires_disposable_ack_and_process_local_override(self):
        source = FAULT_DRIVER.read_text(encoding="utf-8")
        self.assertIn("DISPOSABLE-DATA-WILL-BE-LEFT-INCOMPLETE", source)
        self.assertIn("no warnings 'redefine'", source)
        self.assertIn("POSIX::_exit(137)", source)
        self.assertNotIn("$ENV", source)

    def test_prepare_recovery_is_exact_and_refuses_partial_objects(self):
        source = PREPARE_RECOVERY.read_text(encoding="utf-8")
        self.assertIn("CLEAR-ONLY-PROVEN-PREPARE-INCOMPLETE", source)
        self.assertIn("_with_vg_lock", source)
        self.assertIn("_thick_verify_frontend", source)
        self.assertIn("transition metadata exists", source)
        self.assertIn("a second HEAD exists", source)
        self.assertEqual(source.count("_clear_vg_intent"), 1)
        for command in ("lvremove", "dmsetup remove", "pvcreate", "vgcreate"):
            self.assertNotIn(command, source)

    def test_unrecorded_prepare_recovery_removes_only_two_signed_inactive_objects(self):
        source = UNRECORDED_RECOVERY.read_text(encoding="utf-8")
        self.assertIn("REMOVE-ONLY-PROVEN-UNRECORDED-PREPARE", source)
        self.assertIn("validate_generation_tags", source)
        self.assertIn("validate_transition_tags", source)
        self.assertIn("_verify_autoactivation_disabled", source)
        self.assertIn("unrecorded transition object is active", source)
        self.assertEqual(source.count("['/sbin/lvremove'"), 1)
        self.assertIn('"$option{vg}/$meta", "$option{vg}/$new"', source)
        self.assertNotIn("wipefs", source)

    def test_prepared_evidence_recovery_orders_anchor_before_exact_cleanup(self):
        source = PRIOR_RECOVERY.read_text(encoding="utf-8")
        self.assertIn("RESTORE-ONLY-EXACT-PRIOR-SIGNED-ANCHOR", source)
        self.assertIn("prior evidence is not the exact predecessor", source)
        self.assertIn("live anchor changed after current evidence capture", source)
        self.assertIn("PREPARED source mapper exists", source)
        anchor_update = source.index("$class->_change_exact_tags(")
        cleanup = source.index("['/sbin/lvremove'")
        clear = source.index("$class->_clear_vg_intent")
        self.assertLess(anchor_update, cleanup)
        self.assertLess(cleanup, clear)
        self.assertNotIn("wipefs", source)

    def test_same_vg_coexistence_driver_is_disposable_and_fail_closed(self):
        source = COEXISTENCE.read_text(encoding="utf-8")
        result = subprocess.run(
            ["bash", "-n", str(COEXISTENCE)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("I_ACCEPT_DESTRUCTIVE_DISPOSABLE_TEST", source)
        self.assertIn("_verify_same_vg_alias_configuration", source)
        self.assertIn("SAME_VG_CANONICAL_LOCK_CONTENTION=PASS", source)
        self.assertIn('--devices "$device"', source)
        self.assertIn("INVENTORY_ISOLATION=PASS", source)
        self.assertIn("thick volume leaked into thin inventory", source)
        self.assertIn("thin volume leaked into thick inventory", source)
        self.assertNotIn('! grep -Fq "$thick_vol"', source)
        self.assertNotIn('! grep -Fq "$thin_vol"', source)
        self.assertIn("PVE_SNAPSHOT_RESIZE_ROLLBACK_DELETE=PASS", source)
        self.assertIn("VG_FREE_BYTES_AFTER_COMPLETE_LIFECYCLE_DELTA=0", source)
        self.assertNotIn("trap ", source)
        self.assertNotIn("--force", source)

    def test_long_soak_rechecks_only_a_sole_unscoped_dstate_unknown(self):
        source = LONG_SOAK.read_text(encoding="utf-8")
        result = subprocess.run(
            ["bash", "-n", str(LONG_SOAK)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("UNSCOPED_DSTATE_RECHECKS=1", source)
        self.assertEqual(source.count('sleep 2'), 1)
        self.assertEqual(source.count('TRANSIENT_UNSCOPED_DSTATE_RECHECK=PASS'), 1)
        self.assertIn("[[ $(grep -Ec '=UNKNOWN$'", source)
        self.assertIn("[[ $(grep -Ec '=FAIL$'", source)
        self.assertIn("grep -qx 'NO_RELEVANT_DSTATE=UNKNOWN'", source)
        for gate in (
            "PATHS_HEALTHY", "WWID_MATCH", "PV_UUID_MATCH", "VG_UUID_MATCH",
            "POOL_FLAGS_HEALTHY", "BOUNDED_LVM_PROBES", "PVE_STORAGE_HEALTH",
            "QUORUM",
        ):
            self.assertIn(gate, source)
        for command in (
            "lvcreate", "lvremove", "lvchange", "lvextend", "lvreduce",
            "pvcreate", "vgcreate", "wipefs", "dmsetup create", "dmsetup load",
        ):
            self.assertNotIn(command, source)


if __name__ == "__main__":
    unittest.main()
