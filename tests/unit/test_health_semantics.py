import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HEALTH = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-health-json"


def load_function(name):
    tree = ast.parse(HEALTH.read_text(encoding="utf-8"))
    node = next(
        item
        for item in tree.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == name
    )
    module = ast.Module(body=[node], type_ignores=[])
    namespace = {"re": __import__("re")}
    exec(compile(module, str(HEALTH), "exec"), namespace)
    return namespace[name]


class ClusterHealthSemanticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evaluate = staticmethod(load_function("cluster_check_status"))

    def test_standalone_node_passes_without_quorum(self):
        status, message = self.evaluate(
            {"available": False, "status_available": False, "quorate": None}
        )
        self.assertEqual(status, "PASS")
        self.assertIn("not applicable", message)

    def test_healthy_cluster_passes(self):
        status, _ = self.evaluate(
            {"available": True, "status_available": True, "quorate": True}
        )
        self.assertEqual(status, "PASS")

    def test_two_node_no_qdevice_warns_about_loss_of_quorum(self):
        status, message = self.evaluate({
            "available": True, "status_available": True, "quorate": True,
            "configured_nodes": 2, "online_nodes": 2,
            "expected_votes": 2, "qdevice_configured": False,
        })
        self.assertEqual(status, "WARN")
        self.assertIn("loss of either node removes quorum", message)

    def test_forced_expected_one_fails_even_when_pve_reports_quorate(self):
        status, message = self.evaluate({
            "available": True, "status_available": True, "quorate": True,
            "configured_nodes": 2, "online_nodes": 1,
            "expected_votes": 1, "qdevice_configured": False,
        })
        self.assertEqual(status, "FAIL")
        self.assertIn("forced-quorum", message)

    def test_qdevice_survivor_is_legitimately_quorate(self):
        status, message = self.evaluate({
            "available": True, "status_available": True, "quorate": True,
            "configured_nodes": 2, "online_nodes": 1,
            "expected_votes": 2, "qdevice_configured": True,
        })
        self.assertEqual(status, "PASS")
        self.assertIn("qdevice", message)

    def test_n_node_quorum_is_not_hardcoded(self):
        for configured, online, quorate, expected in (
            (3, 2, True, 3), (3, 1, False, 3),
            (10, 6, True, 10), (10, 4, False, 10),
        ):
            status, _ = self.evaluate({
                "available": True, "status_available": True,
                "quorate": quorate, "configured_nodes": configured,
                "online_nodes": online, "expected_votes": expected,
                "qdevice_configured": False,
            })
            self.assertEqual(status, "PASS" if quorate else "FAIL")

    def test_non_quorate_cluster_fails(self):
        status, _ = self.evaluate(
            {"available": True, "status_available": True, "quorate": False}
        )
        self.assertEqual(status, "FAIL")

    def test_configured_cluster_with_unavailable_status_fails_safe(self):
        status, message = self.evaluate(
            {"available": True, "status_available": False, "quorate": None}
        )
        self.assertEqual(status, "FAIL")
        self.assertIn("unavailable", message)


class ExpectedPathPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evaluate = staticmethod(load_function("evaluate_expected_paths"))

    def test_unconfigured_policy_is_neutral(self):
        self.assertIsNone(self.evaluate({"paths_healthy": 0}, None))

    def test_expected_path_count_passes(self):
        status, message = self.evaluate({"paths_healthy": 2}, 2)
        self.assertEqual(status, "PASS")
        self.assertIn("meet configured minimum 2", message)

    def test_degraded_count_warns_without_policy_change(self):
        status, message = self.evaluate({"paths_healthy": 1}, 2)
        self.assertEqual(status, "WARN")
        self.assertIn("diagnostic only", message)
        self.assertIn("SAN policy unchanged", message)


class AllocationHeadroomPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evaluate = staticmethod(load_function("evaluate_allocation_policy"))

    def test_fixed_mode_warns_without_modification(self):
        status, message = self.evaluate({
            "initial_pool_mode": "fixed", "initial_pool_size_gib": 4,
        })
        self.assertEqual(status, "WARN")
        self.assertIn("burst capacity guarantee NO", message)
        self.assertIn("plugin made no change", message)
        self.assertIn("complete virtual disk", message)

    def test_proportional_and_full_are_explicit(self):
        status, message = self.evaluate({
            "initial_pool_mode": "proportional", "initial_pool_size_gib": 8,
            "initial_pool_percent": 50, "initial_pool_max_gib": 128,
        })
        self.assertEqual(status, "PASS")
        self.assertIn("bounded admission policy", message)
        self.assertIn("complete virtual disk", message)

        status, message = self.evaluate({
            "initial_pool_mode": "full", "initial_pool_size_gib": 8,
            "initial_pool_max_gib": 128,
        })
        self.assertEqual(status, "PASS")
        self.assertIn("fail closed", message)
        self.assertIn("complete virtual disk", message)

    def test_invalid_values_fail(self):
        self.assertEqual(self.evaluate({"initial_pool_mode": "guess"})[0], "FAIL")
        self.assertEqual(self.evaluate({
            "initial_pool_mode": "proportional", "initial_pool_percent": 0,
        })[0], "FAIL")


class ApiCompatibilityPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evaluate = staticmethod(load_function("evaluate_api_compatibility"))

    def test_api14_and_api15_are_explicit_passes(self):
        self.assertEqual(self.evaluate(14, 1, 14)[0], "PASS")
        self.assertEqual(self.evaluate(15, 1, 14)[0], "PASS")

    def test_unknown_runtime_api_fails_closed(self):
        self.assertEqual(self.evaluate(13, 1, 14)[0], "FAIL")
        self.assertEqual(self.evaluate(16, 2, 14)[0], "FAIL")

    def test_missing_api_evidence_fails(self):
        status, message = self.evaluate(None, None, None)
        self.assertEqual(status, "FAIL")
        self.assertIn("Unable to determine", message)


class ThinPoolFlagPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evaluate = staticmethod(load_function("evaluate_pool_flags"))

    def test_healthy_active_and_inactive_pool_flags(self):
        self.assertEqual(self.evaluate("twi-a-tz--", "")[0], "HEALTHY")
        self.assertEqual(self.evaluate("twi---tz--", None)[0], "HEALTHY")

    def test_corrupt_or_ambiguous_flags_are_critical(self):
        self.assertEqual(self.evaluate("twi-cotz--", "needs_check")[0], "CRITICAL")
        self.assertEqual(self.evaluate("twi-aotz--", "", "yes")[0], "CRITICAL")
        self.assertEqual(self.evaluate("twi-aotzM-", "")[0], "CRITICAL")
        self.assertEqual(self.evaluate("unexpected", "")[0], "CRITICAL")

    def test_missing_flags_are_unknown(self):
        self.assertEqual(self.evaluate(None, None)[0], "UNKNOWN")


class MetadataSparePolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evaluate = staticmethod(load_function("evaluate_pmspare"))

    def test_spare_must_cover_pool_metadata(self):
        self.assertEqual(self.evaluate(100, [{"size_bytes": 100}])[0], "PASS")
        self.assertEqual(self.evaluate(101, [{"size_bytes": 100}])[0], "WARN")

    def test_missing_evidence_warns_only(self):
        self.assertEqual(self.evaluate(100, [])[0], "WARN")
        self.assertEqual(self.evaluate(None, [{"size_bytes": 100}])[0], "WARN")


class AutoactivationPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evaluate = staticmethod(load_function("evaluate_autoactivation"))

    def test_disabled_is_pass(self):
        self.assertEqual(self.evaluate("0")[0], "PASS")

    def test_enabled_or_unknown_warn_without_repair(self):
        self.assertEqual(self.evaluate("")[0], "WARN")
        self.assertIn("made no change", self.evaluate("enabled")[1])
        self.assertEqual(self.evaluate(None)[0], "WARN")


class ThinFullPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evaluate = staticmethod(load_function("evaluate_thin_full_policy"))

    def test_bounded_queue_warns_without_claiming_safety(self):
        status, message = self.evaluate("queue", 60)
        self.assertEqual(status, "WARN")
        self.assertIn("60 seconds", message)
        self.assertIn("no setting was changed", message)

    def test_zero_timeout_is_critical(self):
        status, message = self.evaluate("queue", 0)
        self.assertEqual(status, "CRITICAL")
        self.assertIn("without a bounded", message)

    def test_error_policy_is_visible_but_not_called_data_safe(self):
        status, message = self.evaluate("error", 60)
        self.assertEqual(status, "PASS")
        self.assertIn("does not make pool exhaustion data-safe", message)

    def test_unknown_policy_warns(self):
        self.assertEqual(self.evaluate(None, None)[0], "WARN")


