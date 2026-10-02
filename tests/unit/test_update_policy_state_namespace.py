"""Read-only namespace probes; never inspect the host's actual package state."""
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from test_freeze_package_settlement import ROOT, load


class PolicyStateNamespaceTests(unittest.TestCase):
    def setUp(self):
        imports = {} if sys.platform != "win32" else {"fcntl": types.ModuleType("fcntl")}
        with mock.patch.dict(sys.modules, imports):
            self.m = load("policy_namespace_test", ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-update-policy")
        self.production_state = self.m.STATE
        self.production_replacement = self.production_state.parent / "profile-replacement"
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def use_state(self, state):
        self.m.STATE = state
        for field, name in (("POST_GATE", "post-gate-required.json"),
                            ("PACKAGE_TRANSITION", "package-transition.json"),
                            ("FREEZE", "freeze-owned.json"),
                            ("FREEZE_MIGRATION", "freeze-schema-migration.json")):
            setattr(self.m, field, state / name)

    def probe(self, present):
        inspected = []
        record = {"schema": 3, "phase": "ACTIVE"}
        def exists(path):
            inspected.append(path)
            return path in present
        with mock.patch.object(Path, "exists", exists), \
                mock.patch.object(self.m, "read_json", side_effect=lambda path:
                    record if path == self.m.FREEZE else None), \
                mock.patch.object(self.m, "freeze_integrity_v3", return_value={"result": "PASS"}), \
                mock.patch.object(self.m, "atomic_json", side_effect=AssertionError("unexpected write")), \
                mock.patch.object(self.m, "unlink_durable", side_effect=AssertionError("unexpected unlink")):
            result = self.m.migrate_freeze_schema({}, None)
        return result, inspected

    def test_injected_namespace_never_observes_foreign_production_receipts(self):
        state = Path(self.temporary.name) / "update-guard"
        self.use_state(state)
        foreign = {self.production_replacement / name for name in
                   ("ready.json", "prerm-intent.json", "dpkg-done.json")}
        result, inspected = self.probe(foreign)
        self.assertEqual(result["phase"], "ACTIVE")
        self.assertTrue(foreign.isdisjoint(inspected))
        for name in ("ready.json", "prerm-intent.json", "dpkg-done.json"):
            self.assertIn(state.parent / "profile-replacement" / name, inspected)

    def test_each_pending_receipt_in_its_own_namespace_still_refuses(self):
        for state in (Path(self.temporary.name) / "update-guard", self.production_state):
            self.use_state(state)
            for name in ("ready.json", "prerm-intent.json", "dpkg-done.json"):
                with self.subTest(state=str(state), name=name):
                    with self.assertRaisesRegex(RuntimeError, "profile replacement is pending"):
                        self.probe({state.parent / "profile-replacement" / name})

    def test_production_namespace_remains_the_fixed_original_location(self):
        self.assertEqual(self.production_state.as_posix(), "/var/lib/pve-sharedlvmthin/update-guard")
        self.assertEqual(self.production_replacement.as_posix(), "/var/lib/pve-sharedlvmthin/profile-replacement")
        self.use_state(self.production_state)
        _, inspected = self.probe(set())
        self.assertIn(self.production_replacement / "ready.json", inspected)


if __name__ == "__main__":
    unittest.main()
