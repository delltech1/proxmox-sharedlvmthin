"""Crash-prefix tests of the isolated, synthetic ordering model."""
import copy
import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[2] / "experiments/thick-generations/closed-reboot-intent-model.py"
spec = importlib.util.spec_from_file_location("reboot_intent_model", path)
model = importlib.util.module_from_spec(spec)
spec.loader.exec_module(model)


class Store:
    def __init__(self, records, fail_after=None):
        self.records = copy.deepcopy(records)
        self.fail_after = fail_after
        self.writes = []

    def get(self, key):
        return copy.deepcopy(self.records.get(key))

    def write(self, key, value):
        self.records[key] = copy.deepcopy(value)
        self.writes.append(key)
        if len(self.writes) == self.fail_after:
            raise OSError("persisted write lost ACK")


class RebootIntentModelTests(unittest.TestCase):
    def setUp(self):
        self.old_boot = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self.boot = "11111111-2222-3333-4444-555555555555"
        old = {"state": "CLOSED", "authority": "NONE", "txid": "a" * 32,
               "boot_id": self.old_boot, "payload": "b" * 64}
        new = {"state": "CLOSED", "authority": "NONE", "txid": "c" * 32,
               "boot_id": self.boot, "payload": "d" * 64}
        self.proposal = {"node": "node", "old_boot": self.old_boot, "new_boot": self.boot,
                         "old_latch": old, "new_latch": new,
                         "old_release": {"qualified": "UNQUALIFIED", "latch_sha256": model.digest(old)},
                         "new_release": {"qualified": "UNQUALIFIED", "latch_sha256": model.digest(new)},
                         "old_ledger": {"phase": "ACTIVE", "generation": "e" * 32, "payload": "b" * 64},
                         "new_ledger": {"phase": "ACTIVE", "generation": "f" * 32, "payload": "d" * 64}}
        self.initial = {name: copy.deepcopy(self.proposal["old_" + name])
                        for name in ("latch", "release", "ledger")}

    def call(self, store, effects, boot=None, proposal=None):
        return model.reconcile(store, self.proposal if proposal is None else proposal,
                               boot_id=boot or self.boot, effects=effects)

    def assert_closed(self, store):
        self.assertEqual(store.records["latch"]["state"], "CLOSED")
        self.assertEqual(store.records["latch"]["authority"], "NONE")
        self.assertEqual(store.records["release"]["qualified"], "UNQUALIFIED")

    def test_lost_ack_after_every_write_replays_and_stays_closed(self):
        for cut in range(1, 6):
            with self.subTest(cut=cut):
                store = Store(self.initial, fail_after=cut)
                try:
                    self.call(store, "NO_DISPATCH_PROVEN")
                    self.call(store, "EXACT_TARGET_CONFIRMED")
                    self.fail("expected lost ACK")
                except OSError:
                    pass
                self.assert_closed(store)
                store.fail_after = None
                if "intent" not in store.records:
                    self.call(store, "NO_DISPATCH_PROVEN")
                self.call(store, "EXACT_TARGET_CONFIRMED")
                self.assert_closed(store)
                complete = copy.deepcopy(store.records)
                writes = len(store.writes)
                self.call(store, "EXACT_TARGET_CONFIRMED")
                self.assertEqual(store.records, complete)
                self.assertEqual(len(store.writes), writes)

    def test_unknown_effect_and_second_boot_refuse_every_prefix(self):
        for cut in range(1, 6):
            store = Store(self.initial, fail_after=cut)
            try:
                self.call(store, "NO_DISPATCH_PROVEN")
                self.call(store, "EXACT_TARGET_CONFIRMED")
            except OSError:
                pass
            before = copy.deepcopy(store.records)
            for effects, boot in (("UNKNOWN", self.boot), ("EXACT_TARGET_CONFIRMED", self.old_boot)):
                with self.subTest(cut=cut, effects=effects), self.assertRaises(model.Refused):
                    self.call(store, effects, boot)
                self.assertEqual(store.records, before)
                self.assert_closed(store)

    def test_unprepared_target_fork_and_qualified_release_refuse(self):
        store = Store(self.initial)
        with self.assertRaises(model.Refused):
            self.call(store, "EXACT_TARGET_CONFIRMED")
        self.call(store, "NO_DISPATCH_PROVEN")
        fork = copy.deepcopy(self.proposal)
        fork["node"] = "other"
        before = copy.deepcopy(store.records)
        with self.assertRaises(model.Refused):
            self.call(store, "EXACT_TARGET_CONFIRMED", proposal=fork)
        self.assertEqual(store.records, before)
        store.records["release"]["qualified"] = "QUALIFIED"
        with self.assertRaises(model.Refused):
            self.call(store, "EXACT_TARGET_CONFIRMED")


if __name__ == "__main__":
    unittest.main()