class PoolBatchCollectionTests(unittest.TestCase):
    def test_autoactivation_is_collected_in_single_lvs_report(self):
        tree = ast.parse(HEALTH.read_text(encoding="utf-8"))
        node = next(
            item for item in tree.body
            if isinstance(item, ast.FunctionDef) and item.name == "pools"
        )
        calls = []

        def fake_run(command):
            calls.append(command)
            return 0, (
                "sltp-123|1073741824|twi-a-tz--|pve-slt-sid-test|"
                "monitored|1.0|2.0|||4194304|zero|passdown|0|queue\n"
                "vm-123-disk-0|67108864|Vwi-a-tz--|pve-slt-sid-test|"
                "||||||zero|passdown|0|"
            ), ""

        namespace = {"re": re, "run": fake_run}
        exec(
            compile(ast.Module(body=[node], type_ignores=[]), str(HEALTH), "exec"),
            namespace,
        )
        result, error = namespace["pools"]("testvg", "test")
        self.assertEqual(len(calls), 1)
        self.assertIn("lv_autoactivation", " ".join(calls[0]))
        self.assertIn("lv_when_full", " ".join(calls[0]))
        self.assertIsNone(error)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["autoactivation"], "0")
        self.assertEqual(result[0]["when_full"], "queue")

    def test_lvs_failure_is_not_reported_as_an_empty_inventory(self):
        tree = ast.parse(HEALTH.read_text(encoding="utf-8"))
        node = next(
            item for item in tree.body
            if isinstance(item, ast.FunctionDef) and item.name == "pools"
        )
        namespace = {"re": re, "run": lambda command: (124, "", "timed out")}
        exec(
            compile(ast.Module(body=[node], type_ignores=[]), str(HEALTH), "exec"),
            namespace,
        )
        result, error = namespace["pools"]("testvg", "test")
        self.assertIsNone(result)
        self.assertEqual(error, "timed out")


class PvBindingPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evaluate = staticmethod(load_function("evaluate_pv_binding"))

    def test_exact_mapper_binding_passes(self):
        status, _ = self.evaluate(
            [{"pv_uuid": "pv-1", "pv_name": "/dev/mapper/3600abcd"}],
            "pv-1", "3600abcd",
        )
        self.assertEqual(status, "PASS")

    def test_duplicate_raw_and_mapper_binding_fails(self):
        status, message = self.evaluate(
            [
                {"pv_uuid": "pv-1", "pv_name": "/dev/sdd"},
                {"pv_uuid": "pv-1", "pv_name": "/dev/mapper/3600abcd"},
            ],
            "pv-1", "3600abcd",
        )
        self.assertEqual(status, "FAIL")
        self.assertIn("duplicate PV", message)
        self.assertIn("no mutation", message)

    def test_raw_path_preference_fails(self):
        status, message = self.evaluate(
            [{"pv_uuid": "pv-1", "pv_name": "/dev/sdd"}],
            "pv-1", "3600abcd",
        )
        self.assertEqual(status, "FAIL")
        self.assertIn("expected stable multipath", message)


class PvMetadataCapacityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evaluate = staticmethod(load_function("evaluate_pv_metadata"))

    def test_healthy_metadata_area_passes(self):
        status, message = self.evaluate([
            {"pv_mda_size": "16777216", "pv_mda_free": "12582912"}
        ])
        self.assertEqual(status, "PASS")
        self.assertIn("75% free", message)

    def test_low_and_exhausted_metadata_warn_without_repair(self):
        low = self.evaluate([
            {"pv_mda_size": "1000", "pv_mda_free": "100"}
        ])
        exhausted = self.evaluate([
            {"pv_mda_size": "1000", "pv_mda_free": "0"}
        ])
        self.assertEqual(low[0], "WARN")
        self.assertEqual(exhausted[0], "WARN")
        self.assertIn("diagnostic only", exhausted[1])


if __name__ == "__main__":
    unittest.main()
