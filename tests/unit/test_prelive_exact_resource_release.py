import copy
import hashlib
import importlib
from pathlib import Path
import sys
import threading
import unittest


ROOT = Path(__file__).resolve().parents[2]
EXP = ROOT / "experiments/thick-generations"
sys.path.insert(0, str(EXP))
LAB = importlib.import_module("prelive_exact_resource_release")


OWNER = {"pid": 321, "starttime": 654,
         "boot_id": "11111111-2222-3333-4444-555555555555"}


class Calls:
    exact_resource_release_mock = True

    def __init__(self, deadline=1000):
        self.owner = copy.deepcopy(OWNER)
        self.times = [100, 200]
        self.checked = 150
        self.single = True
        self.same = True
        self.closes = []
        self.deadline = deadline

    def owner_identity(self):
        return copy.deepcopy(self.owner)

    def monotonic_ns(self):
        return self.times.pop(0)

    def single_threaded(self):
        return self.single

    def same_open_file_description(self, primary, anchor):
        return {"same": self.same, "checked_ns": self.checked}

    def close(self, fd):
        self.closes.append(fd)


class ExactResourceReleaseTests(unittest.TestCase):
    def setUp(self):
        self.authority = object()
        self.digest = hashlib.sha256(b"authority").hexdigest()
        self.binding = LAB.record_exact_pidfd_binding(
            OWNER, 30, 31, self.authority, self.digest, 1000)
        self.calls = Calls()

    def test_exact_primary_close_is_once_and_anchor_remains(self):
        result = self.binding.release_primary_once(
            self.authority, self.digest, self.calls)
        self.assertEqual(result["classification"],
                         "PRIMARY_CLOSED_ANCHOR_OWNED")
        self.assertEqual(self.calls.closes, [30])
        self.assertTrue(result["primary_close_confirmed"])
        self.assertFalse(result["primary_owned"])
        self.assertTrue(result["anchor_owned"])
        self.assertFalse(result["runtime_authorized"])
        with self.assertRaises(BaseException):
            self.binding.release_primary_once(
                self.authority, self.digest, self.calls)
        self.assertEqual(self.calls.closes, [30])

    def test_recycled_primary_is_quarantined_without_close(self):
        self.calls.same = False
        result = self.binding.release_primary_once(
            self.authority, self.digest, self.calls)
        self.assertEqual(result["classification"],
                         "QUARANTINED_RESOURCE_IDENTITY")
        self.assertEqual(self.calls.closes, [])
        self.assertTrue(result["primary_owned"])

    def test_close_after_effect_error_is_unknown_and_never_retried(self):
        def close_after_effect(fd):
            self.calls.closes.append(fd)
            raise OSError("ack lost")
        self.calls.close = close_after_effect
        result = self.binding.release_primary_once(
            self.authority, self.digest, self.calls)
        self.assertEqual(result["classification"], "PRIMARY_CLOSE_UNKNOWN")
        self.assertEqual(self.calls.closes, [30])
        self.assertFalse(self.binding.primary_owned)
        with self.assertRaises(BaseException):
            self.binding.release_primary_once(
                self.authority, self.digest, self.calls)
        self.assertEqual(self.calls.closes, [30])

    def test_owner_drift_dispatches_zero_close(self):
        self.calls.owner["pid"] += 1
        with self.assertRaises(BaseException):
            self.binding.release_primary_once(
                self.authority, self.digest, self.calls)
        self.assertEqual(self.calls.closes, [])

    def test_nonexclusive_release_dispatches_zero_close(self):
        self.calls.single = False
        with self.assertRaises(BaseException):
            self.binding.release_primary_once(
                self.authority, self.digest, self.calls)
        self.assertEqual(self.calls.closes, [])

    def test_close_confirmed_after_deadline_is_retained(self):
        self.calls.times = [998, 1000]
        self.calls.checked = 999
        result = self.binding.release_primary_once(
            self.authority, self.digest, self.calls)
        self.assertEqual(result["classification"],
                         "PRIMARY_CLOSED_CONFIRMED_LATE")
        self.assertTrue(result["primary_close_confirmed"])
        self.assertEqual(self.calls.closes, [30])

    def test_nested_release_is_permanent_unknown_without_second_close(self):
        original = self.calls.same_open_file_description
        def nested(primary, anchor):
            with self.assertRaises(BaseException):
                self.binding.release_primary_once(
                    self.authority, self.digest, self.calls)
            return original(primary, anchor)
        self.calls.same_open_file_description = nested
        with self.assertRaises(BaseException):
            self.binding.release_primary_once(
                self.authority, self.digest, self.calls)
        self.assertEqual(self.calls.closes, [])
        self.assertEqual(self.binding.state, "RESOURCE_RELEASE_UNKNOWN")

    def test_swallowed_nested_close_is_confirmed_but_authority_unknown(self):
        def nested_then_close(fd):
            with self.assertRaises(BaseException):
                self.binding.release_primary_once(
                    self.authority, self.digest, object())
            self.calls.closes.append(fd)
        self.calls.close = nested_then_close
        result = self.binding.release_primary_once(
            self.authority, self.digest, self.calls)
        self.assertEqual(result["classification"],
                         "PRIMARY_CLOSED_AUTHORITY_UNKNOWN")
        self.assertTrue(result["primary_close_confirmed"])
        self.assertEqual(self.calls.closes, [30])

    def test_swallowed_nested_final_clock_retains_confirmed_unknown(self):
        calls = 0
        def clock():
            nonlocal calls
            calls += 1
            if calls == 2:
                with self.assertRaises(BaseException):
                    self.binding.release_primary_once(
                        self.authority, self.digest, object())
            return (100, 200)[calls - 1]
        self.calls.monotonic_ns = clock
        result = self.binding.release_primary_once(
            self.authority, self.digest, self.calls)
        self.assertEqual(result["classification"],
                         "PRIMARY_CLOSED_AUTHORITY_UNKNOWN")
        self.assertTrue(result["primary_close_confirmed"])
        self.assertEqual(self.calls.closes, [30])

    def test_joint_owner_mutation_cannot_redefine_preimage(self):
        def mutate_owner():
            self.binding.owner["pid"] += 1
            return copy.deepcopy(self.binding.owner)
        self.calls.owner_identity = mutate_owner
        with self.assertRaises(BaseException):
            self.binding.release_primary_once(
                self.authority, self.digest, self.calls)
        self.assertEqual(self.calls.closes, [])

    def test_callback_surface_mutation_dispatches_zero_close(self):
        def mutate_surface():
            self.calls.close = lambda fd: self.calls.closes.append(fd)
            return copy.deepcopy(OWNER)
        self.calls.owner_identity = mutate_surface
        with self.assertRaises(BaseException):
            self.binding.release_primary_once(
                self.authority, self.digest, self.calls)
        self.assertEqual(self.calls.closes, [])

    def test_comparator_crossing_deadline_dispatches_zero_close(self):
        self.calls.checked = 1000
        with self.assertRaises(BaseException):
            self.binding.release_primary_once(
                self.authority, self.digest, self.calls)
        self.assertEqual(self.calls.closes, [])

    def test_final_clock_owner_drift_retains_confirmed_unknown(self):
        calls = 0
        def clock():
            nonlocal calls
            calls += 1
            if calls == 2:
                self.binding.owner["pid"] += 1
            return (100, 200)[calls - 1]
        self.calls.monotonic_ns = clock
        result = self.binding.release_primary_once(
            self.authority, self.digest, self.calls)
        self.assertEqual(result["classification"],
                         "PRIMARY_CLOSED_AUTHORITY_UNKNOWN")
        self.assertTrue(result["primary_close_confirmed"])
        self.assertEqual(self.calls.closes, [30])

    def test_final_clock_regression_from_comparison_is_confirmed_unknown(self):
        self.calls.times = [100, 120]
        self.calls.checked = 150
        with self.assertRaises(BaseException):
            self.binding.release_primary_once(
                self.authority, self.digest, self.calls)
        self.assertTrue(self.binding.close_confirmed)
        self.assertEqual(self.calls.closes, [30])
        self.assertEqual(self.binding.state,
                         "PRIMARY_CLOSED_AUTHORITY_UNKNOWN")

    def test_custom_attribute_lookup_backend_is_refused_without_lookup(self):
        class CustomLookup(Calls):
            lookups = 0
            def __getattribute__(self, name):
                type(self).lookups += 1
                return super().__getattribute__(name)
        calls = CustomLookup()
        CustomLookup.lookups = 0
        with self.assertRaises(BaseException):
            self.binding.release_primary_once(
                self.authority, self.digest, calls)
        self.assertEqual(CustomLookup.lookups, 0)
        self.assertEqual(object.__getattribute__(calls, "closes"), [])

    def test_mutable_comparison_receipt_cannot_lower_watermark(self):
        receipt = {"same": True, "checked_ns": 150}
        self.calls.times = [100, 120]
        self.calls.same_open_file_description = lambda primary, anchor: receipt
        def mutate_then_close(fd):
            receipt["checked_ns"] = 0
            self.calls.closes.append(fd)
        self.calls.close = mutate_then_close
        with self.assertRaises(BaseException):
            self.binding.release_primary_once(
                self.authority, self.digest, self.calls)
        self.assertTrue(self.binding.close_confirmed)
        self.assertEqual(self.binding.state,
                         "PRIMARY_CLOSED_AUTHORITY_UNKNOWN")
        self.assertEqual(self.calls.closes, [30])

    def test_custom_instance_dict_property_is_zero_call_refusal(self):
        class CustomNamespace:
            exact_resource_release_mock = True
            property_calls = 0
            @property
            def __dict__(self):
                type(self).property_calls += 1
                return {}
            def owner_identity(self):
                raise AssertionError
            def monotonic_ns(self):
                raise AssertionError
            def single_threaded(self):
                raise AssertionError
            def same_open_file_description(self, primary, anchor):
                raise AssertionError
            def close(self, fd):
                raise AssertionError
        calls = CustomNamespace()
        with self.assertRaises(BaseException):
            self.binding.release_primary_once(
                self.authority, self.digest, calls)
        self.assertEqual(CustomNamespace.property_calls, 0)

    def test_callback_injected_custom_namespace_key_avoids_equality(self):
        class Collision:
            def __eq__(self, other):
                raise AssertionError("custom namespace equality executed")
            def __hash__(self):
                return hash("close")
        def mutate_namespace():
            object.__getattribute__(self.calls, "__dict__")[Collision()] = 1
            return copy.deepcopy(OWNER)
        self.calls.owner_identity = mutate_namespace
        with self.assertRaises(BaseException) as raised:
            self.binding.release_primary_once(
                self.authority, self.digest, self.calls)
        self.assertNotIn("custom namespace equality executed",
                         str(raised.exception))
        self.assertEqual(self.calls.closes, [])

    def test_comparator_class_swap_never_calls_new_dict_property(self):
        class Swapped(Calls):
            property_calls = 0
            @property
            def __dict__(self):
                type(self).property_calls += 1
                return {}
        def swap(primary, anchor):
            self.calls.__class__ = Swapped
            return {"same": True, "checked_ns": 150}
        self.calls.same_open_file_description = swap
        with self.assertRaises(BaseException):
            self.binding.release_primary_once(
                self.authority, self.digest, self.calls)
        self.assertEqual(Swapped.property_calls, 0)
        self.assertEqual(object.__getattribute__(self.calls, "closes"), [])

    def test_foreign_thread_dispatches_zero_close(self):
        errors = []
        def run():
            try:
                self.binding.release_primary_once(
                    self.authority, self.digest, self.calls)
            except BaseException as exc:
                errors.append(type(exc).__name__)
        worker = threading.Thread(target=run)
        worker.start()
        worker.join()
        self.assertTrue(errors)
        self.assertEqual(self.calls.closes, [])


if __name__ == "__main__":
    unittest.main()
