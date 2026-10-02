import copy
import hashlib
import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

with mock.patch.dict(sys.modules, {} if os.name == "posix" else {"fcntl": types.ModuleType("fcntl")}):
    import test_layout_migration_all_certified_v2 as fixtures
AC = fixtures.M
from test_layout_migration_exact_restore_model import Backend, M

ROOT = Path(__file__).resolve().parents[2] / "experiments/thick-generations"
SPEC = importlib.util.spec_from_file_location("exact_release_validator", ROOT / "layout-migration-exact-release-validator.py")
V = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(V)


def namespace(services):
    result = {}
    for i, (unit, state) in enumerate(services.items()):
        mask = state["masked"]
        result["/etc/systemd/system/" + unit] = {"mask_identity": [1, 200 + i, 41471, 0, 0, 1, 9, 1, 1], "target": "/dev/null"} if mask else None
        result["/run/systemd/system/" + unit] = None
        result["/usr/lib/systemd/system/" + unit] = {"identity": [1, 100 + i, 33188, 0, 0, 1, 100, 1, 1], "sha256": "a" * 64}
        result["effective:" + unit] = {"usrmerge_alias": None, "properties": {
            "Id": unit, "LoadState": "masked" if mask else "loaded",
            "FragmentPath": "/dev/null" if mask else "/usr/lib/systemd/system/" + unit,
            "DropInPaths": "", "SourcePath": "", "UnitFileState": "masked" if mask else "enabled",
            "Transient": "no", "NeedDaemonReload": "no"}}
    return result


def runtime(services, guests):
    return {"inventory_complete": True, "guests": guests, "managed_mappers": [], "managed_open_lvs": [], "ha_state": "DRAINED",
        "services": [{"unit": unit, "active_state": "active" if state["active"] else "inactive", "sub_state": state["substate"],
            "main_pid": 100 if state["active"] and unit != "pve-guests.service" else 0,
            "control_pid": 0, "cgroup_empty": not state["active"] or unit == "pve-guests.service", "job": None,
            "mask_target": "/etc/systemd/system:/dev/null" if state["masked"] else None} for unit, state in services.items()]}


