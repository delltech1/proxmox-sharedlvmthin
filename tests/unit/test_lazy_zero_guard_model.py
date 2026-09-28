import copy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "experiments/thick-generations/lazy_zero_guard_model.py"
SPEC = importlib.util.spec_from_file_location("lazy_zero_guard_model", SCRIPT)
LAB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAB)


def fixture(purpose="GUARDED"):
    common = {"diskseq": 7, "holders": ["253:2"]}
    data = {**common, "kind": "loop", "role": "data", "devno": "7:1",
            "size_bytes": 134217728, "backing_dev": 20, "backing_inode": 31,
            "offset": 0, "sizelimit": 0, "readonly": False}
    metadata = {**common, "kind": "loop", "role": "metadata", "devno": "7:2",
                "size_bytes": 33554432, "backing_dev": 20, "backing_inode": 32,
                "offset": 0, "sizelimit": 0, "readonly": False}
    zero = {**common, "kind": "dm", "role": "zero", "devno": "253:1",
            "size_bytes": 134217728, "name": "slt-lazy-zero-" + "a" * 32,
            "uuid": "SLT-LAZY-ZERO-" + "a" * 32,
            "table": "0 262144 zero", "readonly": True, "suspended": False,
            "open_count": 0, "dependencies": []}
    table = "0 262144 clone 7:2 7:1 253:1 2048 2 no_hydration no_discard_passdown"
    clone = {**common, "kind": "dm", "role": "clone", "devno": "253:2",
             "size_bytes": 134217728, "name": "slt-lazy-clone-" + "a" * 32,
             "uuid": "SLT-LAZY-CLONE-" + "a" * 32,
             "table": table, "readonly": False, "suspended": False,
             "open_count": 0, "holders": [],
             "dependencies": ["7:1", "7:2", "253:1"]}
    return {"schema": 1, "nonce": "a" * 32,
            "boot_id": "12345678-1234-1234-1234-123456789abc",
            "purpose": purpose, "region_sectors": 2048,
            "roles": {"data": data, "metadata": metadata,
                      "zero": zero, "clone": clone}}


def receipt(model):
    clone = model.fixture["roles"]["clone"]
    return {"schema": 1, "fixture_nonce": model.fixture["nonce"],
            "boot_id": model.fixture["boot_id"], "epoch": model.epoch,
            "graph_digest": model.digest, "clone_devno": clone["devno"],
            "clone_diskseq": clone["diskseq"], "clone_table": clone["table"],
            "queue_devno": clone["devno"], "queue_diskseq": clone["diskseq"],
            "discard_max_bytes": 0, "attempt_id": "b" * 32}


