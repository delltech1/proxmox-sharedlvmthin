import copy
import hashlib
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


M = load("target_service_plan", ROOT / "experiments/thick-generations/layout-migration-target-service-plan.py")
H = load("target_service_fixture", ROOT / "tests/unit/test_layout_migration_all_certified_v2.py")


def raw_receipt_sha(value):
    return hashlib.sha256(M.canonical(value) + b"\n").hexdigest()


class TargetServicePlanTests(unittest.TestCase):
    def fixture(self):
        args = H.AllCertifiedV2Tests().fixture()
        keys = ("context", "topology_template", "baseline_raw", "target_raw", "configured", "refresh_authorization",
                "stored_ready", "release_authorization", "ready_records", "certificate_raw", "collection", "records")
        inputs = dict(zip(keys, args[:12]))
        certified = M.CERTIFIED.evaluate(**inputs, now=args[12])
        context = inputs["context"]
        archives = []
        for node in certified["release_nodes"]:
            hold = certified["hold_by_node"][node]
            archives.append({"schema": "slt-layout-local-hold-release-receipt/v1", "tx": context["tx"],
                "generation": context["generation"], "commit_id": certified["commit_id"], "node": node,
                "boot_id": certified["participant_boots"][node], "authorization_sha256": "a" * 64,
                "all_certified_plan_sha256": certified["plan_sha256"], "certificate_sha256": certified["certificate_sha256"],
                "manifest_sha256": hold["manifest_sha256"], "attempt_sha256": "b" * 64,
                "source_identity": copy.deepcopy(hold["source_identity"]), "archive_identity": copy.deepcopy(hold["source_identity"]),
                "archive_name": f"{context['tx']}-{context['generation']}-{certified['commit_id']}.json",
                "rename_outcome": "EXACT_ARCHIVE_CONFIRMED", "directory_sync_complete": True,
                "started_at": args[12] + 1, "finished_at": args[12] + 2,
                "classification": "LOCAL_HOLD_ARCHIVED", "cluster_released": False})
        plan_id, workload_sha = "d" * 32, "e" * 64
        start, end = args[12] + 3, args[12] + 6
        before = []
        for i, node in enumerate(certified["nodes"]):
            source = inputs["records"][i]
            san = context["node_roles"][node] == "SAN_PARTICIPANT"
            guard = copy.deepcopy(source["thinguard"])
            if context["thinguard_required_by_node"][node]:
                for offset, sample in enumerate(guard["samples"], 1):
                    sample["observed_at"] = start + offset
            services = []
            for j, unit in enumerate(M.WATCHED):
                active = unit in ("corosync.service", "pve-cluster.service") or (san and unit == "multipathd.service")
                active = active or (unit == "pve-sharedlvmthin-thin-guard.service" and context["thinguard_required_by_node"][node])
                pid, stime = (500 + j, 1000 + j) if active else (0, 0)
                if active and unit == "pve-sharedlvmthin-thin-guard.service":
                    pid, stime = guard["daemon_pid"], guard["daemon_starttime"]
                mask = None
                if unit in M.TARGET_UNITS or unit == "pve-guests.service":
                    mask = {"path": "/etc/systemd/system/" + unit, "target": "/dev/null",
                            "dev": 100 + i, "ino": 1000 + j, "uid": 0, "mtime_ns": start * 10**9}
                services.append({"unit": unit, "active_state": "active" if active else "inactive",
                    "sub_state": "running" if active else "dead", "main_pid": pid, "starttime": stime,
                    "control_pid": 0, "job": None, "cgroup_empty": not active,
                    "persistent_mask": mask, "runtime_override": False})
            before.append({"schema": "slt-target-service-before/v1", "plan_id": plan_id,
                "challenge": str(i + 1) * 32, "collector_sha256": "f" * 64, "node": node,
                "role": context["node_roles"][node], "boot_id_start": source["boot_id_start"], "boot_id_end": source["boot_id_end"],
                "tx": context["tx"], "generation": context["generation"], "context_sha256": context["context_sha256"],
                "observed_start": start, "observed_end": end, "control_plane": copy.deepcopy(source["control_plane"]),
                "installed": copy.deepcopy(source["installed"]), "payload": copy.deepcopy(source["payload"]),
                "workers": copy.deepcopy(source["workers"]), "hold": {"active": False, "namespace_verified": True},
                "archive_receipt_sha256": raw_receipt_sha(archives[i]) if san else None, "services": services,
                "workload": {"snapshot_sha256": workload_sha, "inventory_complete": True, "managed_running_guests": [],
                    "managed_mappers": [], "managed_open_lvs": [], "ha_start_demands": [], "scheduled_start_demands": []},
                "thinguard": guard, "vg_identities": copy.deepcopy(source["vg_identities"])})
        targets = [{"node": node, "units": [{"unit": unit, "target_active": True, "target_persistent_mask": False}
                    for unit in M.TARGET_UNITS], "preserve_units": list(M.PRESERVE_UNITS)} for node in certified["nodes"]]
        return [inputs, archives, before, targets, plan_id, workload_sha, end]

    def reject(self, change):
        args = self.fixture()
        change(args)
        with self.assertRaises(M.Refusal):
            M.evaluate(*args)

    def test_exact_post_archive_desired_state_is_only_a_plan(self):
        args = self.fixture()
        result = M.evaluate(*args)
        self.assertEqual(result["authorization"], "NONE")
        for key in ("mutation_performed", "execution_authorized", "runtime_qualified"):
            self.assertIs(result[key], False)
        self.assertEqual(result["phase_order"], ["BACKEND", "API", "HA", "SCHEDULER"])
        self.assertEqual(result["targets"], args[3])
        self.assertEqual(len(result["targets"]), 4)
        self.assertEqual(set(result["archive_receipt_sha256"]), {"pve01", "pve02", "pve03"})
        self.assertIn("pve04", result["observed_before_sha256"])
        self.assertEqual(result["archive_receipt_sha256"]["pve01"], raw_receipt_sha(args[1][0]))
        self.assertNotEqual(result["archive_receipt_sha256"]["pve01"], M.digest(args[1][0]))
        body = dict(result); claimed = body.pop("plan_sha256")
        self.assertEqual(claimed, M.digest(body))
        self.assertTrue(all(p["require_all_nodes_previous_phase"] is True for p in result["phases"]))

    def test_pure_deterministic_and_input_output_isolation(self):
        args = self.fixture(); saved = copy.deepcopy(args)
        with patch("subprocess.run", side_effect=AssertionError("no executor")), patch("builtins.open", side_effect=AssertionError("no I/O")):
            first = M.evaluate(*args); second = M.evaluate(*args)
        self.assertEqual(first, second); self.assertEqual(args, saved)
        first["targets"][0]["units"].clear()
        first["preserved_states"]["pve01"].clear()
        self.assertEqual(args, saved)

    def test_receipt_required_fields_and_exact_values(self):
        for field in M.RECEIPT_FIELDS:
            with self.subTest(missing=field): self.reject(lambda a, f=field: a[1][0].pop(f))
        variants = {"schema": "foreign", "tx": "0" * 32, "generation": True, "commit_id": "0" * 32,
            "node": "pve04", "boot_id": "foreign", "authorization_sha256": "bad", "attempt_sha256": "bad",
            "all_certified_plan_sha256": "0" * 64, "certificate_sha256": "0" * 64, "manifest_sha256": "0" * 64,
            "archive_name": "../escape", "rename_outcome": "UNKNOWN", "directory_sync_complete": 1,
            "started_at": 0, "finished_at": True, "classification": "UNKNOWN_RETAIN", "cluster_released": True}
        for field, value in variants.items():
            with self.subTest(field=field): self.reject(lambda a, f=field, v=value: a[1][0].update({f: v}))
        for field in ("source_identity", "archive_identity"):
            with self.subTest(identity=field): self.reject(lambda a, f=field: a[1][0][f].update(ino=999999))

    def test_archive_cohort_and_time_order(self):
        for change in (lambda a: a[1].pop(), lambda a: a[1].append(copy.deepcopy(a[1][0])),
                       lambda a: a[1].__setitem__(1, copy.deepcopy(a[1][0])),
                       lambda a: a[1][0].update(finished_at=a[6] + 1),
                       lambda a: a[1][0].update(started_at=a[1][0]["finished_at"] + 1),
                       lambda a: a[1][0].update(started_at=1),
                       lambda a: a[1][0].update(unexpected=True)):
            with self.subTest(change=change): self.reject(change)

    def test_before_coverage_closed_schema_and_freshness(self):
        for field in self.fixture()[2][0]:
            with self.subTest(missing=field): self.reject(lambda a, f=field: a[2][0].pop(f))
        variants = {"schema": "foreign", "plan_id": "0" * 32, "challenge": "bad", "collector_sha256": "bad",
            "node": "foreign", "role": "CONTROL_ONLY", "boot_id_start": "foreign", "boot_id_end": "foreign",
            "tx": "0" * 32, "generation": 1.0, "context_sha256": "0" * 64, "observed_start": 1,
            "observed_end": True, "archive_receipt_sha256": "0" * 64}
        for field, value in variants.items():
            with self.subTest(field=field): self.reject(lambda a, f=field, v=value: a[2][0].update({f: v}))
        for change in (lambda a: a[2].pop(), lambda a: a[2].append(copy.deepcopy(a[2][0])),
                       lambda a: a[2].__setitem__(1, copy.deepcopy(a[2][0])),
                       lambda a: a[2][0].update(observed_start=a[1][0]["finished_at"] - 1),
                       lambda a: a[2][0].update(observed_end=a[6] + 1),
                       lambda a: a[2][1].update(challenge=a[2][0]["challenge"]),
                       lambda a: a[2][1].update(collector_sha256="1" * 64),
                       lambda a: a[2][0].update(extra=True)):
            with self.subTest(change=change): self.reject(change)

    def test_control_only_has_no_archive_and_no_hold_but_explicit_service_targets(self):
        result = M.evaluate(*self.fixture())
        self.assertEqual(result["node_roles"]["pve04"], "CONTROL_ONLY")
        self.assertEqual(len(result["targets"][3]["units"]), 8)
        self.reject(lambda a: a[2][3].update(archive_receipt_sha256="0" * 64))
        self.reject(lambda a: a[2][3]["hold"].update(active=True))
        self.reject(lambda a: a[2][3].update(vg_identities=[{}]))

    def test_cohort_skew_and_coordinated_payload_drift_refuse(self):
        args = self.fixture()
        args[2][3]["observed_start"] += 70
        args[2][3]["observed_end"] += 70
        args[6] += 70
        with self.assertRaisesRegex(M.Refusal, "skew"):
            M.evaluate(*args)
        args = self.fixture()
        for row in args[2]:
            row["payload"]["package_file_list_sha256"] = "0" * 64
        with self.assertRaisesRegex(M.Refusal, "certification"):
            M.evaluate(*args)

    def test_control_plane_candidate_payload_workers_and_workload(self):
        cases = [("control_plane", "quorate", 1), ("control_plane", "target_storage_cfg_sha256", "0" * 64),
            ("control_plane", "cluster_nodes", ["pve01"]), ("installed", "dpkg_state", "unpacked"),
            ("installed", "artifact_sha256", "0" * 64), ("payload", "dpkg_verify_clean", False),
            ("payload", "dpkg_verify_complete", 1), ("payload", "package_file_list_sha256", "0" * 64),
            ("workers", "inventory_complete", False), ("workers", "storage_processes", [1]),
            ("workers", "transient_units", [1]), ("workers", "pve_tasks", [1]),
            ("hold", "active", 0), ("hold", "namespace_verified", False),
            ("workload", "snapshot_sha256", "0" * 64), ("workload", "inventory_complete", False)]
        cases += [("workload", field, [1]) for field in ("managed_running_guests", "managed_mappers", "managed_open_lvs",
                                                        "ha_start_demands", "scheduled_start_demands")]
        for obj, field, value in cases:
            with self.subTest(obj=obj, field=field): self.reject(lambda a, o=obj, f=field, v=value: a[2][0][o].update({f: v}))

    def test_no_target_expansion_aliases_or_reordering(self):
        for change in (lambda a: a[3][0]["units"].append({"unit": "pve-guests.service", "target_active": True, "target_persistent_mask": False}),
                       lambda a: a[3][0]["units"][0].update(unit="other.service"),
                       lambda a: a[3][0]["units"][0].update(target_active=1),
                       lambda a: a[3][0]["units"][0].update(target_persistent_mask=0),
                       lambda a: a[3][0]["units"][0].update(target_active=False),
                       lambda a: a[3][0]["units"].reverse(), lambda a: a[3].reverse(),
                       lambda a: a[3][0]["preserve_units"].pop(), lambda a: a[3].pop(),
                       lambda a: a[3][0].update(argv=["sh", "-c", "true"])):
            with self.subTest(change=change): self.reject(change)

    def test_service_before_is_exact_stopped_and_masked(self):
        index = M.WATCHED.index("pvedaemon.service")
        variants = {"active_state": "active", "sub_state": "failed", "main_pid": 1, "starttime": True,
                    "control_pid": 1, "job": 42, "cgroup_empty": False, "runtime_override": True,
                    "persistent_mask": None, "unit": "foreign.service"}
        for field, value in variants.items():
            with self.subTest(field=field): self.reject(lambda a, f=field, v=value: a[2][0]["services"][index].update({f: v}))
        for field, value in (("path", "/run/systemd/system/pvedaemon.service"), ("target", "/tmp/mask"),
                             ("dev", False), ("ino", 0), ("uid", True), ("mtime_ns", 0)):
            with self.subTest(mask=field): self.reject(lambda a, f=field, v=value: a[2][0]["services"][index]["persistent_mask"].update({f: v}))
        self.reject(lambda a: a[2][0]["services"].pop())
        self.reject(lambda a: a[2][0]["services"].reverse())
        self.reject(lambda a: a[2][0]["services"].__setitem__(1, copy.deepcopy(a[2][0]["services"][0])))

    def test_pve_guests_is_preserved_including_existing_mask_and_exited_state(self):
        args = self.fixture(); index = M.WATCHED.index("pve-guests.service")
        for row in args[2]:
            row["services"][index].update(active_state="active", sub_state="exited")
        result = M.evaluate(*args)
        for node in result["preserved_states"]:
            row = next(x for x in result["preserved_states"][node] if x["unit"] == "pve-guests.service")
            self.assertEqual(row["active_state"], "active")
            self.assertEqual(row["persistent_mask"]["target"], "/dev/null")
            self.assertNotIn("pve-guests.service", [u for _, units in M.PHASES for u in units])
        self.reject(lambda a: a[2][0]["services"][index].update(main_pid=42))

    def test_essential_and_guard_lifecycle_boundaries(self):
        for unit in ("corosync.service", "pve-cluster.service", "multipathd.service", "pve-sharedlvmthin-thin-guard.service"):
            index = M.WATCHED.index(unit)
            with self.subTest(unit=unit):
                self.reject(lambda a, i=index: a[2][0]["services"][i].update(active_state="inactive", sub_state="dead", main_pid=0, starttime=0, cgroup_empty=True))
        self.reject(lambda a: a[2][0]["thinguard"].update(daemon_pid=99999))
        self.reject(lambda a: a[2][0]["thinguard"]["samples"][0].update(observed_at=1))
        self.reject(lambda a: a[2][0]["thinguard"]["samples"][1].update(state="ARMED"))

    def test_certification_is_recomputed_not_accepted_by_hash_alone(self):
        self.reject(lambda a: a[0]["stored_ready"].update(authorization="RUN"))
        self.reject(lambda a: a[0]["records"].pop())
        self.reject(lambda a: a[0].update(certificate_raw=a[0]["certificate_raw"] + b"\n"))
        self.reject(lambda a: a[0].update(now=a[6]))
        self.reject(lambda a: a[0]["context"].update(generation=True))

    def test_bounds_unknown_types_and_cycles(self):
        args = self.fixture()
        for kwargs in ({"max_age": True}, {"max_age": 0}, {"max_age": 301}, {"max_skew": True}, {"max_skew": 121}):
            with self.subTest(kwargs=kwargs), self.assertRaises(M.Refusal): M.evaluate(*args, **kwargs)
        for index, value in ((4, "bad"), (5, "bad"), (6, True), (6, -1), (6, 1.0)):
            self.reject(lambda a, i=index, v=value: a.__setitem__(i, v))
        self.reject(lambda a: a[2][0].update(object=object()))
        self.reject(lambda a: a[3][0]["units"][0].update(unit=b"pvedaemon.service"))
        self.reject(lambda a: a[2].append(a[2]))
        self.reject(lambda a: a[2][0].update(oversize="x" * (2 * 1024 * 1024 + 1)))


if __name__ == "__main__":
    unittest.main()
