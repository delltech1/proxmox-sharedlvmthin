import copy
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
EXP = ROOT / "experiments/thick-generations"
sys.path.insert(0, str(EXP))

LAB = importlib.import_module("prelive_v2_intent_file_bridge")
from tests.unit import test_prelive_launcher_identity_consumer as FIXTURE


class V2IntentFileBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="slt-v2-intent-")
        self.patch = mock.patch.object(LAB.FILES, "PARENT", self.temp.name)
        self.patch.start()
        self.backends = []

    def tearDown(self):
        for backend in self.backends:
            if backend.root_fd is not None or backend.parent_fd is not None:
                try: backend.release_descriptors_preserving_state()
                except BaseException: pass
        self.patch.stop()
        self.temp.cleanup()

    def event_bytes(self):
        value, binding, backend, chain = FIXTURE.chain_ready()
        return chain.persist_intent().event_bytes

    def backend(self, nonce="d" * 32):
        backend = LAB.FILES.create_fresh_file_backend(nonce)
        self.backends.append(backend)
        return backend

    def test_model_intent_bytes_are_persisted_and_independently_verified(self):
        raw = self.event_bytes()
        backend = self.backend()
        bridge = LAB.create_v2_intent_file_bridge(backend)
        receipt = bridge.persist_intent(raw)
        path = Path(backend.artifact_path()) / "exact-event-000001.json"
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(receipt["classification"],
                         "V2_INTENT_EXACT_BYTES_VERIFIED")
        self.assertFalse(receipt["child_started"])
        self.assertFalse(receipt["grant_attempted"])
        self.assertFalse(receipt["runtime_authorized"])
        self.assertFalse(receipt["storage_authorized"])
        self.assertFalse(receipt["exec_proven"])
        self.assertEqual(bridge.state, "SEALED_INTENT")

    def test_noncanonical_or_non_intent_refuses_before_file_creation(self):
        raw = self.event_bytes()
        for index, candidate in enumerate((
                json.dumps(json.loads(raw), sort_keys=True).encode("ascii"),
                raw.replace(b'"INTENT"', b'"OTHER"', 1)), 1):
            backend = self.backend("%032x" % index)
            bridge = LAB.create_v2_intent_file_bridge(backend)
            with self.assertRaises(BaseException): bridge.persist_intent(candidate)
            self.assertEqual(bridge.state, "UNKNOWN")
            self.assertEqual(backend.persisted_records, [])

    def test_ack_drift_is_terminal_and_not_retried(self):
        raw = self.event_bytes(); backend = self.backend()
        bridge = LAB.create_v2_intent_file_bridge(backend)
        real = backend.persist_exact_file
        calls = []
        def drift(name, data):
            calls.append((name, data))
            ack = real(name, data)
            ack["sha256"] = "f" * 64
            return ack
        with mock.patch.object(backend, "persist_exact_file", side_effect=drift):
            with self.assertRaises(BaseException): bridge.persist_intent(raw)
        self.assertEqual(len(calls), 1)
        self.assertEqual(bridge.state, "UNKNOWN")
        self.assertEqual(len(backend.persisted_records), 1)

    def test_ack_custom_scalar_refuses_without_callback(self):
        raw = self.event_bytes(); backend = self.backend(); calls = []
        bridge = LAB.create_v2_intent_file_bridge(backend)
        real = backend.persist_exact_file
        class EvilStr(str):
            def __eq__(self, other): calls.append("eq"); return True
            def __deepcopy__(self, memo): calls.append("copy"); return str(self)
        def drift(name, data):
            ack = real(name, data)
            ack["sha256"] = EvilStr(ack["sha256"])
            return ack
        with mock.patch.object(backend, "persist_exact_file", side_effect=drift):
            with self.assertRaises(BaseException): bridge.persist_intent(raw)
        self.assertEqual(calls, [])
        self.assertEqual(bridge.state, "UNKNOWN")

    def test_ack_numeric_aliases_refuse(self):
        for index, replacement in enumerate((True, 1.0), 10):
            raw = self.event_bytes(); backend = self.backend("%032x" % index)
            bridge = LAB.create_v2_intent_file_bridge(backend)
            real = backend.persist_exact_file
            def drift(name, data, replacement=replacement):
                ack = real(name, data)
                ack["record_identity"]["inode"] = replacement
                return ack
            with mock.patch.object(backend, "persist_exact_file",
                                   side_effect=drift):
                with self.assertRaises(BaseException): bridge.persist_intent(raw)
            self.assertEqual(bridge.state, "UNKNOWN")

    def test_persist_callback_cannot_rebind_root_authority(self):
        raw = self.event_bytes(); backend = self.backend()
        bridge = LAB.create_v2_intent_file_bridge(backend)
        real = backend.persist_exact_file
        root = Path(backend.artifact_path())
        displaced = root.with_name(root.name + ".old")
        def replace_root(name, data):
            ack = real(name, data)
            root.rename(displaced); root.mkdir(mode=0o700)
            (displaced / name).rename(root / name)
            info = root.stat()
            backend.root_identity = {
                "dev": info.st_dev, "inode": info.st_ino,
                "uid": info.st_uid, "mode": 0o700}
            return ack
        with mock.patch.object(backend, "persist_exact_file",
                               side_effect=replace_root):
            with self.assertRaises(BaseException): bridge.persist_intent(raw)
        self.assertEqual(bridge.state, "UNKNOWN")

    def test_backend_substitution_refuses_before_persist(self):
        raw = self.event_bytes(); original = self.backend("a" * 32)
        replacement = self.backend("b" * 32)
        bridge = LAB.create_v2_intent_file_bridge(original)
        bridge.backend = replacement
        with self.assertRaises(BaseException): bridge.persist_intent(raw)
        self.assertEqual(original.persisted_records, [])
        self.assertEqual(replacement.persisted_records, [])
        self.assertEqual(bridge.state, "UNKNOWN")

    def test_persist_callback_cannot_rebind_root_name(self):
        raw = self.event_bytes(); backend = self.backend()
        bridge = LAB.create_v2_intent_file_bridge(backend)
        real = backend.persist_exact_file
        root = Path(backend.artifact_path())
        displaced = root.with_name(root.name + ".moved")
        def rename_root(name, data):
            ack = real(name, data)
            root.rename(displaced)
            backend.root_name = displaced.name
            return ack
        with mock.patch.object(backend, "persist_exact_file",
                               side_effect=rename_root):
            with self.assertRaises(BaseException): bridge.persist_intent(raw)
        self.assertEqual(bridge.state, "UNKNOWN")

    def test_post_construction_artifact_path_callback_is_never_used(self):
        raw = self.event_bytes(); backend = self.backend(); calls = []
        bridge = LAB.create_v2_intent_file_bridge(backend)
        def reenter():
            calls.append("path")
            try: bridge.persist_intent(raw)
            except BaseException: pass
            return bridge._artifact_path
        with mock.patch.object(backend, "artifact_path", side_effect=reenter):
            receipt = bridge.persist_intent(raw)
        self.assertEqual(calls, [])
        self.assertEqual(receipt["classification"],
                         "V2_INTENT_EXACT_BYTES_VERIFIED")
        self.assertEqual(bridge.state, "SEALED_INTENT")

    def test_post_ack_mutation_cannot_change_frozen_receipt(self):
        raw = self.event_bytes(); backend = self.backend(); calls = []
        bridge = LAB.create_v2_intent_file_bridge(backend)
        real_persist = backend.persist_exact_file; returned = {}
        real_read = LAB.os.read; fired = []
        class EvilInt(int):
            def __deepcopy__(self, memo): calls.append("copy"); return int(self)
        def persist(name, data):
            ack = real_persist(name, data); returned["ack"] = ack; return ack
        def mutate(fd, count):
            data = real_read(fd, count)
            if data and not fired:
                fired.append(True)
                returned["ack"]["record_identity"]["inode"] = EvilInt(1)
                returned["ack"]["name"] = "changed"
            return data
        with mock.patch.object(backend, "persist_exact_file",
                               side_effect=persist), \
                mock.patch.object(LAB.os, "read", side_effect=mutate):
            receipt = bridge.persist_intent(raw)
        self.assertEqual(calls, [])
        self.assertNotEqual(receipt["record_identity"]["inode"], 1)
        self.assertEqual(receipt["name"], "exact-event-000001.json")
        self.assertEqual(bridge.state, "SEALED_INTENT")

    def test_record_replacement_during_reread_is_unknown(self):
        raw = self.event_bytes(); backend = self.backend()
        bridge = LAB.create_v2_intent_file_bridge(backend)
        real_read = LAB.os.read; fired = []
        record = Path(backend.artifact_path()) / "exact-event-000001.json"
        def replace(fd, count):
            data = real_read(fd, count)
            if data and not fired:
                fired.append(True)
                record.unlink(); record.write_bytes(raw); record.chmod(0o600)
            return data
        with mock.patch.object(LAB.os, "read", side_effect=replace):
            with self.assertRaises(BaseException): bridge.persist_intent(raw)
        self.assertEqual(bridge.state, "UNKNOWN")

    def test_root_mode_or_namespace_drift_during_reread_is_unknown(self):
        for index, attack in enumerate(("mode", "replace"), 20):
            raw = self.event_bytes(); backend = self.backend("%032x" % index)
            bridge = LAB.create_v2_intent_file_bridge(backend)
            real_read = LAB.os.read; fired = []
            root = Path(backend.artifact_path())
            displaced = root.with_name(root.name + ".old")
            def mutate(fd, count, attack=attack):
                data = real_read(fd, count)
                if data and not fired:
                    fired.append(True)
                    if attack == "mode": root.chmod(0o755)
                    else:
                        root.rename(displaced)
                        root.mkdir(mode=0o700)
                        (displaced / "exact-event-000001.json").rename(
                            root / "exact-event-000001.json")
                return data
            with mock.patch.object(LAB.os, "read", side_effect=mutate):
                with self.assertRaises(BaseException): bridge.persist_intent(raw)
            self.assertEqual(bridge.state, "UNKNOWN")

    def test_foreign_thread_refuses_without_record(self):
        raw = self.event_bytes(); backend = self.backend()
        bridge = LAB.create_v2_intent_file_bridge(backend); errors = []
        worker = threading.Thread(target=lambda: self._foreign(
            bridge, raw, errors))
        worker.start(); worker.join()
        self.assertEqual(len(errors), 1)
        self.assertEqual(bridge.state, "UNKNOWN")
        self.assertEqual(backend.persisted_records, [])

    def test_source_has_no_process_grant_or_storage_surface(self):
        source = (EXP / "prelive_v2_intent_file_bridge.py").read_text()
        for forbidden in ("os.fork", "execve", "write_grant", "dmsetup",
                          "lvcreate", "pvesm", "subprocess"):
            self.assertNotIn(forbidden, source)
        self.assertNotIn("model_only_v2_journal_backend", source)

    @staticmethod
    def _foreign(bridge, raw, errors):
        try: bridge.persist_intent(raw)
        except BaseException as exc: errors.append(exc)


if __name__ == "__main__":
    unittest.main()
