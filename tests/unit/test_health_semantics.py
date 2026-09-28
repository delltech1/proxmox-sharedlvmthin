import ast
import importlib.util
import hashlib
import io
import os
import re
import types
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HEALTH = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-health-json"
INVENTORY = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin_pve_inventory.py"
_inventory_spec = importlib.util.spec_from_file_location("slt_pve_inventory", INVENTORY)
PVE_INVENTORY = importlib.util.module_from_spec(_inventory_spec)
_inventory_spec.loader.exec_module(PVE_INVENTORY)


def load_function(name):
    tree = ast.parse(HEALTH.read_text(encoding="utf-8"))
    helpers = {"exact_decimal", "exact_byte_count", "percentage_decimal",
               "percentage_bytes", "normalize_wwid_from_pv",
               "flatten_multipath_paths", "read_file"}
    body = [
        item for item in tree.body
        if (
            isinstance(item, ast.ImportFrom) and item.module == "decimal"
        ) or (
            isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and (item.name == name or item.name in helpers)
        )
    ]
    module = ast.Module(body=body, type_ignores=[])
    namespace = {"re": __import__("re")}
    exec(compile(module, str(HEALTH), "exec"), namespace)
    return namespace[name]


class CollectorDurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.elapsed = staticmethod(load_function("elapsed_milliseconds"))

    def test_elapsed_duration_is_reported_in_rounded_milliseconds(self):
        self.assertEqual(self.elapsed(10.0, 11.234), 1234)

    def test_monotonic_clock_regression_is_clamped_to_zero(self):
        self.assertEqual(self.elapsed(11.0, 10.0), 0)


class TransportInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = staticmethod(load_function("build_transport"))

    def test_shared_inventory_avoids_reprobing_and_caches_iscsi_sessions(self):
        wwid = "a" * 32
        inventory = {
            "pvs": [{
                "vg_name": "vg-a", "pv_name": f"/dev/mapper/{wwid}",
                "pv_uuid": "pv-a",
            }],
            "multipath_maps": [{
                "uuid": wwid, "vend": "vendor", "prod": "array",
                "sysfs": "dm-10", "path_faults": 0,
                "path_groups": [{"paths": [{
                    "dev": "sda", "dm_st": "active",
                    "dev_st": "running", "chk_st": "ready",
                }]}],
            }],
            "block": {"sda": {"transport": "iscsi", "hctl": "1:0:0:1"}},
        }
        sessions = [{"portal": "192.0.2.1", "port": 3260, "target": "iqn.test"}]
        globals_ = self.build.__globals__
        fallbacks = {
            "pvs_json": mock.Mock(side_effect=AssertionError("unexpected pvs probe")),
            "multipath_json": mock.Mock(side_effect=AssertionError("unexpected multipath probe")),
            "block_transport_map": mock.Mock(side_effect=AssertionError("unexpected block probe")),
            "iscsi_sessions": mock.Mock(return_value=sessions),
            "iscsi_path_identity": mock.Mock(return_value=({
                "session": 1,
                "target": "iqn.test",
                "portal": "192.0.2.1",
                "port": 3260,
                "iface": "default",
            }, None)),
            "iscsi_node_records": mock.Mock(return_value=([{
                "target": "iqn.test",
                "startup": "automatic",
                "portal": "192.0.2.1",
                "port": 3260,
                "iface": "default",
            }], None)),
            "evaluate_iscsi_boot_persistence": mock.Mock(
                return_value=("PASS", "fixture persistence proven")
            ),
            "run": mock.Mock(return_value=(0, "enabled", "")),
        }
        with mock.patch.dict(globals_, fallbacks):
            first = self.build("vg-a", inventory)
            second = self.build("vg-a", inventory)

        self.assertEqual(first["state"], "HEALTHY")
        self.assertEqual(second["sessions"], sessions)
        self.assertEqual(fallbacks["iscsi_sessions"].call_count, 1)
        self.assertEqual(fallbacks["iscsi_node_records"].call_count, 1)
        self.assertEqual(first["boot_persistence"]["status"], "PASS")


class IscsiBootPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parse = staticmethod(load_function("parse_iscsi_node_records"))
        cls.evaluate = staticmethod(load_function("evaluate_iscsi_boot_persistence"))

    def identity(self, *, portal="192.0.2.10", iface="default"):
        return {
            "target": "iqn.2026-09.example:managed",
            "portal": portal,
            "port": 3260,
            "iface": iface,
        }

    def transport(self, identities):
        return {
            "type": "iscsi",
            "paths": [
                {"device": f"sd{chr(97 + index)}", "iscsi_identity": identity}
                for index, identity in enumerate(identities)
            ],
        }

    def record(self, *, startup="automatic", portal="192.0.2.10", iface="default"):
        return {
            **self.identity(portal=portal, iface=iface),
            "startup": startup,
        }

    def test_parser_keeps_exact_target_portal_iface_and_startup(self):
        parsed = self.parse("""
node.name = iqn.2026-09.example:managed
node.startup = automatic
iface.iscsi_ifacename = default
node.conn[0].address = 192.0.2.10
node.conn[0].port = 3260
node.name = iqn.2026-09.example:other
node.startup = manual
iface.iscsi_ifacename = iface1
node.conn[0].address = 192.0.2.11
node.conn[0].port = 3260
""")
        self.assertEqual(parsed, [
            self.record(),
            {
                "target": "iqn.2026-09.example:other",
                "startup": "manual",
                "iface": "iface1",
                "portal": "192.0.2.11",
                "port": 3260,
            },
        ])

    def test_manual_required_path_fails_despite_live_path(self):
        status, reason = self.evaluate(
            self.transport([self.identity()]), [self.record(startup="manual")],
            "enabled",
        )
        self.assertEqual(status, "FAIL")
        self.assertIn("not automatic", reason)

    def test_one_automatic_and_one_manual_required_path_fails(self):
        identities = [self.identity(), self.identity(portal="192.0.2.11")]
        records = [self.record(), self.record(startup="manual", portal="192.0.2.11")]
        status, reason = self.evaluate(
            self.transport(identities), records, "enabled"
        )
        self.assertEqual(status, "FAIL")
        self.assertIn("sdb", reason)

    def test_unrelated_manual_record_does_not_block_managed_path(self):
        unrelated = {
            "target": "iqn.2026-09.example:unrelated",
            "startup": "manual",
            "iface": "default",
            "portal": "198.51.100.8",
            "port": 3260,
        }
        status, reason = self.evaluate(
            self.transport([self.identity()]), [self.record(), unrelated], "enabled"
        )
        self.assertEqual(status, "PASS")
        self.assertIn("1 managed iSCSI path", reason)

    def test_missing_duplicate_or_wrong_iface_record_fails(self):
        cases = (
            [],
            [self.record(), self.record()],
            [self.record(iface="iface1")],
        )
        for records in cases:
            with self.subTest(records=records):
                status, _ = self.evaluate(
                    self.transport([self.identity()]), records, "enabled"
                )
                self.assertEqual(status, "FAIL")

    def test_disabled_or_unknown_boot_service_fails(self):
        for service in ("disabled", "masked", "unknown", None):
            with self.subTest(service=service):
                status, reason = self.evaluate(
                    self.transport([self.identity()]), [self.record()], service
                )
                self.assertEqual(status, "FAIL")
                self.assertIn("not enabled", reason)


class LvmAliasInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pools = staticmethod(load_function("pools"))

    def test_shared_raw_inventory_keeps_sid_specific_classification(self):
        anchor = "|".join([
            "sltg-a-test", "1048576", "-wi-a-----",
            "slt_tg_sid=sid-a,slt_tg_vol=vm-100-disk-0,"
            "slt_tg_phase=MATERIALIZED,slt_tg_op=CREATE,"
            "slt_tg_tx=" + ("a" * 32) + ",slt_tg_head=head,"
            "slt_tg_generation=1",
            "", "", "", "", "", "", "y", "passdown", "y", "error",
        ])
        thin = "|".join([
            "sltp-100", "1048576", "twi-a-tz--",
            "pve-slt-sid-sid-a,pve-slt-owner-v1", "monitored",
            "1", "2", "", "", "65536", "y", "passdown", "y", "error",
        ])
        inventory = {"rc": 0, "output": anchor + "\n" + thin + "\n", "error": ""}
        globals_ = self.pools.__globals__
        with mock.patch.dict(globals_, {
            "lvm_volume_inventory": mock.Mock(
                side_effect=AssertionError("unexpected LVM reprobe")
            ),
        }):
            pools_a, error_a, anchors_a = self.pools("vg-a", "sid-a", inventory)
            pools_b, error_b, anchors_b = self.pools("vg-a", "sid-b", inventory)

        self.assertIsNone(error_a)
        self.assertIsNone(error_b)
        self.assertEqual([item["name"] for item in anchors_a], ["sltg-a-test"])
        self.assertEqual(anchors_b, [])
        self.assertTrue(pools_a[0]["owned"])
        self.assertFalse(pools_b[0]["owned"])


class StorageConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parse = staticmethod(load_function("parse_storage_cfg"))

    def parse_text(self, text):
        with mock.patch("builtins.open", mock.mock_open(read_data=text)):
            return self.parse()

    def test_explicitly_disabled_storage_is_recorded(self):
        storages = self.parse_text(
            "sharedlvmthin: offline\n"
            "\tslt-vgname vg_offline\n"
            "\tdisable 1\n"
        )
        self.assertEqual(len(storages), 1)
        self.assertTrue(storages[0]["disabled"])

    def test_canonical_pve_bare_disable_is_recorded(self):
        storages = self.parse_text(
            "sharedlvmthin: offline\n"
            "\tdisable\n"
            "\tslt-vgname vg_offline\n"
        )
        self.assertEqual(len(storages), 1)
        self.assertTrue(storages[0]["disabled"])

    def test_enabled_storage_remains_enabled_by_default(self):
        storages = self.parse_text(
            "sharedlvmthin: online\n"
            "\tslt-vgname vg_online\n"
        )
        self.assertEqual(len(storages), 1)
        self.assertFalse(storages[0]["disabled"])

    def test_false_disable_values_do_not_skip_storage(self):
        for value in ("0", "no", "off", "false"):
            with self.subTest(value=value):
                storages = self.parse_text(
                    "sharedlvmthin: online\n"
                    "\tslt-vgname vg_online\n"
                    f"\tdisable {value}\n"
                )
                self.assertFalse(storages[0]["disabled"])


    def test_node_scope_is_recorded(self):
        storages = self.parse_text(
            "sharedlvmthin: scoped\n"
            "\tslt-vgname vg_scoped\n"
            "\tnodes node-a,node-b\n"
        )
        self.assertEqual(storages[0]["nodes"], ["node-a", "node-b"])

    def test_unscoped_storage_applies_to_every_node(self):
        storages = self.parse_text(
            "sharedlvmthin: global\n\tslt-vgname vg_global\n"
        )
        self.assertIsNone(storages[0]["nodes"])

    def test_thick_materialization_limit_defaults_and_parses(self):
        default = self.parse_text(
            "sharedlvmthin: thick-default\n\tslt-vgname vg_a\n"
        )[0]
        self.assertEqual(default["tg_max_active_materializations"], 4)
        explicit = self.parse_text(
            "sharedlvmthin: thick-tuned\n"
            "\tslt-vgname vg_b\n"
            "\tslt-tg-max-active-materializations 12\n"
        )[0]
        self.assertEqual(explicit["tg_max_active_materializations"], 12)


class DmCloneDiagnosticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.info = staticmethod(load_function("dm_clone_info"))

    def test_reports_global_hydration_throttle(self):
        with mock.patch("builtins.open", mock.mock_open(read_data="75\n")):
            self.assertEqual(self.info(), {
                "available": True,
                "hydration_throttle_percent": 75,
            })

    def test_missing_or_invalid_throttle_is_diagnostic_unknown(self):
        for value in ("not-a-number\n", "101\n", "-1\n"):
            with self.subTest(value=value), mock.patch(
                "builtins.open", mock.mock_open(read_data=value)
            ):
                self.assertEqual(self.info(), {
                    "available": False,
                    "hydration_throttle_percent": None,
                })
        with mock.patch("builtins.open", side_effect=FileNotFoundError):
            self.assertFalse(self.info()["available"])

class ThickMaterializationAdmissionHealthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evaluate = staticmethod(
            load_function("evaluate_thick_materialization_admission")
        )

    def test_slots_are_counted_from_non_materialized_anchors(self):
        status, result = self.evaluate([
            {"phase": "MATERIALIZED"},
            {"phase": "HYDRATING"},
            {"phase": "HYDRATION_COMPLETE"},
        ], 3)
        self.assertEqual(status, "PASS")
        self.assertEqual(result["active"], 2)
        self.assertTrue(result["available"])
        self.assertEqual(result["available_slots"], 1)
        self.assertEqual(result["state"], "AVAILABLE")

    def test_saturated_limit_is_visible_but_not_false_failure(self):
        status, result = self.evaluate([
            {"phase": "HYDRATING"}, {"phase": "PREPARED"},
        ], 2)
        self.assertEqual(status, "PASS")
        self.assertFalse(result["available"])
        self.assertEqual(result["available_slots"], 0)
        self.assertEqual(result["state"], "SATURATED")
        self.assertIn("intentionally blocked", result["reason"])

    def test_lowered_limit_reports_zero_slots_without_negative_telemetry(self):
        status, result = self.evaluate([
            {"phase": "HYDRATING"},
            {"phase": "HYDRATING"},
            {"phase": "HYDRATION_COMPLETE"},
        ], 2)
        self.assertEqual(status, "PASS")
        self.assertEqual(result["active"], 3)
        self.assertEqual(result["available_slots"], 0)
        self.assertEqual(result["state"], "SATURATED")
        self.assertFalse(result["available"])

    def test_invalid_limit_fails_health_closed(self):
        for value in (0, 65, "four", True):
            with self.subTest(value=value):
                status, result = self.evaluate([], value)
                self.assertEqual(status, "FAIL")
                self.assertFalse(result["available"])
                self.assertIsNone(result["available_slots"])
                self.assertEqual(result["state"], "UNKNOWN")


class VgMutationIntentHealthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse(HEALTH.read_text(encoding="utf-8"))
        node = next(
            item for item in tree.body
            if isinstance(item, ast.FunctionDef)
            and item.name == "evaluate_vg_mutation_intent"
        )
        namespace = {"re": re, "hashlib": hashlib}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(HEALTH), "exec"), namespace)
        cls.evaluate = staticmethod(namespace["evaluate_vg_mutation_intent"])

    def test_empty_intent_is_healthy(self):
        status, reason, operation = self.evaluate("testvg", "testvg|")
        self.assertEqual((status, operation), ("PASS", None))
        self.assertIn("no persistent", reason)

    def test_open_extend_is_validated_but_fails_health_closed(self):
        values = {
            "v": "1", "tx": "4" * 32, "state": "OPEN", "op": "EXTEND",
            "object": "sltg-a-0123456789abcdef01234567", "before": "b" * 32,
        }
        order = ("v", "tx", "state", "op", "object", "before")
        canonical = "|".join(f"{key}={values[key]}" for key in order)
        digest = hashlib.sha256(canonical.encode()).hexdigest()[:32]
        tags = ",".join([
            *(f"slt_tg_vgi_{key}={values[key]}" for key in order),
            f"slt_tg_vgi_sha256={digest}",
        ])
        status, reason, operation = self.evaluate("testvg", f"testvg|{tags}")
        self.assertEqual((status, operation), ("FAIL", "EXTEND"))
        self.assertIn("blocks all new mutations", reason)
        self.assertIn("thick-recover-resize", reason)

    def test_duplicate_or_wrong_vg_intent_is_fail_closed(self):
        status, _, _ = self.evaluate(
            "testvg", "testvg|slt_tg_vgi_v=1,slt_tg_vgi_v=1"
        )
        self.assertEqual(status, "FAIL")
        status, _, _ = self.evaluate("testvg", "othervg|")
        self.assertEqual(status, "FAIL")

    def test_open_remove_points_to_reference_gated_volume_delete_recovery(self):
        values = {
            "v": "1", "tx": "7" * 32, "state": "OPEN", "op": "REMOVE",
            "object": "sltg-a-0123456789abcdef01234567", "before": "c" * 32,
        }
        order = ("v", "tx", "state", "op", "object", "before")
        canonical = "|".join(f"{key}={values[key]}" for key in order)
        digest = hashlib.sha256(canonical.encode()).hexdigest()[:32]
        tags = ",".join([
            *(f"slt_tg_vgi_{key}={values[key]}" for key in order),
            f"slt_tg_vgi_sha256={digest}",
        ])
        status, reason, operation = self.evaluate("testvg", f"testvg|{tags}")
        self.assertEqual((status, operation), ("FAIL", "REMOVE"))
        self.assertIn("thick-recover-volume-delete", reason)


class PackageIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evaluate = staticmethod(load_function("evaluate_package_identity"))

    def test_dual_and_thick_only_exact_identity_pass(self):
        for package, flavor in (
            ("pve-sharedlvmthin", "dual"),
            ("pve-sharedlvmthin-thick", "thick-only"),
        ):
            with self.subTest(flavor=flavor):
                status, message = self.evaluate(package, flavor, "1.2.3")
                self.assertEqual(status, "PASS")
                self.assertIn(package, message)

    def test_missing_or_unknown_marker_fails(self):
        for flavor in (None, "", "future"):
            with self.subTest(flavor=flavor):
                status, _ = self.evaluate(None, flavor, None)
                self.assertEqual(status, "FAIL")

    def test_identity_mismatch_and_missing_version_fail(self):
        status, _ = self.evaluate("pve-sharedlvmthin", "thick-only", "1.2.3")
        self.assertEqual(status, "FAIL")
        status, _ = self.evaluate("pve-sharedlvmthin-thick", "thick-only", None)
        self.assertEqual(status, "FAIL")


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

    def test_thick_generations_does_not_inherit_thin_headroom_policy(self):
        status, message = self.evaluate({
            "allocation_mode": "thick-generations",
            "initial_pool_mode": "invalid-thin-only-value",
        })
        self.assertEqual(status, "PASS")
        self.assertIn("independent thick LVs", message)
        self.assertIn("do not apply", message)

    def test_lazy_thick_does_not_inherit_thin_headroom_policy(self):
        status, message = self.evaluate({
            "allocation_mode": "thick-generations-lazy",
            "initial_pool_mode": "invalid-thin-only-value",
        })
        self.assertEqual(status, "PASS")
        self.assertIn("independent thick LVs", message)

    def test_unknown_allocation_mode_fails_closed(self):
        status, message = self.evaluate({"allocation_mode": "unknown"})
        self.assertEqual(status, "FAIL")
        self.assertIn("invalid allocation mode", message)

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


class DmeventdRequirementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evaluate = staticmethod(load_function("evaluate_dmeventd"))

    def test_active_service_passes(self):
        status, message = self.evaluate(True, [])
        self.assertEqual(status, "PASS")
        self.assertIn("active", message)

    def test_inactive_service_without_active_pool_warns(self):
        storages = [{
            "id": "thin-a",
            "pools": [{"name": "pool-a", "owned": True, "attr": "twi---tz--"}],
        }]
        status, message = self.evaluate(False, storages)
        self.assertEqual(status, "WARN")
        self.assertIn("no local owned thin pool", message)

    def test_inactive_service_with_active_owned_pool_fails(self):
        storages = [{
            "id": "thin-a",
            "pools": [{"name": "pool-a", "owned": True, "attr": "twi-a-tz--"}],
        }]
        status, message = self.evaluate(False, storages)
        self.assertEqual(status, "FAIL")
        self.assertIn("thin-a:pool-a", message)

    def test_unowned_active_pool_does_not_create_requirement(self):
        storages = [{
            "id": "foreign",
            "pools": [{"name": "pool-x", "owned": False, "attr": "twi-a-tz--"}],
        }]
        status, _ = self.evaluate(False, storages)
        self.assertEqual(status, "WARN")


class PoolBatchCollectionTests(unittest.TestCase):
    def test_autoactivation_is_collected_in_single_lvs_report(self):
        tree = ast.parse(HEALTH.read_text(encoding="utf-8"))
        wanted = {"exact_decimal", "exact_byte_count", "percentage_decimal",
                  "percentage_bytes", "lvm_volume_inventory", "pools"}
        body = [
            item for item in tree.body
            if (isinstance(item, ast.ImportFrom) and item.module == "decimal")
            or (isinstance(item, ast.FunctionDef) and item.name in wanted)
        ]
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
            compile(ast.Module(body=body, type_ignores=[]), str(HEALTH), "exec"),
            namespace,
        )
        result, error, thick_anchors = namespace["pools"]("testvg", "test")
        self.assertEqual(len(calls), 1)
        self.assertIn("lv_autoactivation", " ".join(calls[0]))
        self.assertIn("lv_when_full", " ".join(calls[0]))
        self.assertIsNone(error)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["autoactivation"], "0")
        self.assertEqual(result[0]["when_full"], "queue")
        self.assertEqual(result[0]["payload_used_bytes"], 10737418)
        self.assertEqual(result[0]["reserved_slack_bytes"], 1063004406)
        self.assertEqual(thick_anchors, [])

    def test_lvs_failure_is_not_reported_as_an_empty_inventory(self):
        tree = ast.parse(HEALTH.read_text(encoding="utf-8"))
        nodes = [
            item for item in tree.body
            if isinstance(item, ast.FunctionDef)
            and item.name in {"lvm_volume_inventory", "pools"}
        ]
        namespace = {"re": re, "run": lambda command: (124, "", "timed out")}
        exec(
            compile(ast.Module(body=nodes, type_ignores=[]), str(HEALTH), "exec"),
            namespace,
        )
        result, error, thick_anchors = namespace["pools"]("testvg", "test")
        self.assertIsNone(result)
        self.assertEqual(error, "timed out")
        self.assertIsNone(thick_anchors)


