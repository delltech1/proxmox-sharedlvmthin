from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "experiments" / "thick-generations" / "lazy-zero-shared-lvm-lab.pl"


class LazyZeroLvmLabContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE.read_text(encoding="utf-8")

    def _action_body(self):
        start = self.source.index(
            "if ($action eq 'activate' || $action eq 'deactivate' || $action eq 'hold')"
        )
        end = self.source.index("my $cleanup =", start)
        return self.source[start:end]

    def test_runtime_state_is_not_inferred_from_readonly_lv_attr(self):
        body = self._action_body()
        self.assertNotRegex(body, r"\{attr\}.*\^\.\.\.\.a")
        self.assertIn("require_active_identity", body)
        self.assertIn("require_kernel_absence($vg, $data, $meta)", body)

    def test_mutating_lvchange_is_single_dispatch_without_timeout_or_reconcile(self):
        body = self._action_body()
        self.assertEqual(body.count("'/sbin/lvchange'"), 2)
        self.assertNotIn("'/usr/bin/timeout'", body)
        self.assertNotIn("_thick_activate_exact_lvs", body)
        self.assertNotIn("_thick_deactivate_exact_lvs", body)
        self.assertNotIn("_thick_verify_active_lv_identity", body)
        self.assertNotRegex(body, r"eval\s*\{\s*run_command\([^;]*lvchange")

    def test_persistent_no_autoactivation_flags_are_required(self):
        self.assertIn("'--setactivationskip', 'y'", self.source)
        self.assertIn("'--setautoactivation', 'n'", self.source)
        self.assertIn("LV activation-skip flag is absent", self.source)
        self.assertIn("LV autoactivation is enabled", self.source)

    def test_scale_geometry_is_canonical_bounded_and_owned(self):
        self.assertIn("qw(storeid tx nonce data-gib)", self.source)
        self.assertIn("data size must be an integer from 1 through 512 GiB", self.source)
        self.assertIn("$Config{ivsize} < 8", self.source)
        self.assertIn("my $data_bytes = $data_gib * 1024 * 1024 * 1024", self.source)
        self.assertIn("my $data_sectors = int($data_bytes / 512)", self.source)
        self.assertIn("my $total_regions = int($data_bytes / $region_bytes)", self.source)
        self.assertIn('"slt_lazy_bytes_$data_bytes"', self.source)
        self.assertIn("@size_tags != 1", self.source)
        for field in ("data_gib", "data_bytes", "data_sectors", "region_bytes",
                      "total_regions", "metadata_bytes"):
            self.assertIn(field, self.source)

    def test_cleanup_binds_geometry_before_first_remove(self):
        cleanup = self.source.index("my $cleanup =")
        data_check = self.source.index(
            "validate_record($data_record, $vg, $data, $data_bytes, $data_bytes",
            cleanup)
        meta_check = self.source.index(
            "validate_record($meta_record, $vg, $meta, $meta_bytes, $data_bytes",
            cleanup)
        remove = self.source.index("'/sbin/lvremove'", cleanup)
        self.assertLess(data_check, remove)
        self.assertLess(meta_check, remove)

    def test_scale_capacity_is_admitted_before_intent_and_create(self):
        setup = self.source.index("if ($action eq 'setup')")
        capacity = self.source.index("$class->_thick_capacity_gate(", setup)
        self.assertIn("int(($data_bytes + $meta_bytes + 1023) / 1024)",
                      self.source[capacity:capacity + 300])
        intent = self.source.index("set_intent_terminal", capacity)
        create = self.source.index("'/sbin/lvcreate'", intent)
        self.assertLess(capacity, intent)
        self.assertLess(intent, create)

    def test_kernel_inventory_is_complete_and_rejects_ambiguity(self):
        match = re.search(
            r"sub dm_kernel_inventory \{(?P<body>.*?)\n\}", self.source, re.S
        )
        self.assertIsNotNone(match)
        body = match.group("body")
        self.assertIn("'name,uuid,suspended'", body)
        self.assertIn("exists($inventory{$name})", body)
        self.assertIn("command_lines", body)

    def test_peer_hold_and_activation_share_the_canonical_vg_lock(self):
        self.assertIn("$action eq 'hold'", self.source)
        branch = self.source.index(
            "if ($action eq 'activate' || $action eq 'deactivate' || $action eq 'hold')"
        )
        lock = self.source.index("$class->_with_vg_lock", branch)
        hold = self.source.index("ensure_local_hold", lock)
        refusal = self.source.index("require_no_local_hold", hold)
        lvchange = self.source.index("'/sbin/lvchange'", refusal)
        self.assertLess(lock, hold)
        self.assertLess(hold, refusal)
        self.assertLess(refusal, lvchange)
        self.assertIn("O_WRONLY | O_CREAT | O_EXCL", self.source)
        self.assertIn("$fh->sync()", self.source)
        self.assertIn("sync_directory($base, 'peer hold base')", self.source)
        self.assertIn("existing peer hold identity mismatch", self.source)
        self.assertIn("LAB_PEER_HOLD_RETAINED_EXACT_ABSENCE_PROVEN", self.source)
        retained = self.source.index("existing peer hold identity mismatch")
        returned = self.source.index("return ($path, 0)", retained)
        retained_body = self.source[retained:returned]
        self.assertIn("cannot sync retained peer hold record", retained_body)
        self.assertIn("sync_directory($path, 'retained peer hold')", retained_body)
        self.assertIn("sync_directory($base, 'retained peer hold base')", retained_body)
        self.assertIn("sync_directory($parent, 'retained peer hold parent')", retained_body)
        cleanup = self.source.index("my $cleanup =")
        cleanup_hold = self.source.index("require_no_local_hold", cleanup)
        cleanup_remove = self.source.index("'/sbin/lvremove'", cleanup_hold)
        self.assertLess(cleanup_hold, cleanup_remove)


if __name__ == "__main__":
    unittest.main()
