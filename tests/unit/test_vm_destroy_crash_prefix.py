import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
JOURNAL_SOURCE = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-vm-destroy"
DESCRIPTOR_SOURCE = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-vm-destroy-recovery"
TXID = "a" * 32


def load_source(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


journal_mod = load_source("vm_destroy_journal_crash", JOURNAL_SOURCE)
descriptor_mod = load_source("vm_destroy_descriptor_crash", DESCRIPTOR_SOURCE)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def core_context():
    return {
        "schema": "sharedlvmthin-vm-destroy-executor-core/v1",
        "receipt": {
            "schema": 1,
            "txid": TXID,
            "vmid": 101,
            "config": {"digest": "c" * 40},
            "runtime": {"status": "QUALIFIED"},
            "plan": {"schema": "fixture-plan/v1"},
        },
    }


def descriptor_value():
    context = core_context()
    return {
        "schema": descriptor_mod.SCHEMA,
        "authority": "NONE",
        "txid": TXID,
        "context": context,
        "context_sha256": digest(context),
    }


def append_payload(stage, previous, evidence=None):
    return {
        "schema": journal_mod.APPEND_SCHEMA,
        "authority": "NONE",
        "request_id": TXID,
        "stage": stage,
        "expected_previous": previous,
        "context": core_context(),
        "evidence": evidence or {"evidence": {"boundary": stage}},
    }


def fresh_observe(journal_root, descriptor_root):
    code = r'''
import importlib.machinery, importlib.util, json, os, pathlib, sys
def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod
j = load("fresh_journal", sys.argv[1]); d = load("fresh_descriptor", sys.argv[2])
txid = sys.argv[5]
out = {"descriptor": d.Store(pathlib.Path(sys.argv[4]), expected_uid=os.geteuid()).read(txid)}
out["journal"] = j.Journal(pathlib.Path(sys.argv[3]) / txid, expected_uid=os.geteuid()).observe(durable=True)
print(json.dumps(out, sort_keys=True, separators=(",", ":")))
'''
    result = subprocess.run(
        [sys.executable, "-c", code, str(JOURNAL_SOURCE), str(DESCRIPTOR_SOURCE),
         str(journal_root), str(descriptor_root), TXID],
        check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return json.loads(result.stdout)


@unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0,
                     "root POSIX durability semantics required")
class VMDestroyCrashPrefixIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.journal_root = self.base / "journal"
        self.descriptor_root = self.base / "descriptor"
        self.journal_root.mkdir(mode=0o700)
        self.descriptor_root.mkdir(mode=0o700)

    def tearDown(self):
        self.temp.cleanup()

    def publish_descriptor(self, fault=None):
        store = descriptor_mod.Store(self.descriptor_root, expected_uid=0, fault=fault)
        return store.create(TXID, canonical(descriptor_value()) + b"\n")

    def append(self, stage, previous, fault=None):
        return journal_mod.append_request(
            append_payload(stage, previous), base=self.journal_root,
            expected_uid=0, fault=fault,
        )

    def durable_prefix(self, stages):
        self.publish_descriptor()
        previous = None
        for stage in stages:
            previous = self.append(stage, previous)["last_sha256"]
        return previous

    def test_descriptor_crash_prefix_is_durable_or_permanent_blocker(self):
        for point, readable in (("after_create", False), ("after_file_fsync", True),
                                ("after_directory_fsync", True)):
            with self.subTest(point=point):
                self.tearDown(); self.setUp()
                def fault(at):
                    if at == point:
                        raise RuntimeError("injected crash")
                with self.assertRaises(RuntimeError):
                    self.publish_descriptor(fault)
                fresh = descriptor_mod.Store(self.descriptor_root, expected_uid=0)
                if readable:
                    self.assertEqual(fresh.read(TXID)["descriptor"], descriptor_value())
                else:
                    with self.assertRaises(Exception):
                        fresh.read(TXID)
                with self.assertRaises(FileExistsError):
                    self.publish_descriptor()

    def test_prepared_and_dispatched_publication_crashes_never_adopt_or_replay(self):
        cases = [
            ("PREPARED", [], "after_request_mkdir", "EMPTY"),
            ("PREPARED", [], "after_request_parent_fsync", "EMPTY"),
            ("PREPARED", [], "after_create", "ERROR"),
            ("PREPARED", [], "after_file_fsync", "PREPARED"),
            ("PREPARED", [], "after_directory_fsync", "PREPARED"),
            ("DISPATCHED", ["PREPARED"], "after_create", "ERROR"),
            ("DISPATCHED", ["PREPARED"], "after_file_fsync", "DISPATCHED"),
            ("DISPATCHED", ["PREPARED"], "after_directory_fsync", "DISPATCHED"),
        ]
        for stage, prefix, point, expected in cases:
            with self.subTest(stage=stage, point=point):
                self.tearDown(); self.setUp()
                previous = self.durable_prefix(prefix)
                def fault(at):
                    if at == point:
                        raise RuntimeError("injected crash")
                with self.assertRaises(RuntimeError):
                    self.append(stage, previous, fault)
                observer = journal_mod.Journal(self.journal_root / TXID, expected_uid=0)
                if expected == "ERROR":
                    with self.assertRaises(Exception):
                        observer.observe(durable=True)
                else:
                    observed = observer.observe(durable=True)
                    self.assertEqual(observed["stage"], expected)
                    if expected == "EMPTY":
                        self.assertEqual(observed["records"], [])
                        self.assertIsNone(observed["last_sha256"])
                        fresh = fresh_observe(self.journal_root, self.descriptor_root)
                        self.assertEqual(fresh["journal"]["stage"], "EMPTY")
                        self.assertEqual(fresh["journal"]["records"], [])
                # The same stage cannot be retried: create-only request/record
                # publication converts lost acknowledgement into refusal.
                with self.assertRaises(Exception):
                    self.append(stage, previous)

    def test_every_effect_prefix_fresh_observation_has_zero_redispatch_authority(self):
        stages = ["PREPARED", "DISPATCHED"]
        self.durable_prefix(stages)
        native = self.base / "mock-native-count"
        native.write_text("1\n", encoding="ascii")
        os.chmod(native, 0o600)
        with native.open("rb") as stream:
            os.fsync(stream.fileno())

        for stage in ("DISPATCHED", "STORAGE_ABSENT", "FINALIZING", "COMPLETE"):
            if stage != "DISPATCHED":
                head = journal_mod.Journal(self.journal_root / TXID, expected_uid=0).observe(durable=True)
                self.append(stage, digest(head["records"][-1]))
            for _restart in range(2):
                observed = fresh_observe(self.journal_root, self.descriptor_root)
                self.assertEqual(observed["journal"]["stage"], stage)
                self.assertEqual(observed["descriptor"]["authority"], "NONE")
                self.assertEqual(observed["journal"]["authority"], "NONE")
                self.assertEqual(native.read_text(encoding="ascii"), "1\n")

    def test_finalization_effect_prefixes_remain_observable_without_native_replay(self):
        self.durable_prefix(["PREPARED", "DISPATCHED", "STORAGE_ABSENT", "FINALIZING"])
        native = self.base / "mock-native-count"
        native.write_text("1\n", encoding="ascii")
        effects = self.base / "mock-finalization.json"
        state = {"acl": "PRESENT", "firewall": "PRESENT", "config": "PRESENT"}
        for effect in ("acl", "firewall", "config"):
            state[effect] = "ABSENT"
            effects.write_bytes(canonical(state) + b"\n")
            with effects.open("rb") as stream:
                os.fsync(stream.fileno())
            observed = fresh_observe(self.journal_root, self.descriptor_root)
            self.assertEqual(observed["journal"]["stage"], "FINALIZING")
            self.assertEqual(json.loads(effects.read_text(encoding="ascii")), state)
            self.assertEqual(native.read_text(encoding="ascii"), "1\n")

    @unittest.skipUnless(shutil.which("perl"), "Perl dispatcher checkpoint unavailable")
    def test_real_dispatcher_fault_boundary_checkpoint(self):
        # This is deliberately the real source-only dispatcher test, not a
        # second Python state-machine imitation. Together with the helper
        # crash-prefix cases above it pins both sides of the private IPC seam.
        result = subprocess.run(
            ["perl", str(ROOT / "tests/unit/vm_destroy_dispatcher.t")],
            cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
