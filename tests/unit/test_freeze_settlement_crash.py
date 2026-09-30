"""Independent crash-prefix and CAS regressions for plugin-only FREEZE rotation."""

import copy
import sys
import types
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest import mock

from test_update_policy_runtime import load_script


class SimulatedCrash(RuntimeError):
    pass


class FreezeSettlementCrashTests(unittest.TestCase):
    def setUp(self):
        # Portable state-machine model only; native flock/fsync tests remain
        # in the Linux runtime suite. No fake lock method is ever invoked.
        imports = {} if sys.platform != "win32" else {"fcntl": types.ModuleType("fcntl")}
        with mock.patch.dict(sys.modules, imports):
            self.module, self.core = load_script()
        self.manifest = {"watched_packages": ["pve-sharedlvmthin", "qemu-server"],
                         "watched_package_patterns": []}
        self.old = [
            {"package": "pve-sharedlvmthin", "binary": "pve-sharedlvmthin",
             "version": "1", "architecture": "all", "status": "installed"},
            {"package": "qemu-server", "binary": "qemu-server",
             "version": "1", "architecture": "amd64", "status": "installed"},
        ]
        self.current = copy.deepcopy(self.old)
        self.versions = {"pve-sharedlvmthin": "1", "pve-sharedlvmthin-thick": None}
        self.holds = {"pve-sharedlvmthin", "qemu-server"}
        self.files = {self.module.FREEZE: {
            "schema": 2, "phase": "ACTIVE", "generation": "a" * 32,
            "manifest_sha256": self.core.digest(self.manifest),
            "targets": sorted(self.holds), "baseline": copy.deepcopy(self.old),
            "owned": sorted(self.holds), "preexisting": [],
        }}
        self.effects = []
        self.crash_after_write = None
        self.crash_after_unlink = None
        self.after_mark = None
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)

        def patch(name, **kwargs):
            return self.stack.enter_context(mock.patch.object(self.module, name, **kwargs))

        patch("read_json", side_effect=lambda path, default=None: copy.deepcopy(self.files.get(path, default)))
        patch("atomic_json", side_effect=self.write)
        patch("create_json", side_effect=self.create)
        patch("unlink_durable", side_effect=self.unlink)
        patch("installed_packages", side_effect=lambda: [row["package"] for row in self.current])
        patch("package_state_records", side_effect=lambda _targets: copy.deepcopy(self.current))
        patch("held_packages", side_effect=lambda: set(self.holds))
        patch("package_version", side_effect=lambda name: self.versions.get(name))
        patch("apt_mark", side_effect=self.mark)
        self.stack.enter_context(mock.patch.object(self.core, "strict_json", return_value=self.manifest))
        self.stack.enter_context(mock.patch.object(self.module.pathlib.Path, "read_text", return_value="boot-id"))
        self.stack.enter_context(mock.patch.object(self.module.os, "uname", create=True, return_value=SimpleNamespace(
            nodename="test-node", release="test-kernel")))
        self.receipt = self.module.prepare_freeze_package(
            "pve-sharedlvmthin", "1", "pve-sharedlvmthin", "2", "all", "b" * 64,
            "/candidate-manifest.json", self.manifest, self.core)
        self.current[0]["version"] = "2"
        self.versions["pve-sharedlvmthin"] = "2"
        self.effects.clear()

    def write(self, path, value):
        self.files[path] = copy.deepcopy(value)
        self.effects.append(("write", path, value.get("phase", value.get("state"))))
        if self.crash_after_write and self.crash_after_write(path, value):
            self.crash_after_write = None
            raise SimulatedCrash("crash after durable write")

    def create(self, path, value):
        if path in self.files:
            raise FileExistsError(str(path))
        self.write(path, value)

    def unlink(self, path):
        self.files.pop(path, None)
        self.effects.append(("unlink", path))
        if self.crash_after_unlink == path:
            self.crash_after_unlink = None
            raise SimulatedCrash("crash after durable unlink")

    def mark(self, operation, packages):
        self.effects.append(("apt-mark", operation, tuple(packages)))
        if operation == "hold":
            self.holds.update(packages)
        else:
            self.holds.difference_update(packages)
        if self.after_mark:
            hook, self.after_mark = self.after_mark, None
            hook()

    def settle(self, sha=None):
        return self.module.settle_freeze_package(
            "pve-sharedlvmthin", "2", sha or "b" * 64, self.receipt["txid"],
            self.manifest, self.core)

    def test_crash_after_active_publish_finishes_exact_successor(self):
        self.crash_after_write = lambda path, value: path == self.module.FREEZE and value.get("phase") == "ACTIVE"
        with self.assertRaises(SimulatedCrash):
            self.settle()
        successor = copy.deepcopy(self.files[self.module.FREEZE])
        result = self.settle()
        self.assertEqual(result, successor, "recovery must not rotate to another generation")
        self.assertNotIn(self.module.PACKAGE_TRANSITION, self.files)
        self.assertEqual(self.files[self.module.LAST_PACKAGE_TRANSITION]["state"], "COMPLETE")

    def test_crash_after_complete_archive_finishes_without_second_rotation(self):
        self.crash_after_write = lambda path, _value: path == self.module.LAST_PACKAGE_TRANSITION
        with self.assertRaises(SimulatedCrash):
            self.settle()
        successor = copy.deepcopy(self.files[self.module.FREEZE])
        self.assertEqual(self.settle(), successor)
        self.assertNotIn(self.module.PACKAGE_TRANSITION, self.files)

    def test_prepared_successor_is_not_regenerated_after_prepublication_crash(self):
        self.crash_after_write = lambda path, value: (
            path == self.module.PACKAGE_TRANSITION and value.get("state") == "SETTLEMENT_PREPARED")
        with self.assertRaises(SimulatedCrash):
            self.settle()
        successor = copy.deepcopy(self.files[self.module.PACKAGE_TRANSITION]["successor_ledger"])
        self.assertEqual(self.settle(), successor, "durably prepared successor identity is immutable")

    def test_rotating_ledger_must_match_durably_prepared_successor(self):
        self.crash_after_write = lambda path, value: path == self.module.FREEZE and value.get("phase") == "ROTATING"
        with self.assertRaises(SimulatedCrash):
            self.settle()
        self.files[self.module.FREEZE]["generation"] = "c" * 32
        self.effects.clear()
        with self.assertRaises(RuntimeError):
            self.settle()
        self.assertEqual(self.effects, [], "foreign ROTATING generation refused before hold changes")

    def test_crash_after_receipt_unlink_is_readonly_complete_replay(self):
        self.crash_after_unlink = self.module.PACKAGE_TRANSITION
        with self.assertRaises(SimulatedCrash):
            self.settle()
        successor = copy.deepcopy(self.files[self.module.FREEZE])
        self.effects.clear()
        self.assertEqual(self.settle(), successor)
        self.assertEqual(self.effects, [], "COMPLETE replay may verify but must not mutate")

    def test_complete_replay_requires_same_identity_and_current_integrity(self):
        successor = self.settle()
        self.effects.clear()
        self.assertEqual(self.settle(), successor)
        self.assertEqual(self.effects, [])
        with self.assertRaises(RuntimeError):
            self.settle(sha="c" * 64)
        self.assertEqual(self.effects, [])
        self.current[1]["version"] = "foreign-drift"
        with self.assertRaises(RuntimeError):
            self.settle()
        self.assertEqual(self.effects, [])

    def test_crashed_successor_cannot_be_replaced_with_foreign_ledger(self):
        self.crash_after_write = lambda path, value: path == self.module.FREEZE and value.get("phase") == "ACTIVE"
        with self.assertRaises(SimulatedCrash):
            self.settle()
        self.files[self.module.FREEZE]["generation"] = "c" * 32
        self.effects.clear()
        with self.assertRaises(RuntimeError):
            self.settle()
        self.assertEqual(self.effects, [])

    def test_target_architecture_must_equal_prepared_candidate(self):
        self.current[0]["architecture"] = "amd64"
        with self.assertRaises(RuntimeError):
            self.settle()
        self.assertEqual(self.effects, [], "architecture mismatch must precede holds or ledger writes")

    def test_ledger_cas_is_rechecked_after_hold_commands(self):
        self.after_mark = lambda: self.files[self.module.FREEZE].update(external_writer="foreign")
        with self.assertRaises(RuntimeError):
            self.settle()
        self.assertEqual(self.files[self.module.FREEZE].get("external_writer"), "foreign")
        self.assertNotEqual(self.files[self.module.FREEZE].get("phase"), "ACTIVE")
        self.assertIn(self.module.PACKAGE_TRANSITION, self.files)

    def test_package_state_is_rechecked_after_hold_commands(self):
        self.after_mark = lambda: self.current[1].update(version="foreign-drift")
        with self.assertRaises(RuntimeError):
            self.settle()
        self.assertNotEqual(self.files[self.module.FREEZE].get("phase"), "ACTIVE")
        self.assertIn(self.module.PACKAGE_TRANSITION, self.files)

    def test_transition_receipt_cas_is_rechecked_after_hold_commands(self):
        self.after_mark = lambda: self.files[self.module.PACKAGE_TRANSITION].update(txid="c" * 32)
        with self.assertRaises(RuntimeError):
            self.settle()
        self.assertEqual(self.files[self.module.PACKAGE_TRANSITION]["txid"], "c" * 32)
        self.assertNotEqual(self.files[self.module.FREEZE].get("phase"), "ACTIVE")

    def test_hold_outside_owned_release_set_cannot_disappear(self):
        self.holds.add("admin-unrelated-package")
        self.after_mark = lambda: self.holds.discard("admin-unrelated-package")
        with self.assertRaisesRegex(RuntimeError, "outside its exact release set"):
            self.settle()
        self.assertNotEqual(self.files[self.module.FREEZE].get("phase"), "ACTIVE")
        self.assertIn(self.module.PACKAGE_TRANSITION, self.files)


if __name__ == "__main__":
    unittest.main()