class ThickAnchorReferenceTests(unittest.TestCase):
    def test_cluster_wide_reference_scan_and_exact_token_matching(self):
        tree = ast.parse(HEALTH.read_text(encoding="utf-8"))
        node = next(
            item for item in tree.body
            if isinstance(item, ast.FunctionDef)
            and item.name == "pve_volume_reference_files"
        )

        class FakeGlob:
            @staticmethod
            def glob(pattern):
                if pattern == "/etc/pve/nodes/*/qemu-server/*.conf":
                    return ["/etc/pve/nodes/node-a/qemu-server/100.conf"]
                if pattern == "/etc/pve/qemu-server/*.conf":
                    return ["/etc/pve/qemu-server/duplicate-local.conf"]
                return []

        contents = {
            "/etc/pve/nodes/node-a/qemu-server/100.conf": (
                "scsi0: thick:vm-100-disk-0,size=1G\n"
            ),
            "/etc/pve/qemu-server/duplicate-local.conf": (
                "scsi0: thick:vm-100-disk-0,size=1G\n"
            ),
        }
        namespace = {
            "re": re,
            "glob": FakeGlob,
            "read_file": lambda path: contents.get(path),
        }
        exec(
            compile(ast.Module(body=[node], type_ignores=[]), str(HEALTH), "exec"),
            namespace,
        )
        scan = namespace["pve_volume_reference_files"]
        self.assertEqual(
            scan("thick:vm-100-disk-0"),
            ["/etc/pve/nodes/node-a/qemu-server/100.conf"],
        )
        self.assertEqual(scan("thick:vm-100-disk"), [])

    def test_cluster_reference_index_reads_each_config_once(self):
        tree = ast.parse(HEALTH.read_text(encoding="utf-8"))
        node = next(
            item for item in tree.body
            if isinstance(item, ast.FunctionDef)
            and item.name == "pve_volume_reference_index"
        )

        paths = [f"/etc/pve/nodes/node-a/qemu-server/{i}.conf" for i in range(1000)]
        reads = []

        class FakeGlob:
            @staticmethod
            def glob(pattern):
                return paths if pattern == "/etc/pve/nodes/*/qemu-server/*.conf" else []

        def read_file(path):
            reads.append(path)
            vmid = path.rsplit("/", 1)[-1].removesuffix(".conf")
            return (
                f"scsi0: thick:vm-{vmid}-disk-0,size=1G\n"
                f"description: near-thick:vm-{vmid}-disk-0x must not match\n"
            )

        namespace = {
            "re": re, "glob": FakeGlob, "read_file": read_file,
            "collect_pve_inventory": PVE_INVENTORY.collect,
        }
        exec(
            compile(ast.Module(body=[node], type_ignores=[]), str(HEALTH), "exec"),
            namespace,
        )
        index = namespace["pve_volume_reference_index"]()
        self.assertEqual(len(reads), 1000)
        self.assertEqual(len(set(reads)), 1000)
        self.assertEqual(
            sum(volid.startswith("thick:") for volid in index), 1000
        )
        self.assertEqual(
            index["thick:vm-999-disk-0"],
            ["/etc/pve/nodes/node-a/qemu-server/999.conf"],
        )
        self.assertNotIn("thick:vm-999-disk-0x", index)

    def test_reference_index_current_size_ignores_snapshot_size(self):
        tree = ast.parse(HEALTH.read_text(encoding="utf-8"))
        node = next(
            item for item in tree.body
            if isinstance(item, ast.FunctionDef)
            and item.name == "pve_volume_reference_index"
        )

        class FakeGlob:
            @staticmethod
            def glob(pattern):
                return ["/etc/pve/nodes/n/qemu-server/100.conf"] \
                    if pattern == "/etc/pve/nodes/*/qemu-server/*.conf" else []

        content = (
            "scsi0: thick:vm-100-disk-0,size=4G\n"
            "[old]\nscsi0: thick:vm-100-disk-0,size=2G\n"
        )
        namespace = {
            "re": re, "glob": FakeGlob, "read_file": lambda _path: content,
            "Decimal": __import__("decimal").Decimal,
            "InvalidOperation": __import__("decimal").InvalidOperation,
            "collect_pve_inventory": PVE_INVENTORY.collect,
        }
        exec(
            compile(ast.Module(body=[node], type_ignores=[]), str(HEALTH), "exec"),
            namespace,
        )
        references, sizes, errors = namespace["pve_volume_reference_index"](
            include_current_sizes=True
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(references["thick:vm-100-disk-0"]), 1)
        self.assertEqual(
            sizes["thick:vm-100-disk-0"],
            [("/etc/pve/nodes/n/qemu-server/100.conf", 4 * 2**30, "attached")],
        )

    def test_reference_index_rejects_description_and_duplicate_size(self):
        tree = ast.parse(HEALTH.read_text(encoding="utf-8"))
        node = next(item for item in tree.body if isinstance(item, ast.FunctionDef)
                    and item.name == "pve_volume_reference_index")

        class FakeGlob:
            @staticmethod
            def glob(pattern):
                return ["/etc/pve/nodes/n/qemu-server/100.conf"] \
                    if pattern == "/etc/pve/nodes/*/qemu-server/*.conf" else []

        content = (
            "description: thick:vm-100-disk-9,size=4G\n"
            "scsi0: cache=none,file=thick:vm-100-disk-0,size=4G,size=3G\n"
            "unused0: thick:vm-100-disk-1\n"
            "scsi1: thick:vm-100-disk-2,file=thick:vm-100-disk-3,size=4G\n"
            "scsi99999: thick:vm-100-disk-4,size=4G\n"
            "unused1: thick:vm-100-disk-5\n"
            "scsi2: cache=none,file=thick:vm-100-disk-5,size=3G\n"
        )
        namespace = {
            "re": re, "glob": FakeGlob, "read_file": lambda _path: content,
            "Decimal": __import__("decimal").Decimal,
            "InvalidOperation": __import__("decimal").InvalidOperation,
            "collect_pve_inventory": PVE_INVENTORY.collect,
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(HEALTH), "exec"),
             namespace)
        _references, sizes, errors = namespace["pve_volume_reference_index"](
            include_current_sizes=True
        )
        self.assertEqual(errors, [])
        self.assertNotIn("thick:vm-100-disk-9", sizes)
        self.assertEqual(sizes["thick:vm-100-disk-0"][0][1:], (None, "attached"))
        self.assertEqual(sizes["thick:vm-100-disk-1"][0][1:], (None, "detached"))
        self.assertEqual(sizes["thick:vm-100-disk-2"][0][2], "invalid")
        self.assertEqual(sizes["thick:vm-100-disk-3"][0][2], "invalid")
        self.assertEqual(sizes["thick:vm-100-disk-4"][0][2], "invalid")
        self.assertEqual(
            [entry[2] for entry in sizes["thick:vm-100-disk-5"]],
            ["detached", "attached"],
        )

    def test_reference_index_accepts_reordered_lxc_volume_at_schema_boundary(self):
        tree = ast.parse(HEALTH.read_text(encoding="utf-8"))
        node = next(item for item in tree.body if isinstance(item, ast.FunctionDef)
                    and item.name == "pve_volume_reference_index")

        class FakeGlob:
            @staticmethod
            def glob(pattern):
                return ["/etc/pve/nodes/n/lxc/100.conf"] \
                    if pattern == "/etc/pve/nodes/*/lxc/*.conf" else []

        content = (
            "mp255: mp=/data,volume=thick:vm-100-disk-0,size=1.5G\n"
            "mp256: mp=/bad,volume=thick:vm-100-disk-1,size=4G\n"
        )
        namespace = {
            "re": re, "glob": FakeGlob, "read_file": lambda _path: content,
            "Decimal": __import__("decimal").Decimal,
            "InvalidOperation": __import__("decimal").InvalidOperation,
            "collect_pve_inventory": PVE_INVENTORY.collect,
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(HEALTH), "exec"),
             namespace)
        _references, sizes, errors = namespace["pve_volume_reference_index"](
            include_current_sizes=True
        )
        self.assertEqual(errors, [])
        self.assertEqual(
            sizes["thick:vm-100-disk-0"][0][1:],
            (int(1.5 * 2**30), "attached"),
        )
        self.assertEqual(sizes["thick:vm-100-disk-1"][0][2], "invalid")

    def evaluate_with_counts(self, counts):
        tree = ast.parse(HEALTH.read_text(encoding="utf-8"))
        node = next(
            item for item in tree.body
            if isinstance(item, ast.FunctionDef)
            and item.name == "evaluate_thick_anchor_references"
        )
        namespace = {
            "re": re,
            "pve_volume_reference_files": lambda volid: [
                f"config-{index}" for index in range(counts.get(volid, 0))
            ],
        }
        exec(
            compile(ast.Module(body=[node], type_ignores=[]), str(HEALTH), "exec"),
            namespace,
        )
        return namespace["evaluate_thick_anchor_references"]

    def test_exactly_one_reference_passes(self):
        evaluate = self.evaluate_with_counts({"thick:vm-100-disk-0": 1})
        result = evaluate("thick", [{
            "name": "sltg-a-key", "volume": "vm-100-disk-0",
            "phase": "MATERIALIZED",
        }])
        self.assertEqual(result[0]["status"], "PASS")
        self.assertEqual(result[0]["reference_count"], 1)

    def test_stale_current_pve_size_is_recovery_required(self):
        evaluate = self.evaluate_with_counts({"thick:vm-100-disk-0": 1})
        result = evaluate(
            "thick",
            [{
                "name": "sltg-a-key", "volume": "vm-100-disk-0",
                "phase": "MATERIALIZED", "head_size_bytes": 4 * 2**30,
            }],
            reference_index={"thick:vm-100-disk-0": ["config"]},
            current_size_index={
                "thick:vm-100-disk-0": [("config", 3 * 2**30, "attached")]
            },
        )
        self.assertEqual(result[0]["status"], "FAIL")
        self.assertEqual(
            result[0]["materialization_state"], "RECOVERY_REQUIRED"
        )
        self.assertIn("differs from authoritative HEAD", result[0]["reason"])

    def test_detached_unused_reference_does_not_require_declared_size(self):
        evaluate = self.evaluate_with_counts({"thick:vm-100-disk-0": 1})
        result = evaluate(
            "thick",
            [{"name": "sltg-a-key", "volume": "vm-100-disk-0",
              "phase": "MATERIALIZED", "head_size_bytes": 4 * 2**30}],
            reference_index={"thick:vm-100-disk-0": ["config"]},
            current_size_index={
                "thick:vm-100-disk-0": [("config", None, "detached")]
            },
        )
        self.assertEqual(result[0]["status"], "PASS")
        self.assertIn("detached", result[0]["reason"])

    def test_unreferenced_anchor_warns_without_cleanup_claim(self):
        evaluate = self.evaluate_with_counts({})
        result = evaluate("thick", [{
            "name": "sltg-a-key", "volume": "vm-100-disk-0",
            "phase": "MATERIALIZED",
        }])
        self.assertEqual(result[0]["status"], "WARN")
        self.assertIn("incomplete destination", result[0]["reason"])
        self.assertIn("before any explicit cleanup", result[0]["reason"])

    def test_multiple_references_are_ambiguous(self):
        evaluate = self.evaluate_with_counts({"thick:vm-100-disk-0": 2})
        result = evaluate("thick", [{
            "name": "sltg-a-key", "volume": "vm-100-disk-0",
            "phase": "MATERIALIZED",
        }])
        self.assertEqual(result[0]["status"], "FAIL")
        self.assertEqual(result[0]["materialization_state"], "RECOVERY_REQUIRED")
        self.assertIn("ambiguous", result[0]["reason"])

    def test_unreadable_config_evidence_fails_closed(self):
        evaluate = self.evaluate_with_counts({"thick:vm-100-disk-0": 1})
        result = evaluate(
            "thick",
            [{"name": "sltg-a-key", "volume": "vm-100-disk-0",
              "phase": "MATERIALIZED", "head_size_bytes": 4 * 2**30}],
            reference_index={"thick:vm-100-disk-0": ["config"]},
            current_size_index={
                "thick:vm-100-disk-0": [("config", 4 * 2**30, "attached")]
            },
            reference_evidence_errors=["unreadable.conf"],
        )
        self.assertEqual(result[0]["status"], "FAIL")
        self.assertIn("incomplete", result[0]["reason"])

    def test_thousand_anchors_use_prebuilt_index_without_rescanning(self):
        evaluate = self.evaluate_with_counts({})
        anchors = [
            {
                "name": f"sltg-a-{index}",
                "volume": f"vm-{900000 + index}-disk-0",
                "phase": "MATERIALIZED",
            }
            for index in range(1000)
        ]
        reference_index = {
            f"thick:vm-{900000 + index}-disk-0": [f"config-{index}"]
            for index in range(1000)
        }
        result = evaluate(
            "thick", anchors, reference_index=reference_index
        )
        self.assertEqual(len(result), 1000)
        self.assertTrue(all(item["status"] == "PASS" for item in result))
        self.assertTrue(all(item["reference_count"] == 1 for item in result))

    def test_active_materialization_is_visible_and_blocks_dependency_changes(self):
        evaluate = self.evaluate_with_counts({"thick:vm-100-disk-0": 1})
        result = evaluate("thick", [{
            "name": "sltg-a-key", "volume": "vm-100-disk-0",
            "phase": "HYDRATING", "transaction": "a" * 32,
        }], lambda _transaction, _sid, _volume: "RUNNING")
        self.assertEqual(result[0]["status"], "WARN")
        self.assertEqual(result[0]["materialization_state"], "IN_PROGRESS")
        self.assertIn("remain blocked", result[0]["reason"])

    def test_interrupted_materialization_is_recovery_required(self):
        evaluate = self.evaluate_with_counts({"thick:vm-100-disk-0": 1})
        result = evaluate("thick", [{
            "name": "sltg-a-key", "volume": "vm-100-disk-0",
            "phase": "HYDRATING", "transaction": "b" * 32,
        }], lambda _transaction, _sid, _volume: "ABSENT")
        self.assertEqual(result[0]["status"], "FAIL")
        self.assertEqual(result[0]["materialization_state"], "RECOVERY_REQUIRED")
        self.assertIn("thick-resume", result[0]["reason"])
        self.assertIn("no automatic repair", result[0]["reason"])
        self.assertIn("synchronous PVE foreground caller", result[0]["reason"])
        self.assertIn("do not start a second worker", result[0]["reason"])
        self.assertIn("lack of kernel hydration progress", result[0]["reason"])

    def test_every_persisted_transition_phase_is_reported_as_resumable(self):
        evaluate = self.evaluate_with_counts({"thick:vm-100-disk-0": 1})
        for phase in (
            "PREPARED", "SOURCE_READY", "COMMITTED", "HYDRATING",
            "HYDRATION_COMPLETE", "LINEAR_PIVOTED",
        ):
            with self.subTest(phase=phase):
                result = evaluate("thick", [{
                    "name": "sltg-a-key", "volume": "vm-100-disk-0",
                    "phase": phase, "transaction": "d" * 32,
                }], lambda _transaction, _sid, _volume: "ABSENT")
                self.assertEqual(result[0]["status"], "FAIL")
                self.assertEqual(
                    result[0]["materialization_state"], "RECOVERY_REQUIRED"
                )
                self.assertIn("thick-resume", result[0]["reason"])
                self.assertIn("do not start a second worker", result[0]["reason"])

    def test_ambiguous_materialization_phase_fails_closed(self):
        evaluate = self.evaluate_with_counts({"thick:vm-100-disk-0": 1})
        result = evaluate("thick", [{
            "name": "sltg-a-key", "volume": "vm-100-disk-0",
            "phase": "MAYBE", "transaction": "c" * 32,
        }], lambda _transaction, _sid, _volume: "RUNNING")
        self.assertEqual(result[0]["status"], "FAIL")
        self.assertEqual(result[0]["materialization_state"], "RECOVERY_REQUIRED")

    def test_failed_exact_worker_is_distinct_from_absent_worker(self):
        evaluate = self.evaluate_with_counts({"thick:vm-100-disk-0": 1})
        result = evaluate("thick", [{
            "name": "sltg-a-key", "volume": "vm-100-disk-0",
            "phase": "HYDRATING", "transaction": "d" * 32,
        }], lambda _transaction, _sid, _volume: "FAILED")
        self.assertEqual(result[0]["status"], "FAIL")
        self.assertEqual(result[0]["worker_state"], "FAILED")
        self.assertEqual(result[0]["materialization_state"], "RECOVERY_REQUIRED")
        self.assertIn("failed exact transaction worker", result[0]["reason"])
        self.assertIn("thick-resume", result[0]["reason"])

    def test_dstate_worker_is_not_reported_as_ordinary_running_work(self):
        evaluate = self.evaluate_with_counts({"thick:vm-100-disk-0": 1})
        result = evaluate("thick", [{
            "name": "sltg-a-key", "volume": "vm-100-disk-0",
            "phase": "HYDRATING", "transaction": "e" * 32,
        }], lambda _transaction, _sid, _volume: "BLOCKED_DSTATE")
        self.assertEqual(result[0]["status"], "FAIL")
        self.assertEqual(result[0]["worker_state"], "BLOCKED_DSTATE")
        self.assertEqual(result[0]["materialization_state"], "RECOVERY_REQUIRED")
        self.assertIn("uninterruptible kernel sleep", result[0]["reason"])
        self.assertIn("do not start another worker", result[0]["reason"])