class Tests(unittest.TestCase):
    def setUp(self):
        compatibility = mock.patch.dict(sys.modules, {} if os.name == "posix" else {"fcntl": types.ModuleType("fcntl")})
        compatibility.start(); self.addCleanup(compatibility.stop)
        args = fixtures.AllCertifiedV2Tests().fixture()
        self.now = args[-1] + 2
        certified = AC.evaluate(*args)
        context = args[0]
        candidate = context["candidate"]["artifact_sha256"]
        managed = sorted(line.split(":", 1)[1].strip() for line in args[2].decode().splitlines() if line.startswith("sharedlvmthin:"))
        self.vmconfig = f"scsi0: {managed[0]}:vm-100-disk-0\n"
        workload = {"schema": "slt-workload-prepare/v1", "observed_at": self.now - 100,
            "managed_storages": managed, "consumers": [{"vmid": 100, "type": "qemu", "name": "test", "node": "pve01", "status": "stopped",
                "disks": [{"key": "scsi0", "storage": managed[0], "volume": "vm-100-disk-0"}]}],
            "running_consumers": [], "storage_cfg_sha256": context["baseline_storage_cfg_sha256"], "resources_sha256": "a" * 64,
            "config_sha256": {"100": hashlib.sha256(self.vmconfig.encode()).hexdigest()}, "mutation_performed": False,
            "authorization": "NONE", "verdict": "SNAPSHOT_READY", "limitations": []}
        workload["evidence_sha256"] = V.digest(workload)
        self.plan = {"schema": "slt-exact-restore-model/v1", "tx": context["tx"],
            "participants": [{"node": n["name"], "boot_id": n["boot_id"], "role": "SAN" if i < 3 else "CONTROL_ONLY",
                              "before_package_sha256": "1" * 64 if i < 3 else candidate, "after_package_sha256": candidate}
                             for i, n in enumerate(context["nodes"])],
            "candidate_sha256": candidate, "storage_cfg_sha256": context["baseline_storage_cfg_sha256"],
            "target_storage_cfg_sha256": context["target_storage_cfg_sha256"], "workload_sha256": workload["evidence_sha256"],
            "issued_at": self.now - 100, "expires_at": self.now + 100}
        backend = Backend(self.plan); backend.time = self.now
        baseline = M.Coordinator(self.plan, backend).baseline()
        sources = {n: namespace(r["services"]) for n, r in baseline["records"].items()}
        backend.held()
        inputs = dict(zip(V.CERTIFIED_FIELDS, args[:-1]))
        for field in V.BYTE_FIELDS: inputs[field] = inputs[field].decode()
        self.e = {"schema": "slt-exact-local-release-evidence/v1", "authority": "NONE", "plan": self.plan,
            "baseline": baseline, "baseline_sources": sources, "baseline_captures": {}, "workload": workload, "current": {}, "certified_inputs": inputs,
            "archives": {}, "cohort_id": "4" * 32, "issued_at": self.now, "expires_at": self.now + 20}
        for node, record in backend.records.items():
            self.e["baseline_captures"][node] = {"schema": "slt-exact-local-baseline/v1", "authority": "NONE", "mutation_performed": False,
                "tx": self.plan["tx"], "plan_sha256": V.digest(self.plan), "observation": copy.deepcopy(baseline["records"][node]),
                "service_namespace": copy.deepcopy(sources[node]), "artifact_identity": [1, 900, 33188, 0, 0, 1, 65, 1, 1],
                "source_evidence_sha256": ["a" * 64, "b" * 64]}
            self.e["current"][node] = {"cohort_id": self.e["cohort_id"], "observed_at": self.now,
                "observation": record, "service_namespace": namespace(record["services"]),
                "runtime": runtime(record["services"], [{"vmid": 100, "type": "qemu", "node": node, "status": "stopped", "config": self.vmconfig}] if node == "pve01" else [])}
        for i, node in enumerate(certified["release_nodes"]):
            hold = certified["hold_by_node"][node]
            common = {"tx": certified["tx"], "generation": certified["generation"], "commit_id": certified["commit_id"],
                "node": node, "boot_id": certified["participant_boots"][node], "all_certified_plan_sha256": certified["plan_sha256"],
                "certificate_sha256": certified["certificate_sha256"], "manifest_sha256": hold["manifest_sha256"]}
            auth = {**common, "schema": "slt-live-hold-archive-authorization/v2", "context_sha256": certified["context_sha256"],
                "source_identity_sha256": V.digest(hold["source_identity"]), "certificate_identity_sha256": V.digest(hold["certificate_identity"]),
                "operation_id": str(i + 5) * 32, "issued_at": self.now - 1, "expires_at": self.now + 30, "effect": "archive-exact-active-once"}
            stable = {**common, "authorization_sha256": V.digest(auth), "source_identity": hold["source_identity"],
                      "archive_name": f"{certified['tx']}-{certified['generation']}-{certified['commit_id']}.json"}
            attempt = {**stable, "schema": "slt-layout-local-hold-release-attempt/v1", "executor_pid": 100 + i,
                       "executor_starttime": 10000, "started_at": self.now - 1}
            receipt = {**stable, "schema": "slt-layout-local-hold-release-receipt/v1",
                "attempt_sha256": hashlib.sha256(V.canonical(attempt) + b"\n").hexdigest(), "archive_identity": hold["source_identity"],
                "rename_outcome": "EXACT_ARCHIVE_CONFIRMED", "directory_sync_complete": True, "started_at": self.now - 1,
                "finished_at": self.now, "classification": "LOCAL_HOLD_ARCHIVED", "cluster_released": False}
            current = {"active_present": False, "archive_name": stable["archive_name"], "archive_identity": hold["source_identity"],
                "certificate_identity": hold["certificate_identity"], "receipt_sha256": hashlib.sha256(V.canonical(receipt) + b"\n").hexdigest(),
                "observed_at": self.now}
            self.e["archives"][node] = copy.deepcopy({"authorization": auth, "attempt": attempt, "receipt": receipt, "current": current})
        release = {"plan_sha256": V.digest(self.plan), "authority": "NONE", "all_certified_sha256": V.digest(certified),
                   "archive_sha256": {n: V.digest(a["receipt"]) for n, a in self.e["archives"].items()}}
        self.request = {"tx": self.plan["tx"], "plan_sha256": V.digest(self.plan), "node": "pve01",
            "boot_id": self.plan["participants"][0]["boot_id"], "unit": "pvedaemon.service", "operation": "UNMASK",
            "baseline_sha256": V.digest(baseline), "release_sha256": V.digest(release)}
        self.identity = {"schema": "slt-local-restore-source/v1", "authority": "NONE",
                         "package": {"package-artifact-sha256": {"sha256": candidate}}}
        self.sources = {n: (ROOT / n).read_bytes() for n in V.FILES}
        self.hashes = {n: hashlib.sha256(b).hexdigest() for n, b in self.sources.items()}

    def validator(self): return V.Validator.for_offline_test(self.sources, self.hashes)

    def before(self):
        n, unit = self.request["node"], self.request["unit"]
        current = self.e["current"][n]
        state = current["observation"]["services"][unit]
        return {"active": state["active"], "masked": state["masked"], "substate": state["substate"],
                "source": {prefix + unit: current["service_namespace"][prefix + unit] for prefix in
                           ("/etc/systemd/system/", "/run/systemd/system/", "/usr/lib/systemd/system/", "effective:")}}

    def run_value(self, validator=None, now=None):
        return (validator or self.validator()).evaluate(V.canonical(self.e), self.request, self.identity, self.before(), now=now or self.now)

    def refuse(self):
        with self.assertRaises(Exception) as caught: self.run_value()
        self.assertEqual(type(caught.exception).__name__, "Refusal", str(caught.exception))

    def test_full_real_v2_chain_yields_exact_short_grant_and_repeat_bracket(self):
        validator = self.validator(); result = self.run_value(validator)
        self.assertEqual(result["allowed_effect"], "UNMASK")
        self.assertEqual(result["request_sha256"], V.digest(self.request))
        self.assertEqual(self.run_value(validator, self.now + 1), result)
        self.assertEqual(result["expires_at"] - result["issued_at"], 20)
        with self.assertRaises(V.Refusal): V.Validator()

    def test_certified_labels_are_not_trusted(self):
        for field in ("stored_ready", "configured"):
            with self.subTest(field=field):
                original = copy.deepcopy(self.e)
                self.e["certified_inputs"][field]["phase"] = "FORGED"
                self.refuse(); self.e = original
        self.e["certified_inputs"]["records"][0]["payload"]["dpkg_verify_clean"] = False
        self.refuse()

    def test_stale_mixed_or_nonzero_cohort_refuses(self):
        changes = [lambda e: e["current"]["pve02"].update(cohort_id="8" * 32),
            lambda e: e["current"]["pve02"].update(observed_at=self.now - 1),
            lambda e: e["current"]["pve02"]["observation"].update(boot_id=self.plan["participants"][0]["boot_id"]),
            lambda e: e["current"]["pve02"]["observation"].update(consumers=["vm"]),
            lambda e: e["current"]["pve02"]["observation"].update(workers=["worker"]),
            lambda e: e["current"]["pve02"]["observation"].update(quorate=False),
            lambda e: e.update(expires_at=self.now - 1), lambda e: e["current"].pop("pve04")]
        original = copy.deepcopy(self.e)
        for change in changes:
            self.e = copy.deepcopy(original); change(self.e); self.refuse()

    def test_three_archive_receipts_need_attempt_and_current_exact_readback(self):
        changes = [lambda a: a["receipt"].update(directory_sync_complete=False),
            lambda a: a["receipt"].update(attempt_sha256="0" * 64),
            lambda a: a["attempt"].update(executor_pid=True),
            lambda a: a["current"].update(active_present=True),
            lambda a: a["current"].update(receipt_sha256="0" * 64),
            lambda a: a["current"]["archive_identity"].update(ino=1),
            lambda a: a["authorization"].update(operation_id="bad"),
            lambda a: a["receipt"].update(cluster_released=True)]
        original = copy.deepcopy(self.e)
        for change in changes:
            self.e = copy.deepcopy(original); change(self.e["archives"]["pve02"]); self.refuse()
        self.e = original; self.e["archives"]["pve04"] = copy.deepcopy(self.e["archives"]["pve01"]); self.refuse()

    def test_plan_config_package_workload_request_drift_refuses(self):
        original = copy.deepcopy(self.e)
        for field in ("package_sha256", "storage_cfg_sha256", "workload_sha256"):
            self.e = copy.deepcopy(original); self.e["current"]["pve02"]["observation"][field] = "0" * 64; self.refuse()
        self.e = original
        for field in ("baseline_sha256", "release_sha256", "plan_sha256"):
            old = self.request[field]; self.request[field] = "0" * 64; self.refuse(); self.request[field] = old
        self.identity["package"]["package-artifact-sha256"]["sha256"] = "0" * 64; self.refuse()

    def test_source_drift_dropins_and_preexisting_mask_refuse(self):
        original = copy.deepcopy(self.e)
        for node in ("pve01", "pve04"):
            self.e = copy.deepcopy(original)
            self.e["current"][node]["service_namespace"]["/usr/lib/systemd/system/pvedaemon.service"]["sha256"] = "b" * 64
            self.refuse()
        self.e = copy.deepcopy(original)
        self.e["current"]["pve01"]["service_namespace"]["effective:pvedaemon.service"]["properties"]["DropInPaths"] = "/etc/evil"
        self.refuse()
        self.e = original; self.request["unit"] = "spiceproxy.service"; self.refuse()

    def test_control_only_same_bound_request_and_start_baseline(self):
        self.request.update(node="pve04", boot_id=self.plan["participants"][3]["boot_id"])
        self.assertEqual(self.run_value()["allowed_effect"], "UNMASK")
        node, unit = self.request["node"], self.request["unit"]
        self.e["current"][node]["observation"]["services"][unit]["masked"] = False
        self.e["current"][node]["service_namespace"] = namespace(self.e["current"][node]["observation"]["services"])
        self.e["current"][node]["runtime"] = runtime(self.e["current"][node]["observation"]["services"], [])
        self.request["operation"] = "START"
        self.assertEqual(self.run_value()["allowed_effect"], "START")
        self.request["unit"] = "pvescheduler.service"; self.refuse()

    def test_source_bundle_pins_full_closure_and_never_reopens_disk(self):
        changed = dict(self.sources); changed[V.FILES[-1]] += b"\n# change\n"
        with self.assertRaises(V.Refusal): V.Validator.for_offline_test(changed, self.hashes)
        with self.assertRaises(V.Refusal): V.Validator.for_offline_test({k: v for k, v in self.sources.items() if k != V.FILES[-1]}, self.hashes)
        validator = self.validator()
        with self.assertRaises(TypeError): validator.code.sources[V.FILES[0]] = b"raise Exception('changed')"
        with mock.patch.object(V.importlib.util, "spec_from_file_location", side_effect=AssertionError("disk loader forbidden")):
            self.run_value(validator)
        with self.assertRaises(V.Refusal): validator.code.load("foreign.py")

    def test_replay_changed_request_cohort_and_clock_refuse(self):
        validator = self.validator(); self.run_value(validator)
        with self.assertRaises(V.Refusal): self.run_value(validator, self.now - 1)
        validator = self.validator(); self.run_value(validator)
        self.e["cohort_id"] = "5" * 32
        for row in self.e["current"].values(): row["cohort_id"] = self.e["cohort_id"]
        with self.assertRaises(V.Refusal): self.run_value(validator)

    def test_duplicate_noncanonical_and_unknown_evidence_refuse(self):
        raw = V.canonical(self.e)
        for bad in (raw + b"\n", b'{"x":1,"x":1}', b'{"x":NaN}'):
            with self.assertRaises(V.Refusal): self.validator().evaluate(bad, self.request, self.identity, self.before(), now=self.now)
        self.e["command"] = ["systemctl", "start", "anything"]; self.refuse()

    def test_zero_consumers_is_recomputed_not_trusted_label(self):
        original = copy.deepcopy(self.e)
        for change in (lambda r: r["guests"][0].update(status="running"), lambda r: r["guests"].clear(),
                       lambda r: r["guests"][0].update(config=self.vmconfig + "name: changed\n"),
                       lambda r: r.update(inventory_complete=False), lambda r: r.update(managed_mappers=["dm-1"]),
                       lambda r: r["services"][0].update(control_pid=42)):
            self.e = copy.deepcopy(original); change(self.e["current"]["pve01"]["runtime"]); self.refuse()

    def test_baseline_capture_inode_and_workload_digest_refuse(self):
        original = copy.deepcopy(self.e)
        self.e["baseline_captures"]["pve02"]["observation"]["services"]["pvedaemon.service"]["active"] = False
        self.refuse(); self.e = copy.deepcopy(original)
        self.e["baseline_captures"]["pve02"]["artifact_identity"][3] = 1000
        self.refuse(); self.e = copy.deepcopy(original)
        self.e["workload"]["consumers"][0]["status"] = "running"; self.refuse()

    def test_later_restore_phase_cannot_overtake_drained_backend(self):
        self.request["unit"] = "pveproxy.service"; self.refuse()

    def test_identity_type_confusion_and_durable_receipt_unknown_fields_refuse(self):
        original = copy.deepcopy(self.e)
        for change in (lambda e: e["baseline_captures"]["pve01"]["artifact_identity"].__setitem__(1, True),
                       lambda e: e["current"]["pve01"]["service_namespace"]["/usr/lib/systemd/system/pvedaemon.service"]["identity"].__setitem__(3, 1000),
                       lambda e: e["archives"]["pve01"]["receipt"].update(retry=True),
                       lambda e: e["archives"]["pve01"]["receipt"].update(finished_at=self.now + 1)):
            self.e = copy.deepcopy(original); change(self.e); self.refuse()


if __name__ == "__main__": unittest.main()
