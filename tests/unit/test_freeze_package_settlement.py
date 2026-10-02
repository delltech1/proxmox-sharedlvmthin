"""Portable settlement state-machine tests; not Linux fsync/flock qualification."""
import copy
import importlib.machinery
import importlib.util
import sys
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class FreezePackageSettlementTests(unittest.TestCase):
    def setUp(self):
        # Only the platform import is stubbed on Windows. No lock API is used
        # by these isolated functions; native runtime tests remain separate.
        imports = {} if sys.platform != "win32" else {"fcntl": types.ModuleType("fcntl")}
        with mock.patch.dict(sys.modules, imports):
            self.m = load("freeze_settlement_test", ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-update-policy")
        self.core = load("freeze_settlement_core", ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin_update_policy.py")
        self.source = "pve-sharedlvmthin"
        self.target = "pve-sharedlvmthin-thick"
        self.manifest = {"watched_packages": [self.source, self.target, "qemu-server"],
                         "watched_package_patterns": []}
        self.records = [self.row(self.source, "1", "all"), self.row("qemu-server", "1", "amd64")]
        self.holds = {self.source, "qemu-server"}
        self.store = {self.m.FREEZE: {
            "schema": 2, "phase": "ACTIVE", "generation": "a" * 32,
            "manifest_sha256": self.core.digest(self.manifest),
            "targets": sorted(self.holds), "baseline": copy.deepcopy(self.records),
            "owned": sorted(self.holds), "preexisting": [],
        }}
        self.effects = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        patches = [
            mock.patch.object(self.m, "read_json", side_effect=lambda p, default=None: copy.deepcopy(self.store.get(p, default))),
            mock.patch.object(self.m, "atomic_json", side_effect=self.write),
            mock.patch.object(self.m, "create_json", side_effect=self.create),
            mock.patch.object(self.m, "unlink_durable", side_effect=lambda p: self.store.pop(p, None)),
            mock.patch.object(self.m, "installed_packages", side_effect=lambda: [r["package"] for r in self.records]),
            mock.patch.object(self.m, "package_state_records", side_effect=lambda _: copy.deepcopy(self.records)),
            mock.patch.object(self.m, "package_version", side_effect=lambda p: next((r["version"] for r in self.records if r["package"] == p), None)),
            mock.patch.object(self.m, "held_packages", side_effect=lambda: set(self.holds)),
            mock.patch.object(self.m, "apt_mark", side_effect=self.mark),
            mock.patch.object(self.m, "run", side_effect=self.run_command),
            mock.patch.object(self.core, "strict_json", return_value=self.manifest),
            mock.patch.object(self.m.os, "uname", create=True, return_value=types.SimpleNamespace(nodename="test-node")),
            mock.patch.object(self.m.pathlib.Path, "read_text", return_value="test-boot"),
        ]
        for patch in patches:
            self.stack.enter_context(patch)

    @staticmethod
    def row(package, version, architecture):
        return dict(package=package, binary=package, version=version,
                    architecture=architecture, status="installed")

    def write(self, path, value):
        self.store[path] = copy.deepcopy(value)

    def create(self, path, value):
        self.assertNotIn(path, self.store)
        self.write(path, value)

    def mark(self, operation, packages):
        self.effects.append((operation, list(packages)))
        if operation == "hold":
            self.holds.update(packages)
        else:
            self.holds.difference_update(packages)

    def run_command(self, argv, *, input_text=None):
        if argv == ["dpkg", "--audit"]:
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv == ["dpkg", "--set-selections"]:
            for line in (input_text or "").splitlines():
                package, selection = line.split()
                self.assertEqual(selection, "deinstall")
                self.holds.discard(package)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {argv!r}")

    def prepare(self, target=None, version="2"):
        return self.m.prepare_freeze_package(self.source, "1", target or self.source,
            version, "all", "b" * 64, "candidate.json", self.manifest, self.core)

    def install(self, target=None, version="2"):
        self.records[0] = self.row(target or self.source, version, "all")
        self.holds.discard(self.source)

    def settle(self, receipt, replacement_txid=None):
        return self.m.settle_freeze_package(receipt["target_package"], receipt["target_version"],
            "b" * 64, receipt["txid"], self.manifest, self.core, replacement_txid)

    def replacement(self, receipt):
        imports = {} if sys.platform != "win32" else {"fcntl": types.ModuleType("fcntl")}
        with mock.patch.dict(sys.modules, imports):
            helper = load("freeze_test_replacement", ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-profile-replacement")
        self.stack.enter_context(mock.patch.object(self.m, "load_profile_replacement", return_value=helper))
        done = dict(schema=helper.SCHEMA, phase="DPKG_DONE", txid="c" * 32,
                    created=1, host="test-node", boot_id="test-boot",
                    source_package=self.source, source_version="1", source_flavor="dual",
                    target_package=self.target, target_version="1", target_flavor="thick-only",
                    candidate=str(helper.ROOT / "candidate.deb"), candidate_sha256="b" * 64,
                    dpkg_pid=42, dpkg_start="123", consumed=2, dpkg_completed=3, dpkg_exit=0)
        intent = {k: v for k, v in done.items() if k not in {"dpkg_completed", "dpkg_exit"}}
        intent["phase"] = "PRERM_INTENT"
        self.store[helper.DONE] = done
        self.store[helper.INTENT] = intent
        return helper, done["txid"]

    def test_cross_profile_requires_exact_successful_done_and_supports_archive_replay(self):
        receipt = self.prepare(target=self.target, version="1")
        helper, replacement_txid = self.replacement(receipt)
        self.install(target=self.target, version="1")
        settled = self.settle(receipt, replacement_txid)
        completed = self.store[self.m.LAST_PACKAGE_TRANSITION]
        self.assertEqual(completed["replacement_proof"]["txid"], replacement_txid)
        archive = dict(self.store.pop(helper.DONE), phase="COMPLETE", completed=4,
                       settlement="DPKG_EXIT_ZERO")
        self.store.pop(helper.INTENT)
        self.store[helper.ARCHIVE / f"{replacement_txid}.json"] = archive
        self.effects.clear()
        self.assertEqual(self.settle(receipt, replacement_txid), settled)
        self.assertEqual(self.effects, [])

    def test_cross_profile_releases_absent_source_hold_without_apt_resolution(self):
        receipt = self.prepare(target=self.target, version="1")
        _helper, replacement_txid = self.replacement(receipt)
        self.install(target=self.target, version="1")

        original_mark = self.mark
        def apt_mark_rejects_absent(operation, packages):
            if operation == "unhold" and self.source in packages:
                raise RuntimeError("Can't select installed nor candidate version")
            original_mark(operation, packages)

        with mock.patch.object(self.m, "apt_mark", side_effect=apt_mark_rejects_absent):
            settled = self.settle(receipt, replacement_txid)
        self.assertEqual(settled["phase"], "ACTIVE")
        self.assertNotIn(self.source, self.holds)
        # Schema 3 deliberately does not auto-hold either plugin profile.  The
        # package remains watched by the APT guard while upstream storage-stack
        # packages stay frozen.
        self.assertNotIn(self.target, self.holds)

    def test_cross_profile_rejects_missing_done_or_intent(self):
        receipt = self.prepare(target=self.target, version="1")
        helper, replacement_txid = self.replacement(receipt)
        self.install(target=self.target, version="1")
        saved = self.store.pop(helper.DONE)
        with self.assertRaisesRegex(RuntimeError, "no DPKG_DONE"):
            self.settle(receipt, replacement_txid)
        self.store[helper.DONE] = saved
        self.store.pop(helper.INTENT)
        with self.assertRaisesRegex(RuntimeError, "no consumed PRERM_INTENT"):
            self.settle(receipt, replacement_txid)
        self.assertEqual(self.effects, [])

    def test_cross_profile_recovered_target_archive_is_not_dpkg_done(self):
        receipt = self.prepare(target=self.target, version="1")
        helper, replacement_txid = self.replacement(receipt)
        self.install(target=self.target, version="1")
        self.store.pop(helper.DONE)
        archive = dict(self.store.pop(helper.INTENT), phase="COMPLETE", completed=4,
                       settlement="RECOVERED_EXACT_TARGET")
        self.store[helper.ARCHIVE / f"{replacement_txid}.json"] = archive
        with self.assertRaisesRegex(RuntimeError, "not successful DPKG_DONE"):
            self.settle(receipt, replacement_txid)
        self.assertEqual(self.effects, [])

    def test_cross_profile_rejects_foreign_done_identity(self):
        receipt = self.prepare(target=self.target, version="1")
        helper, replacement_txid = self.replacement(receipt)
        self.install(target=self.target, version="1")
        original_done = copy.deepcopy(self.store[helper.DONE])
        original_intent = copy.deepcopy(self.store[helper.INTENT])
        for key, value in (("txid", "d" * 32), ("candidate_sha256", "e" * 64),
                           ("boot_id", "other-boot"), ("host", "other-node"),
                           ("dpkg_exit", 1), ("dpkg_exit", False)):
            with self.subTest(key=key, value=value):
                self.store[helper.DONE] = dict(original_done, **{key: value})
                self.store[helper.INTENT] = copy.deepcopy(original_intent)
                if key in original_intent:
                    self.store[helper.INTENT][key] = value
                with self.assertRaises((RuntimeError, helper.Refusal)):
                    self.settle(receipt, replacement_txid)
        self.assertEqual(self.effects, [])

    def test_cross_profile_rejects_inconsistent_intent_and_done(self):
        receipt = self.prepare(target=self.target, version="1")
        helper, replacement_txid = self.replacement(receipt)
        self.install(target=self.target, version="1")
        self.store[helper.INTENT]["dpkg_start"] = "other-process"
        with self.assertRaisesRegex(helper.Refusal, "identity changed"):
            self.settle(receipt, replacement_txid)
        self.assertEqual(self.effects, [])

    def test_cross_profile_rechecks_done_during_hold_reconciliation(self):
        receipt = self.prepare(target=self.target, version="1")
        helper, replacement_txid = self.replacement(receipt)
        self.install(target=self.target, version="1")
        def drift(operation, packages):
            self.mark(operation, packages)
            self.store[helper.DONE]["dpkg_completed"] += 1
        with mock.patch.object(self.m, "apt_mark", side_effect=drift):
            with self.assertRaisesRegex(RuntimeError, "proof changed"):
                self.settle(receipt, replacement_txid)
        self.assertEqual(self.store[self.m.FREEZE]["phase"], "ROTATING")

    def test_same_profile_rejects_replacement_txid(self):
        receipt = self.prepare()
        self.install()
        with self.assertRaisesRegex(RuntimeError, "same-profile"):
            self.settle(receipt, "c" * 32)
        self.assertEqual(self.effects, [])

    def test_prepared_transition_can_abort_only_with_unchanged_frozen_source(self):
        receipt = self.prepare()
        completed = self.m.recover_prepared_freeze_source(
            receipt["txid"], self.manifest, self.core,
        )
        self.assertEqual(
            completed["state"], "SOURCE_REVALIDATED_AFTER_FAILED_TRANSACTION",
        )
        self.assertNotIn(self.m.PACKAGE_TRANSITION, self.store)
        archive = self.m.PACKAGE_RECOVERY_ARCHIVE / f'{receipt["txid"]}.json'
        self.assertIn(archive, self.store)
        before = copy.deepcopy(self.store)
        self.assertEqual(
            self.m.recover_prepared_freeze_source(
                receipt["txid"], self.manifest, self.core,
            ),
            completed,
        )
        self.assertEqual(self.store, before)

    def test_prepared_transition_abort_refuses_package_or_parent_drift(self):
        receipt = self.prepare()
        self.records[0]["version"] = "unexpected"
        with self.assertRaisesRegex(RuntimeError, "unchanged frozen baseline"):
            self.m.recover_prepared_freeze_source(
                receipt["txid"], self.manifest, self.core,
            )
        self.assertIn(self.m.PACKAGE_TRANSITION, self.store)

        self.records[0]["version"] = "1"
        self.store[self.m.FREEZE]["generation"] = "d" * 32
        with self.assertRaisesRegex(RuntimeError, "parent FREEZE ledger changed"):
            self.m.recover_prepared_freeze_source(
                receipt["txid"], self.manifest, self.core,
            )
        self.assertIn(self.m.PACKAGE_TRANSITION, self.store)

    def test_gate_recovery_is_not_prepare_or_dpkg_dispatch(self):
        # Static routing assertion only. Actual Bash/dpkg execution belongs to
        # the native package gate; these tests never claim that qualification.
        self.stack.close()
        source = (ROOT / "experiments/thick-generations/package-profile-gate.sh").read_text()
        self.assertIn('if ((settle_recovery == 0)) && [[ "$prior_update_policy" == FREEZE', source)
        self.assertIn('--artifact-sha256 "$candidate_artifact"', source)
        recovery = source.split('profile_replacement=0\n', 1)[1].split('elif [[', 1)[0]
        self.assertIn('[[ -z "$transaction_id" ]] || profile_replacement=1', recovery)
        self.assertNotIn('dpkg -i', recovery)
        self.assertNotIn('prepare-freeze-package', recovery)
        self.assertIn('--freeze-transaction-id)', source)
        self.assertIn('freeze_settle_args+=(--replacement-txid "$transaction_id")', source)

    def test_crash_after_active_publication_finishes_exact_successor(self):
        receipt = self.prepare()
        self.install()
        def crash(path, value):
            if path == self.m.LAST_PACKAGE_TRANSITION:
                raise RuntimeError("injected receipt crash")
            self.write(path, value)
        with mock.patch.object(self.m, "atomic_json", side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.settle(receipt)
        published = copy.deepcopy(self.store[self.m.FREEZE])
        self.assertEqual(published["phase"], "ACTIVE")
        self.assertEqual(self.settle(receipt), published)
        self.assertNotIn(self.m.PACKAGE_TRANSITION, self.store)

    def test_completed_retry_is_read_only_and_exact(self):
        receipt = self.prepare()
        self.install()
        settled = self.settle(receipt)
        before = copy.deepcopy(self.store)
        self.effects.clear()
        self.assertEqual(self.settle(receipt), settled)
        self.assertEqual(self.store, before)
        self.assertEqual(self.effects, [])
        self.records[1]["version"] = "unexpected"
        with self.assertRaisesRegex(RuntimeError, "exact successor"):
            self.settle(receipt)

    def test_same_version_reinstall_migrates_plugin_out_of_auto_holds(self):
        receipt = self.prepare(version="1")
        self.install(version="1")
        settled = self.settle(receipt)
        self.assertEqual(settled["baseline"], self.records)
        self.assertEqual(settled["schema"], 3)
        self.assertEqual(settled["hold_targets"], ["qemu-server"])
        self.assertNotIn(self.source, self.holds)
        self.assertIn("qemu-server", self.holds)

    def test_target_architecture_drift_refuses_before_hold_changes(self):
        receipt = self.prepare()
        self.install()
        self.records[0]["architecture"] = "amd64"
        with self.assertRaisesRegex(RuntimeError, "prepared identity"):
            self.settle(receipt)
        self.assertEqual(self.effects, [])

    def test_admin_held_cross_profile_source_refused_before_prepare(self):
        self.store[self.m.FREEZE]["owned"] = ["qemu-server"]
        self.store[self.m.FREEZE]["preexisting"] = [self.source]
        with self.assertRaisesRegex(RuntimeError, "administrator-held"):
            self.prepare(target=self.target)
        self.assertNotIn(self.m.PACKAGE_TRANSITION, self.store)
        self.assertEqual(self.effects, [])

    def test_existing_old_receipt_cannot_discard_admin_held_source(self):
        receipt = self.prepare(target=self.target)
        ledger = self.store[self.m.FREEZE]
        ledger["owned"] = ["qemu-server"]
        ledger["preexisting"] = [self.source]
        # Model a receipt created by the previous implementation, which did
        # not reject this authorized-parent shape at preparation time.
        self.store[self.m.PACKAGE_TRANSITION]["parent_ledger_sha256"] = self.core.digest(ledger)
        self.install(target=self.target)
        with self.assertRaisesRegex(RuntimeError, "administrator-held"):
            self.settle(receipt)
        self.assertEqual(self.effects, [])

    def test_crash_before_rotating_ledger_can_retry(self):
        receipt = self.prepare()
        self.install()
        def crash(path, value):
            if path == self.m.FREEZE and value["phase"] == "ROTATING":
                raise RuntimeError("injected ledger crash")
            self.write(path, value)
        with mock.patch.object(self.m, "atomic_json", side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.settle(receipt)
        self.assertEqual(self.settle(receipt)["phase"], "ACTIVE")

    def test_rotating_hold_failure_can_retry(self):
        receipt = self.prepare()
        self.install()
        with mock.patch.object(self.m, "apt_mark", side_effect=RuntimeError("injected hold failure")):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.settle(receipt)
        self.assertEqual(self.store[self.m.FREEZE]["phase"], "ROTATING")
        self.assertEqual(self.settle(receipt)["phase"], "ACTIVE")

    def test_completed_retry_rejects_lost_hold(self):
        receipt = self.prepare()
        self.install()
        self.settle(receipt)
        self.holds.remove("qemu-server")
        with self.assertRaisesRegex(RuntimeError, "integrity"):
            self.settle(receipt)


if __name__ == "__main__":
    unittest.main()