class GuardEpochTests(unittest.TestCase):
    def test_exact_graph_and_guard_publish_only_model_result(self):
        model = LAB.GuardEpochModel(fixture())
        model.accept_guard(receipt(model))
        result = model.publish()
        self.assertEqual(result["classification"], "MODEL_GUARD_PUBLICATION_ORDER_VALID")
        self.assertTrue(all(value is False for key, value in result.items()
                            if key != "classification"))

    def test_semantically_wrong_graphs_refuse(self):
        mutations = []
        bad = fixture(); bad["roles"]["clone"]["table"] = "0 262144 zero"; mutations.append(bad)
        bad = fixture(); bad["roles"]["zero"]["readonly"] = False; mutations.append(bad)
        bad = fixture(); bad["roles"]["metadata"]["size_bytes"] = 134217728; mutations.append(bad)
        bad = fixture(); bad["roles"]["data"]["devno"] = "7:2"; mutations.append(bad)
        for value in mutations:
            with self.assertRaises(LAB.Refusal):
                LAB.GuardEpochModel(value)

    def test_every_guard_binding_mismatch_poisons(self):
        fields = {"fixture_nonce": "c" * 32, "boot_id": "other",
                  "epoch": 2, "graph_digest": "0" * 64,
                  "clone_devno": "253:9", "clone_diskseq": 9,
                  "clone_table": "wrong", "queue_devno": "253:9",
                  "queue_diskseq": 9, "discard_max_bytes": 1,
                  "attempt_id": "invalid"}
        for key, value in fields.items():
            model = LAB.GuardEpochModel(fixture())
            bad = receipt(model); bad[key] = value
            with self.assertRaises(LAB.Refusal):
                model.accept_guard(bad)
            self.assertEqual(model.state, "UNKNOWN")

    def test_old_guard_refused_after_same_identity_reload(self):
        model = LAB.GuardEpochModel(fixture())
        old = receipt(model)
        model.accept_guard(old); model.publish(); model.withdraw(True, 0)
        model.reconfigure("RELOAD", fixture(), True, 0)
        self.assertEqual(model.epoch, 2)
        with self.assertRaises(LAB.Refusal):
            model.accept_guard(old)
        self.assertEqual(model.state, "UNKNOWN")

    def test_new_guard_after_stopped_reload_is_accepted(self):
        model = LAB.GuardEpochModel(fixture())
        model.accept_guard(receipt(model)); model.publish(); model.withdraw(True, 0)
        model.reconfigure("RESUME", fixture(), True, 0)
        model.accept_guard(receipt(model))
        self.assertEqual(model.publish()["classification"],
                         "MODEL_GUARD_PUBLICATION_ORDER_VALID")

    def test_reload_requires_withdrawal_and_terminal_zero_opens(self):
        model = LAB.GuardEpochModel(fixture())
        with self.assertRaises(LAB.Refusal):
            model.reconfigure("RELOAD", fixture(), True, 0)
        model.accept_guard(receipt(model)); model.publish()
        with self.assertRaises(LAB.Refusal):
            model.reconfigure("RELOAD", fixture(), True, 0)
        with self.assertRaises(LAB.Refusal):
            model.withdraw(True, 1)

    def test_negative_l2_fixture_never_publishes(self):
        model = LAB.GuardEpochModel(fixture("NEGATIVE_L2"))
        model.accept_guard(receipt(model))
        with self.assertRaisesRegex(LAB.Refusal, "negative"):
            model.publish()

    def test_reopen_requires_same_metadata_backing_identity(self):
        model = LAB.GuardEpochModel(fixture())
        model.accept_guard(receipt(model)); model.publish(); model.withdraw(True, 0)
        changed = fixture(); changed["roles"]["metadata"]["backing_inode"] = 999
        with self.assertRaisesRegex(LAB.Refusal, "metadata"):
            model.reconfigure("REOPEN", changed, True, 0)
        self.assertEqual(model.state, "UNKNOWN")

    def test_reconfigure_cannot_replace_fixture_or_backing_graph(self):
        mutations = []
        bad = fixture(); bad["nonce"] = "c" * 32; mutations.append(("RELOAD", bad))
        bad = fixture(); bad["boot_id"] = "87654321-1234-1234-1234-123456789abc"; mutations.append(("RESUME", bad))
        bad = fixture(); bad["purpose"] = "NEGATIVE_L2"; mutations.append(("RELOAD", bad))
        bad = fixture(); bad["roles"]["data"]["backing_inode"] = 888; mutations.append(("REOPEN", bad))
        bad = fixture(); bad["roles"]["zero"]["diskseq"] = 8; mutations.append(("REOPEN", bad))
        for kind, changed in mutations:
            model = LAB.GuardEpochModel(fixture())
            model.accept_guard(receipt(model)); model.publish(); model.withdraw(True, 0)
            with self.assertRaises(LAB.Refusal):
                model.reconfigure(kind, changed, True, 0)
            self.assertEqual(model.state, "UNKNOWN")

    def test_fixture_exact_types_and_holder_policy_refuse(self):
        mutations = []
        bad = fixture(); bad["schema"] = True; mutations.append(bad)
        bad = fixture(); bad["schema"] = 1.0; mutations.append(bad)
        bad = fixture(); bad["boot_id"] = "x" * 36; mutations.append(bad)
        bad = fixture(); bad["roles"]["data"]["size_bytes"] = 134217728.0; mutations.append(bad)
        bad = fixture(); bad["roles"]["data"]["offset"] = False; mutations.append(bad)
        bad = fixture(); bad["roles"]["metadata"]["sizelimit"] = 0.0; mutations.append(bad)
        bad = fixture(); bad["roles"]["data"]["readonly"] = True; mutations.append(bad)
        bad = fixture(); bad["roles"]["clone"]["holders"] = ["253:999"]; mutations.append(bad)
        bad = fixture(); bad["roles"]["zero"]["holders"] = []; mutations.append(bad)
        for value in mutations:
            with self.assertRaises(LAB.Refusal):
                LAB.GuardEpochModel(value)

    def test_reopen_may_change_only_clone_incarnation_then_needs_new_guard(self):
        model = LAB.GuardEpochModel(fixture())
        model.accept_guard(receipt(model)); model.publish(); model.withdraw(True, 0)
        reopened = fixture(); reopened["roles"]["clone"]["diskseq"] = 8
        model.reconfigure("REOPEN", reopened, True, 0)
        self.assertEqual(model.epoch, 2)
        model.accept_guard(receipt(model))
        self.assertEqual(model.publish()["classification"],
                         "MODEL_GUARD_PUBLICATION_ORDER_VALID")

    def test_bool_float_integer_fields_refuse(self):
        for field, value in (("epoch", True), ("clone_diskseq", 7.0),
                             ("queue_diskseq", True), ("discard_max_bytes", 0.0)):
            model = LAB.GuardEpochModel(fixture())
            bad = receipt(model); bad[field] = value
            with self.assertRaises(LAB.Refusal):
                model.accept_guard(bad)
            self.assertEqual(model.state, "UNKNOWN")

    def test_caller_mutation_does_not_change_pinned_fixture_or_guard(self):
        source = fixture(); model = LAB.GuardEpochModel(source)
        proof = receipt(model); model.accept_guard(proof)
        source["roles"]["clone"]["table"] = "foreign"
        proof["clone_table"] = "foreign"
        self.assertNotEqual(model.fixture["roles"]["clone"]["table"], "foreign")
        self.assertNotEqual(model.guard["clone_table"], "foreign")


if __name__ == "__main__":
    unittest.main()