class ThickWorkerStateTests(unittest.TestCase):
    def load_worker_state(self, cmdlines=None, unit_state="inactive"):
        tree = ast.parse(HEALTH.read_text(encoding="utf-8"))
        nodes = [
            item for item in tree.body
            if isinstance(item, ast.FunctionDef)
            and item.name in {"thick_worker_process_inventory", "thick_worker_state"}
        ]
        cmdlines = cmdlines or {}
        def systemctl_run(command):
            if isinstance(unit_state, dict):
                unit = command[2]
                suffix = unit.rsplit(".", 1)[-1]
                state = unit_state.get(suffix)
                return (0, state, "") if state is not None else (1, "", "")
            return (0, unit_state, "")

        def fake_open(path, _mode):
            if path in cmdlines:
                return io.BytesIO(cmdlines[path])
            if path.endswith("/stat"):
                pid = path.split("/")[2]
                return io.BytesIO(f"{pid} (worker) S 0 0 0\n".encode())
            raise FileNotFoundError(path)

        def fake_glob(pattern):
            suffix = "/cmdline" if pattern.endswith("/cmdline") else "/stat"
            return [path for path in cmdlines if path.endswith(suffix)]

        namespace = {
            "re": re,
            "os": os,
            "glob": types.SimpleNamespace(glob=fake_glob),
            "open": fake_open,
            "run": systemctl_run,
        }
        exec(
            compile(ast.Module(body=nodes, type_ignores=[]), str(HEALTH), "exec"),
            namespace,
        )
        return namespace["thick_worker_state"], namespace["thick_worker_process_inventory"]

    def test_prebuilt_process_inventory_avoids_per_anchor_proc_rescan(self):
        tree = ast.parse(HEALTH.read_text(encoding="utf-8"))
        nodes = [
            item for item in tree.body
            if isinstance(item, ast.FunctionDef)
            and item.name in {"thick_worker_process_inventory", "thick_worker_state"}
        ]
        glob_calls = []

        def fake_glob(pattern):
            glob_calls.append(pattern)
            return []

        namespace = {
            "re": re,
            "os": os,
            "glob": types.SimpleNamespace(glob=fake_glob),
            "open": mock.mock_open(),
            "run": lambda command: (0, "inactive", ""),
        }
        exec(
            compile(ast.Module(body=nodes, type_ignores=[]), str(HEALTH), "exec"),
            namespace,
        )
        inventory = namespace["thick_worker_process_inventory"]()
        worker = namespace["thick_worker_state"]
        for index in range(1000):
            self.assertEqual(
                worker(
                    f"{index:032x}", "thick", f"vm-{900000 + index}-disk-0",
                    inventory,
                ),
                "ABSENT",
            )
        self.assertEqual(
            glob_calls,
            ["/proc/[0-9]*/cmdline", "/proc/[0-9]*/stat"],
        )

    def test_thousand_transaction_units_use_four_bounded_batches(self):
        tree = ast.parse(HEALTH.read_text(encoding="utf-8"))
        node = next(
            item for item in tree.body
            if isinstance(item, ast.FunctionDef)
            and item.name == "thick_worker_unit_states"
        )
        commands = []

        def fake_run(command):
            commands.append(command)
            units = [item for item in command if item.endswith((".service", ".timer"))]
            blocks = []
            for unit in units:
                blocks.append(f"Id={unit}\nActiveState=inactive")
            return 0, "\n\n".join(blocks), ""

        namespace = {"re": re, "run": fake_run}
        exec(
            compile(ast.Module(body=[node], type_ignores=[]), str(HEALTH), "exec"),
            namespace,
        )
        transactions = [f"{index:032x}" for index in range(1000)]
        states = namespace["thick_worker_unit_states"](transactions)
        self.assertEqual(len(commands), 4)
        self.assertTrue(all(command[:2] == ["systemctl", "show"] for command in commands))
        self.assertTrue(all(len([
            item for item in command if item.endswith((".service", ".timer"))
        ]) <= 512 for command in commands))
        self.assertEqual(len(states), 1000)
        self.assertTrue(all(
            state == {"service": "inactive", "timer": "inactive"}
            for state in states.values()
        ))

    def test_failed_unit_batch_is_unknown_not_absent(self):
        tree = ast.parse(HEALTH.read_text(encoding="utf-8"))
        nodes = [
            item for item in tree.body
            if isinstance(item, ast.FunctionDef)
            and item.name in {
                "thick_worker_process_inventory", "thick_worker_unit_states",
                "thick_worker_state",
            }
        ]
        namespace = {
            "re": re,
            "os": os,
            "glob": types.SimpleNamespace(glob=lambda pattern: []),
            "open": mock.mock_open(),
            "run": lambda command: (124, "", "timed out"),
        }
        exec(
            compile(ast.Module(body=nodes, type_ignores=[]), str(HEALTH), "exec"),
            namespace,
        )
        transaction = "a" * 32
        units = namespace["thick_worker_unit_states"]([transaction])
        inventory = namespace["thick_worker_process_inventory"]()
        self.assertEqual(units, {})
        self.assertEqual(
            namespace["thick_worker_state"](
                transaction, "thick", "vm-100-disk-0", inventory, units
            ),
            "UNKNOWN",
        )

    def test_exact_explicit_resume_process_is_running(self):
        worker, _inventory = self.load_worker_state({
            "/proc/42/cmdline": (
                b"/usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize\0"
                b"--resume\0thick\0vm-100-disk-0\0"
            ),
        })
        self.assertEqual(worker("a" * 32, "thick", "vm-100-disk-0"), "RUNNING")

    def test_exact_explicit_resume_process_in_dstate_is_blocked(self):
        worker, _inventory = self.load_worker_state({
            "/proc/42/cmdline": (
                b"/usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize\0"
                b"--resume\0thick\0vm-100-disk-0\0"
            ),
            "/proc/42/stat": b"42 (shared worker name) D 0 0 0\n",
        }, unit_state="active")
        self.assertEqual(
            worker("a" * 32, "thick", "vm-100-disk-0"),
            "BLOCKED_DSTATE",
        )

    def test_exact_scheduled_worker_in_dstate_overrides_active_unit(self):
        transaction = "a" * 32
        worker, _inventory = self.load_worker_state({
            "/proc/43/cmdline": (
                b"/usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize\0"
                b"thick\0vm-100-disk-0\0snap-a\0SNAPSHOT\0"
                + transaction.encode() + b"\0"
            ),
            "/proc/43/stat": b"43 (worker with spaces) D 0 0 0\n",
        }, unit_state="active")
        self.assertEqual(
            worker(transaction, "thick", "vm-100-disk-0"),
            "BLOCKED_DSTATE",
        )

    def test_dstate_dmsetup_descendant_overrides_active_worker_unit(self):
        transaction = "b" * 32
        worker, _inventory = self.load_worker_state({
            "/proc/43/cmdline": (
                b"/usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize\0"
                b"thick\0vm-100-disk-0\0snap-a\0SNAPSHOT\0"
                + transaction.encode() + b"\0"
            ),
            "/proc/43/stat": b"43 (materialize worker) S 1 0 0\n",
            "/proc/44/stat": b"44 (timeout) S 43 0 0\n",
            "/proc/45/stat": b"45 (dmsetup) D 44 0 0\n",
        }, unit_state="active")
        self.assertEqual(
            worker(transaction, "thick", "vm-100-disk-0"),
            "BLOCKED_DSTATE",
        )

    def test_unrelated_dstate_process_is_not_attributed_to_worker(self):
        worker, _inventory = self.load_worker_state({
            "/proc/42/cmdline": (
                b"/usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize\0"
                b"--resume\0thick\0vm-100-disk-0\0"
            ),
            "/proc/42/stat": b"42 (materialize worker) S 1 0 0\n",
            "/proc/99/stat": b"99 (unrelated io) D 1 0 0\n",
        }, unit_state="active")
        self.assertEqual(worker("a" * 32, "thick", "vm-100-disk-0"), "RUNNING")

    def test_perl_interpreter_resume_process_is_running(self):
        worker, _inventory = self.load_worker_state({
            "/proc/42/cmdline": (
                b"/usr/bin/perl\0"
                b"/usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize\0"
                b"--resume\0thick\0vm-100-disk-0\0"
            ),
        })
        self.assertEqual(worker("a" * 32, "thick", "vm-100-disk-0"), "RUNNING")

    def test_near_match_resume_process_is_rejected(self):
        worker, _inventory = self.load_worker_state({
            "/proc/42/cmdline": (
                b"/usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize\0"
                b"--resume\0thick\0vm-100-disk-00\0"
            ),
        })
        self.assertEqual(worker("a" * 32, "thick", "vm-100-disk-0"), "ABSENT")

    def test_active_canonical_transaction_unit_is_running(self):
        worker, _inventory = self.load_worker_state(unit_state="active")
        self.assertEqual(worker("a" * 32, "thick", "vm-100-disk-0"), "RUNNING")

    def test_failed_canonical_transaction_unit_is_failed(self):
        worker, _inventory = self.load_worker_state(unit_state="failed")
        self.assertEqual(worker("a" * 32, "thick", "vm-100-disk-0"), "FAILED")

    def test_failed_service_is_not_masked_by_lingering_active_timer(self):
        worker, _inventory = self.load_worker_state(unit_state={
            "service": "failed",
            "timer": "active",
        })
        self.assertEqual(worker("a" * 32, "thick", "vm-100-disk-0"), "FAILED")

    def test_explicit_resume_process_supersedes_old_failed_unit(self):
        worker, _inventory = self.load_worker_state({
            "/proc/42/cmdline": (
                b"/usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize\0"
                b"--resume\0thick\0vm-100-disk-0\0"
            ),
        }, unit_state={"service": "failed", "timer": "inactive"})
        self.assertEqual(worker("a" * 32, "thick", "vm-100-disk-0"), "RUNNING")


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
