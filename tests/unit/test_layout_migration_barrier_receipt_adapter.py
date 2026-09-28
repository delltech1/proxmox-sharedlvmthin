import copy
import importlib.util
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


M = load("receipt_adapter_test", ROOT / "experiments/thick-generations/layout-migration-barrier-receipt-adapter.py")
H = load("receipt_adapter_fixture", ROOT / "tests/unit/test_layout_migration_v2_v1_adapter.py")
R = load("receipt_runner_test", ROOT / "experiments/thick-generations/layout-migration-barrier-node-runner.py")


class Tests(unittest.TestCase):
    def fixture(self, mode="remote-audit"):
        base, target, template, ctx, preflights, _, auth, now = H.Tests().fixture(mode)
        workload = {"schema": "slt-workload-prepare/v1", "verdict": "SNAPSHOT_READY",
                    "managed_storages": sorted(M.B.WORKLOAD.storage_ids(base)),
                    "storage_cfg_sha256": M.B.digest(base), "running_consumers": [],
                    "consumers": [], "config_sha256": {}}
        workload["evidence_sha256"] = M.B.digest(M.B.WORKLOAD.canonical(workload))
        names = [row["name"] for row in ctx["nodes"]]
        plan = {"schema": "slt-maintenance-barrier-model/v1", "tx": ctx["tx"],
                "participants": [{"node": row["name"], "boot_id": row["boot_id"],
                                  "san_role": row["san_role"] == "SAN_PARTICIPANT"} for row in ctx["nodes"]],
                "storage_cfg_sha256": M.B.digest(base), "workload_snapshot_sha256": workload["evidence_sha256"],
                "candidate_sha256": ctx["candidate"]["deb_sha256"], "issued_at": now - 100,
                "deadline": now + 100, "actions": [{"step": step, "node": node} for step in M.B.MODEL.STEPS for node in names]}
        receipts = []
        for participant in plan["participants"]:
            node, boot = participant["node"], participant["boot_id"]
            services = []
            for unit in M.B.WATCHED:
                active = unit in ("pve-cluster.service", "corosync.service") or (
                    participant["san_role"] and unit == "multipathd.service") or (
                    ctx["thinguard_required_by_node"][node] and unit == "pve-sharedlvmthin-thin-guard.service")
                services.append({"unit": unit, "active_state": "active" if active else "inactive",
                                 "sub_state": "running" if active else "dead", "main_pid": 9 if active else 0,
                                 "control_pid": 0, "cgroup_empty": not active, "job": None,
                                 "mask_target": "/etc/systemd/system:/dev/null" if unit in M.B.MODEL.RESTARTABLE_MASKS else None})
            raw = {"observation": {"node": node, "boot_id": boot, "membership": names,
                    "storage_cfg_sha256": plan["storage_cfg_sha256"], "workload_snapshot_sha256": workload["evidence_sha256"],
                    "quorate": True, "ha_state": "DRAINED", "tasks": [], "workers": [], "pending_jobs": []},
                   "storage_config": base.decode(), "workload": copy.deepcopy(workload), "guests": [],
                   "inventory_complete": True, "managed_mappers": [], "managed_open_lvs": [], "services": services}
            request = {"tx": ctx["tx"], "plan_sha256": M.B.MODEL.frozen_hash(plan), "node": node, "operation": "OBSERVE"}
            attempts = {}
            for operation in ("ENTRY_BLOCK", "SERVICE_DRAIN"):
                op_request = {**request, "operation": operation}
                attempts[operation] = {"state": "COMPLETED_HISTORICAL", "retry_authorized": False,
                    "release_authorized": False, "records": [
                        {"schema": "slt-barrier-node-attempt/v1", "request": copy.deepcopy(op_request),
                         "boot_id": boot, "state": "ATTEMPT_RESERVED"},
                        {"schema": "slt-barrier-node-result/v1", "request_sha256": M.B.digest(M.L.canonical(op_request)),
                         "state": "OBSERVED_COMPLETE", "response": {"request": copy.deepcopy(op_request), "evidence": copy.deepcopy(raw)}}]}
            receipts.append({"schema": "slt-barrier-node-runner/v2", "verdict": "COMPLETED",
                             "started_at": now - 5, "observed_at": now - 4, "attempts": attempts,
                             "response": {"request": request, "evidence": raw}})
        return [ctx, template, base, target, plan, receipts, preflights, auth, now]

    def test_exact_existing_contract_accepts_all_four_nodes(self):
        f = self.fixture()
        before = copy.deepcopy(f)
        barrier = M.project(*f)
        self.assertEqual(f, before)
        self.assertEqual(barrier["observed_at"], f[-1] - 5)
        result = M.A.project(f[0], f[1], f[2], f[3], f[6], barrier, f[7], f[8])
        self.assertEqual(result["sidecar"]["actions"][-1]["action"], "VERIFY_CURRENT_ONLY")
        barrier["nodes"][0]["boot_id"] = "changed"
        self.assertEqual(f, before)

    def test_active_guard_requires_fresh_matching_protocol_and_pid(self):
        f = self.fixture("runtime-guard")
        self.assertEqual(M.project(*f)["authorization"], "HOLD_ACTIVE")
        for mutate in (
                lambda g: g.update(daemon_pid=99),
                lambda g: g["samples"][0].update(observed_at=f[-1] - 10),
                lambda g: g["samples"][1].update(watchdog="ARMED")):
            bad = copy.deepcopy(f)
            mutate(bad[6][0]["role_evidence"]["thinguard"])
            with self.assertRaises(M.Refusal): M.project(*bad)

    def test_barrier_flows_through_offline_composer_without_schema_changes(self):
        f = self.fixture()
        barrier = M.project(*f)
        topology = {**f[1], "storage_cfg_sha256": M.B.digest(f[2])}
        evidence = M.A.CTX.TOPO.validate(topology, f[2])
        result = M.C.compose({"tx": f[0]["tx"], "generation": f[0]["generation"]},
            topology, evidence, f[6], f[2], f[3], "pve01", f[8], barrier=barrier,
            adapter_auth_fields={key: f[7][key] for key in ("authorization_id", "issued_at", "expires_at")})
        self.assertIsNotNone(result["prepare"])
        self.assertEqual(result["native_cas_request"]["evidence"]["barrier_sha256"], M.A.digest(barrier))
        self.assertFalse(result["execution_authorized"])
        self.assertFalse(result["mutation_performed"])

    def test_archived_or_untimed_receipt_cannot_be_promoted(self):
        for mutate in (
                lambda r: r.update(schema="slt-barrier-node-runner/v1"),
                lambda r: r.pop("started_at"),
                lambda r: r.update(started_at=1, observed_at=2),
                lambda r: r.update(started_at=True),
                lambda r: r.update(observed_at=2_000_000_001),
                lambda r: r.update(started_at=2_000_000_000, observed_at=1_999_999_999)):
            f = self.fixture(); mutate(f[5][0])
            with self.assertRaises(M.Refusal): M.project(*f)

    def test_evaluator_clock_does_not_refresh_receipts(self):
        f = self.fixture()
        f[-1] += 101
        with self.assertRaises(M.Refusal): M.project(*f)

    def test_wrong_plan_candidate_configuration_role_boot_and_transaction_refuse(self):
        for mutate in (
                lambda p: p.update(candidate_sha256="f" * 64),
                lambda p: p.update(storage_cfg_sha256="f" * 64),
                lambda p: p.update(tx="f" * 32),
                lambda p: p["participants"][0].update(boot_id="99999999-9999-4999-8999-999999999999"),
                lambda p: p["participants"].reverse()):
            f = self.fixture(); mutate(f[4])
            with self.assertRaises(M.Refusal): M.project(*f)

    def test_missing_duplicate_and_foreign_node_refuse(self):
        for mutate in (
                lambda rs: rs.pop(), lambda rs: rs.append(copy.deepcopy(rs[0])),
                lambda rs: rs.__setitem__(1, copy.deepcopy(rs[0])),
                lambda rs: rs[0]["response"]["request"].update(node="other")):
            f = self.fixture(); mutate(f[5])
            with self.assertRaises(M.Refusal): M.project(*f)

    def test_final_observation_must_follow_effects_and_remain_quiescent(self):
        for mutate in (
                lambda r: r["response"]["request"].update(operation="SERVICE_DRAIN"),
                lambda r: r["response"]["evidence"]["observation"].update(quorate=False),
                lambda r: r["response"]["evidence"]["observation"].update(workers=["orphan"]),
                lambda r: r["response"]["evidence"].update(inventory_complete=False),
                lambda r: r["response"]["evidence"].update(managed_mappers=["open"]),
                lambda r: next(s for s in r["response"]["evidence"]["services"]
                               if s["unit"] == "pvedaemon.service").update(mask_target=None)):
            f = self.fixture(); mutate(f[5][0])
            with self.assertRaises(M.Refusal): M.project(*f)

    def test_completed_latch_binding_is_mandatory(self):
        for mutate in (
                lambda a: a.update(state="RECOVERY_REQUIRED"),
                lambda a: a.update(retry_authorized=True),
                lambda a: a["records"].pop(),
                lambda a: a["records"][0].update(boot_id="foreign"),
                lambda a: a["records"][1].update(request_sha256="0" * 64),
                lambda a: a["records"][1]["response"]["request"].update(operation="OBSERVE"),
                lambda a: a["records"][1]["response"]["evidence"].update(storage_config="foreign")):
            f = self.fixture(); mutate(f[5][0]["attempts"]["SERVICE_DRAIN"])
            with self.assertRaises(M.Refusal): M.project(*f)

    def test_no_caller_authority_flag_or_unknown_fields(self):
        for mutate in (
                lambda f: f[5][0].update(rollout_authorized=True),
                lambda f: f[5][0]["attempts"].update(RELEASE={}),
                lambda f: f[7].update(effect="install"),
                lambda f: f[6][0].update(observed_at=f[-1] - 10),
                lambda f: f[6][0].update(role_evidence=None),
                lambda f: f[6][0]["workers"].update(proc_inventory_complete=False)):
            f = self.fixture(); mutate(f)
            with self.assertRaises(M.Refusal): M.project(*f)

    def test_evidence_digest_binds_latch_and_guard_sources(self):
        f = self.fixture()
        original = M.project(*f)["nodes"][0]["evidence_sha256"]
        f[6][0]["challenge"] = "e" * 32
        self.assertNotEqual(M.project(*f)["nodes"][0]["evidence_sha256"], original)

    def test_runner_inspects_two_latches_before_new_observation(self):
        f = self.fixture(); receipt = f[5][0]; events = []
        agent = mock.Mock(plan=f[4], node="pve01")
        def inspect(request):
            events.append(request["operation"])
            return copy.deepcopy(receipt["attempts"][request["operation"]])
        def observe(request):
            events.append(request["operation"])
            return copy.deepcopy(receipt["response"])
        agent.request.side_effect = observe
        clock = mock.Mock(side_effect=[f[-1] - 5, f[-1] - 4])
        result = R.observe_receipt(agent, receipt["response"]["request"], inspect=inspect, clock=clock)
        self.assertEqual(result, receipt)
        self.assertEqual(events, ["ENTRY_BLOCK", "SERVICE_DRAIN", "OBSERVE"])

    def test_runner_v2_never_dispatches_effect(self):
        f = self.fixture(); agent = mock.Mock(plan=f[4], node="pve01")
        for operation in ("ENTRY_BLOCK", "SERVICE_DRAIN", "RELEASE"):
            with self.assertRaises(R.Refusal):
                R.observe_receipt(agent, {**f[5][0]["response"]["request"], "operation": operation})
        agent.request.assert_not_called()

    def test_runner_incomplete_latch_prevents_observe(self):
        f = self.fixture(); agent = mock.Mock(plan=f[4], node="pve01")
        with self.assertRaises(R.Refusal):
            R.observe_receipt(agent, f[5][0]["response"]["request"],
                              inspect=lambda request: {"state": "RECOVERY_REQUIRED"}, clock=lambda: f[-1])
        agent.request.assert_not_called()

    def test_runner_clock_regression_and_deadline_overrun_refuse(self):
        for times in ([2_000_000_000, 1_999_999_999], [2_000_000_000, 2_000_000_101]):
            f = self.fixture(); receipt = f[5][0]
            agent = mock.Mock(plan=f[4], node="pve01")
            agent.request.return_value = receipt["response"]
            with self.assertRaises(R.Refusal):
                R.observe_receipt(agent, receipt["response"]["request"],
                    inspect=lambda request: receipt["attempts"][request["operation"]], clock=mock.Mock(side_effect=times))


if __name__ == "__main__":
    unittest.main()
