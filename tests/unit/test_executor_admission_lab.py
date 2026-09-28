import json
import pathlib
import runpy
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
HARNESS = ROOT / "experiments" / "thick-generations" / "executor-admission-lab.py"


class ExecutorAdmissionLabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = HARNESS.read_text(encoding="utf-8")
        cls.namespace = runpy.run_path(str(HARNESS), run_name="executor_admission_lab_test")

    def test_is_explicitly_disposable_and_path_scoped(self):
        self.assertIn("DISPOSABLE-NONSTORAGE-ADMISSION-LAB", self.source)
        self.assertIn('ROOT_PARENT = pathlib.Path("/var/tmp")', self.source)
        self.assertIn('ROOT_PREFIX = "slt-executor-admission-lab-"', self.source)
        self.assertIn("lab root must be a direct /var/tmp/slt-executor-admission-lab-* path", self.source)
        self.assertIn("os.O_DIRECTORY | os.O_NOFOLLOW", self.source)

    def test_uses_one_stable_lock_and_durable_update_order(self):
        self.assertIn("fcntl.LOCK_EX | fcntl.LOCK_NB", self.source)
        self.assertIn("authority lock acquisition timed out; no takeover attempted", self.source)
        self.assertIn("stable lock inode is missing; authority state is UNKNOWN", self.source)
        self.assertIn("os.O_NOFOLLOW", self.source)
        self.assertIn("os.fsync(fd)", self.source)
        self.assertIn('os.replace(temporary, "ledger.json"', self.source)
        self.assertIn("os.fsync(self.root_fd)", self.source)
        self.assertLess(self.source.index("os.fsync(fd)"), self.source.index("os.replace(temporary"))
        self.assertLess(self.source.index("os.replace(temporary"), self.source.index("os.fsync(self.root_fd)"))

    def test_missing_or_inconsistent_history_is_never_empty_authority(self):
        self.assertIn("ledger history is missing; authority state is UNKNOWN", self.source)
        self.assertIn("consumed-attempt history is invalid", self.source)
        self.assertIn("active slot is inconsistent with authority history", self.source)
        self.assertIn("if self.allow_uninitialized:", self.source)

    def test_duplicate_json_keys_are_rejected_recursively(self):
        hook = self.namespace["reject_duplicate_keys"]
        for payload in (
            '{"slots":{},"slots":{}}',
            '{"slots":{"a":{},"a":{}}}',
            '{"slots":{"a":{"state":"BOUND","state":"TERMINAL"}}}',
        ):
            with self.assertRaises(RuntimeError):
                json.loads(payload, object_pairs_hook=hook)

    def test_mutable_or_noncanonical_parent_paths_are_rejected(self):
        validate = self.namespace["Backend"].validate_root_name
        for path in (
            "/tmp/alias/slt-executor-admission-lab-x",
            "/var/tmp/../tmp/slt-executor-admission-lab-x",
            "slt-executor-admission-lab-x",
        ):
            with self.assertRaises(RuntimeError):
                validate(path)

    def test_has_no_storage_or_configuration_mutation_surface(self):
        for forbidden in (
            "lvcreate", "lvremove", "lvchange", "lvextend", "vgchange",
            "dmsetup", "multipath", "pvesm", "qm ", "pct ", "/etc/pve",
            "systemctl", "systemd-run", "subprocess", "os.system", "execv",
        ):
            self.assertNotIn(forbidden, self.source)

    def test_reports_scope_limits(self):
        self.assertIn("power-loss durability and cross-node authority were not tested", self.source)
        self.assertIn("systemd/cgroup invocation binding was not exercised", self.source)
        self.assertIn("no LVM, device-mapper, PVE configuration or guest storage was touched", self.source)


if __name__ == "__main__":
    unittest.main()
