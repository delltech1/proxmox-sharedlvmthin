import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
HARNESS = ROOT / "experiments" / "thick-generations" / "executor-systemd-lab.py"


class ExecutorSystemdLabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = HARNESS.read_text(encoding="utf-8")

    def test_requires_explicit_disposable_scope(self):
        self.assertIn("DISPOSABLE-NONSTORAGE-SYSTEMD-LAB", self.source)
        self.assertIn('PARENT = pathlib.Path("/var/tmp")', self.source)
        self.assertIn('PREFIX = "slt-executor-systemd-lab-"', self.source)
        self.assertIn('UNIT_PREFIX = "slt-thick-lab-exec-"', self.source)
        self.assertIn("os.O_DIRECTORY | os.O_NOFOLLOW", self.source)
        self.assertIn("os.mkdir(root.name, 0o700, dir_fd=parent_fd)", self.source)

    def test_runner_starts_unarmed_and_requires_exact_grant(self):
        self.assertLess(self.source.index('atomic_json(root, "runner.json"'),
                        self.source.index('read_json(root, "grant.json"'))
        self.assertLess(self.source.index('read_json(root, "grant.json"'),
                        self.source.index('exclusive_json(root, "dispatch-marker.json"'))
        self.assertIn("os.link(temporary, name", self.source)
        for identity in (
            "attempt", "run_nonce", "invocation_id", "unit", "boot_id",
            "pid", "start_ticks", "runner_sha256",
        ):
            self.assertIn(f'"{identity}"', self.source)

    def test_systemd_contract_is_explicit_and_nonrestarting(self):
        for setting in (
            "--service-type=exec", "ExitType=cgroup", "Restart=no",
            "RemainAfterExit=yes", "KillMode=control-group", "NoNewPrivileges=yes",
            "PrivateDevices=yes", "ProtectSystem=strict", "ProtectHome=yes",
            "ProtectControlGroups=yes", "Delegate=no", "CapabilityBoundingSet=",
        ):
            self.assertIn(setting, self.source)
        self.assertNotIn("--collect", self.source)
        self.assertNotIn("--scope", self.source)
        self.assertIn("exact lab unit ownership is not proven; cleanup refused", self.source)

    def test_contains_no_storage_or_pve_mutation(self):
        for forbidden in (
            "lvcreate", "lvremove", "lvchange", "lvextend", "vgchange",
            "dmsetup", "multipath", "pvesm", "qm ", "pct ", "/etc/pve",
        ):
            self.assertNotIn(forbidden, self.source)

    def test_child_lifecycle_requires_external_release_and_completion(self):
        self.assertIn('"release-child"', self.source)
        self.assertIn('"child-complete.json"', self.source)
        self.assertIn('joinpath("cgroup.procs")', self.source)
        self.assertIn("unit became terminal while its controlled child remained", self.source)


if __name__ == "__main__":
    unittest.main()
