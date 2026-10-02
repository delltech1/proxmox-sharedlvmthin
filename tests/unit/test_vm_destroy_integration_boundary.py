"""Isolated promotion/IPC tests, NOT certification of a production dispatcher.

No DEB is installed, no PVE module is loaded, and no VM/storage action exists
here. Staged dispatcher bytes come from the installed payload; placement
tests prove identity coverage only, not launcher safety or runtime authority.
"""

import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
LIBEXEC = "usr/libexec/pve-sharedlvmthin/"
DISPATCH_MODULE = "usr/share/perl5/PVE/SharedLvmThinVMDestroyDispatcher.pm"
SOURCE_MODULE = DISPATCH_MODULE
SOURCE_LAUNCHER = LIBEXEC + "sharedlvmthin-vm-destroy-dispatch"
TXID = "a" * 32


def load(name, relative):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / relative))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


artifact = load("vm_destroy_integration_artifact", "scripts/package-artifact-identity.py")
journal = load("vm_destroy_integration_journal", LIBEXEC + "sharedlvmthin-vm-destroy")
recovery = load("vm_destroy_integration_recovery", LIBEXEC + "sharedlvmthin-vm-destroy-recovery")


def core():
    # Deliberately only a substrate fixture: this is not runtime qualification.
    return {"schema": recovery.CORE_SCHEMA, "receipt": {
        "schema": 1, "txid": TXID, "vmid": 123,
        "config": {"digest": "b" * 40},
        "runtime": {"status": "QUALIFIED", "boot_id": "fixture-only"},
        "plan": {"authority": "NONE", "objects": ["fixture-only"]}}}


class PromotionIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)

    def stage(self, profile):
        stage = self.base / profile
        files = {
            DISPATCH_MODULE: ROOT / SOURCE_MODULE,
            LIBEXEC + "sharedlvmthin-vm-destroy-dispatch": ROOT / SOURCE_LAUNCHER,
            LIBEXEC + "sharedlvmthin-vm-destroy": ROOT / (LIBEXEC + "sharedlvmthin-vm-destroy"),
            LIBEXEC + "sharedlvmthin-vm-destroy-recovery": ROOT / (LIBEXEC + "sharedlvmthin-vm-destroy-recovery"),
            LIBEXEC + "sharedlvmthin-vm-destroy-state-bootstrap": ROOT / (LIBEXEC + "sharedlvmthin-vm-destroy-state-bootstrap"),
            "DEBIAN/postinst": ROOT / "DEBIAN/postinst",
        }
        for path in (ROOT / "usr/share/perl5/PVE").glob("SharedLvmThinVMDestroy*.pm"):
            files[path.relative_to(ROOT).as_posix()] = path
        for relative, source in files.items():
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        marker = stage / "usr/share/pve-sharedlvmthin/package-flavor"
        marker.parent.mkdir(parents=True)
        marker.write_text(profile + "\n", encoding="ascii")
        return stage, files

    def digest(self, stage):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            artifact.main(["artifact", str(stage), str(stage / "DEBIAN")])
        result = output.getvalue().strip()
        self.assertRegex(result, r"^[0-9a-f]{64}$")
        return result

    def test_every_staged_boundary_component_changes_artifact_in_both_profiles(self):
        for profile in ("dual", "thick-only"):
            stage, files = self.stage(profile)
            baseline = self.digest(stage)
            for relative in sorted(files):
                with self.subTest(profile=profile, component=relative):
                    path = stage / relative
                    original = path.read_bytes()
                    path.write_bytes(original + b"\n# identity sensitivity fixture\n")
                    self.assertNotEqual(baseline, self.digest(stage))
                    path.write_bytes(original)
                    self.assertEqual(baseline, self.digest(stage))

    def test_profiles_have_distinct_identity_but_identical_dispatcher_bytes(self):
        dual, _ = self.stage("dual")
        thick, _ = self.stage("thick-only")
        self.assertNotEqual(self.digest(dual), self.digest(thick))
        self.assertEqual((dual / DISPATCH_MODULE).read_bytes(), (thick / DISPATCH_MODULE).read_bytes())

    def test_promoted_dispatcher_is_in_existing_perl_runtime_hash_domain(self):
        stage, _ = self.stage("dual")

        def runtime_digest():
            paths = sorted((stage / "usr/share/perl5/PVE").rglob("*.pm"))
            rows = [hashlib.sha256(path.read_bytes()).hexdigest() + "  " +
                    path.relative_to(stage).as_posix() + "\n" for path in paths]
            return hashlib.sha256("".join(rows).encode("ascii")).hexdigest()

        before = runtime_digest()
        path = stage / DISPATCH_MODULE
        path.write_bytes(path.read_bytes() + b"\n# changed dispatcher\n")
        self.assertNotEqual(before, runtime_digest())

    def test_isolated_python_helper_rejects_public_path_injection_without_loading_site_hook(self):
        # This tests the required -I invocation, not current Perl bridge wiring.
        injected = self.base / "python-path"
        injected.mkdir()
        marker = self.base / "site-hook-ran"
        (injected / "sitecustomize.py").write_text(
            "from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\n", encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=str(injected), PYTHONHOME=str(injected))
        for name in ("sharedlvmthin-vm-destroy", "sharedlvmthin-vm-destroy-recovery"):
            result = subprocess.run([sys.executable, "-I", str(ROOT / (LIBEXEC + name)),
                                     "--base", str(self.base)], env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(marker.exists())


@unittest.skipUnless(os.name == "posix", "requires real POSIX dirfd/flock/fsync semantics")
class DurableBoundaryIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.journal_root = self.base / "journal"
        self.descriptor_root = self.base / "descriptor"
        for path in (self.journal_root, self.descriptor_root):
            path.mkdir(mode=0o700)
        self.context = core()
        self.store = recovery.Store(self.descriptor_root, expected_uid=os.geteuid())

    def descriptor(self):
        value = {"schema": recovery.SCHEMA, "authority": "NONE", "txid": TXID,
                 "context": self.context, "context_sha256": recovery.digest(self.context)}
        return recovery.canonical(value) + b"\n"

    def append(self, stage, previous):
        return journal.append_request({"schema": journal.APPEND_SCHEMA,
            "authority": "NONE", "request_id": TXID, "stage": stage,
            "expected_previous": previous, "context": self.context,
            "evidence": {"fixture_only": True}}, base=self.journal_root,
            expected_uid=os.geteuid())

    def snapshot(self):
        return {p.relative_to(self.base).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.base.rglob("*") if p.is_file()}

    def test_descriptor_then_full_journal_share_exact_context_and_observe_does_not_write(self):
        result = self.store.create(TXID, self.descriptor())
        self.assertIs(result["durable"], True)
        previous = None
        for stage in journal.STAGES:
            self.append(stage, previous)
            observed = journal.Journal(self.journal_root / TXID,
                                       expected_uid=os.geteuid()).observe(durable=True)
            previous = observed["last_sha256"]
            self.assertEqual(observed["stage"], stage)
            self.assertEqual(observed["authority"], "NONE")
            self.assertTrue(all(row["context"] == self.context for row in observed["records"]))
        before = self.snapshot()
        self.assertEqual(self.store.read(TXID), result)
        journal.Journal(self.journal_root / TXID, expected_uid=os.geteuid()).observe()
        self.assertEqual(before, self.snapshot())

    def test_orphan_descriptor_is_not_adopted_or_turned_into_execution(self):
        self.store.create(TXID, self.descriptor())
        before = self.snapshot()
        with self.assertRaises(FileExistsError):
            self.store.create(TXID, self.descriptor())
        self.assertEqual(before, self.snapshot())
        self.assertEqual(list(self.journal_root.iterdir()), [])

    def test_unsafe_existing_roots_are_never_repaired_or_adopted(self):
        self.journal_root.chmod(0o755)
        self.descriptor_root.chmod(0o755)
        with self.assertRaises(journal.Refusal):
            self.append("PREPARED", None)
        with self.assertRaises(recovery.Refusal):
            self.store.create(TXID, self.descriptor())
        for path in (self.journal_root, self.descriptor_root):
            self.assertEqual(path.stat().st_mode & 0o777, 0o755)
            self.assertEqual(list(path.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
