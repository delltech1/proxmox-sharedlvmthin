import copy
import importlib.util
from pathlib import Path
import unittest

PATH = Path(__file__).resolve().parents[2] / "experiments/thick-generations/layout-migration-exact-restore-model.py"
SPEC = importlib.util.spec_from_file_location("exact_restore", PATH)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def fixture():
    participants = [{"node": f"pve0{i}", "boot_id": f"0000000{i}-0000-4000-8000-00000000000{i}",
        "role": "SAN" if i < 4 else "CONTROL_ONLY", "before_package_sha256": "a" * 64,
        "after_package_sha256": ("b" if i < 4 else "a") * 64} for i in range(1, 5)]
    return {"schema": "slt-exact-restore-model/v1", "tx": "1" * 32, "participants": participants,
            "candidate_sha256": "b" * 64, "storage_cfg_sha256": "c" * 64,
            "target_storage_cfg_sha256": "d" * 64, "workload_sha256": "e" * 64,
            "issued_at": 100, "expires_at": 1800}


class Backend:
    def __init__(self, plan):
        self.plan, self.store, self.effects = plan, {}, []
        self.time, self.cut, self.writes = 200, None, 0
        self.lost_ack, self.drift, self.unknown = False, None, False
        self.records = {}
        for row in plan["participants"]:
            services = {unit: {"active": True, "substate": "exited" if unit == "pve-guests.service" else "running",
                               "masked": False, "job": None, "control_pid": 0} for unit in M.UNITS}
            # Mixed pre-state: never start either of these originally inactive units.
            for unit, masked in (("spiceproxy.service", True), ("pvescheduler.service", False)):
                services[unit].update(active=False, substate="dead", masked=masked)
            self.records[row["node"]] = {"node": row["node"], "boot_id": row["boot_id"],
                "membership": [r["node"] for r in plan["participants"]], "quorate": True,
                "storage_cfg_sha256": plan["storage_cfg_sha256"], "package_sha256": row["before_package_sha256"],
                "workload_sha256": plan["workload_sha256"], "consumers": [], "workers": [], "tasks": [],
                "start_demands": [], "services": services}

    def now(self): return self.time
    def read(self, tx, key): return copy.deepcopy(self.store.get(key))
    def observe(self, node): return copy.deepcopy(self.records[node])
    def create_once(self, tx, key, value):
        if key in self.store: raise M.Refusal("create-only conflict")
        self.store[key] = copy.deepcopy(value)
        self.writes += 1
        if self.writes == self.cut: raise OSError("durable write lost ACK")
        return {"durable": True, "sha256": M.digest(value)}
    def verify_release(self, plan, observations):
        if self.unknown: raise M.Refusal("ALL_CERTIFIED unknown")
        return {"plan_sha256": M.digest(plan), "authority": "NONE", "all_certified_sha256": "f" * 64,
                "archive_sha256": {r["node"]: "a" * 64 for r in plan["participants"] if r["role"] == "SAN"}}
    def dispatch_typed(self, request):
        self.effects.append(copy.deepcopy(request))
        state = self.records[request["node"]]["services"][request["unit"]]
        if request["operation"] == "UNMASK": state["masked"] = False
        elif request["operation"] == "START": state.update(active=True, substate="running")
        else: raise AssertionError("unknown operation")
        if self.lost_ack: raise OSError("effect lost ACK")
        if self.drift: self.drift(self)
        return {"request_sha256": M.digest(request), "result": "CONFIRMED"}
    def held(self):
        for row in self.plan["participants"]:
            record = self.records[row["node"]]
            record["storage_cfg_sha256"] = self.plan["target_storage_cfg_sha256"]
            record["package_sha256"] = row["after_package_sha256"]
            for unit in M.MASKED_BY_BARRIER:
                record["services"][unit]["masked"] = True
                if unit != "pve-guests.service": record["services"][unit].update(active=False, substate="dead")


class Tests(unittest.TestCase):
    def enrolled(self):
        plan = fixture(); backend = Backend(plan)
        M.Coordinator(plan, backend).baseline(); backend.held()
        return plan, backend

    def test_exact_restore_including_control_only_and_preexisting_inactive_masks(self):
        plan, backend = self.enrolled()
        result = M.Coordinator(plan, backend).restore()
        self.assertEqual(result["state"], "MODEL_EXACT_RESTORED")
        self.assertEqual(result["authority"], "NONE")
        self.assertFalse(any(v for k, v in result.items() if k.endswith("authorized")))
        self.assertFalse(result["mutation_performed"])
        self.assertEqual({r["node"] for r in backend.effects}, {r["node"] for r in plan["participants"]})
        for request in backend.effects:
            self.assertNotEqual(request["unit"], "spiceproxy.service")
            if request["operation"] == "START":
                self.assertNotIn(request["unit"], ("pvescheduler.service", "pve-guests.service"))
        count = len(backend.effects)
        M.Coordinator(plan, backend).restore()
        self.assertEqual(len(backend.effects), count, "completed replay observes only")

    def test_each_effect_lost_ack_never_redispatches(self):
        plan, backend = self.enrolled(); backend.lost_ack = True
        with self.assertRaises(OSError): M.Coordinator(plan, backend).restore()
        self.assertEqual(len(backend.effects), 1)
        backend.lost_ack = False
        with self.assertRaisesRegex(M.Refusal, "uncertain prior attempt"):
            M.Coordinator(plan, backend).restore()
        self.assertEqual(len(backend.effects), 1)

    def test_every_durable_intent_and_done_lost_ack_prefix(self):
        plan, reference = self.enrolled(); M.Coordinator(plan, reference).restore()
        for cut in range(2, reference.writes + 1):
            with self.subTest(cut=cut):
                plan, backend = self.enrolled(); backend.cut = cut
                with self.assertRaises(OSError): M.Coordinator(plan, backend).restore()
                effects = len(backend.effects); backend.cut = None
                unfinished = [k for k in backend.store if k.endswith(":intent") and k[:-7] + ":done" not in backend.store]
                if unfinished:
                    with self.assertRaisesRegex(M.Refusal, "uncertain prior attempt"):
                        M.Coordinator(plan, backend).restore()
                    self.assertEqual(len(backend.effects), effects)
                else:
                    M.Coordinator(plan, backend).restore()
                    ids = [(r["node"], r["unit"], r["operation"]) for r in backend.effects]
                    self.assertEqual(len(ids), len(set(ids)))

    def test_nonzero_unknown_consumer_or_identity_refuses_before_effect(self):
        for field, value in (("consumers", [1]), ("workers", [1]), ("tasks", [1]), ("start_demands", [1]),
                             ("quorate", False), ("boot_id", "changed"), ("package_sha256", "0" * 64),
                             ("storage_cfg_sha256", "0" * 64), ("workload_sha256", "0" * 64)):
            with self.subTest(field=field):
                plan, backend = self.enrolled(); backend.records["pve04"][field] = value
                with self.assertRaises(M.Refusal): M.Coordinator(plan, backend).restore()
                self.assertEqual(backend.effects, [])

    def test_unknown_release_and_control_only_archive_refuse(self):
        plan, backend = self.enrolled(); backend.unknown = True
        with self.assertRaises(M.Refusal): M.Coordinator(plan, backend).restore()
        self.assertEqual(backend.effects, [])
        backend.unknown = False; original = backend.verify_release
        def invalid(*args):
            result = original(*args); result["archive_sha256"]["pve04"] = "a" * 64; return result
        backend.verify_release = invalid
        with self.assertRaises(M.Refusal): M.Coordinator(plan, backend).restore()
        self.assertEqual(backend.effects, [])

    def test_expiry_and_boot_drift_after_effect_stop_further_dispatch(self):
        for drift in (lambda b: setattr(b, "time", 2000),
                      lambda b: b.records["pve04"].update(boot_id="changed")):
            plan, backend = self.enrolled(); backend.drift = drift
            with self.assertRaises(M.Refusal): M.Coordinator(plan, backend).restore()
            self.assertEqual(len(backend.effects), 1)

    def test_baseline_not_adopted_and_active_masked_unsupported_pre_effect(self):
        plan = fixture(); backend = Backend(plan)
        backend.records["pve01"]["services"]["pvedaemon.service"]["masked"] = True
        with self.assertRaises(M.Refusal): M.Coordinator(plan, backend).baseline()
        self.assertEqual(backend.store, {})
        plan, backend = self.enrolled()
        with self.assertRaises(M.Refusal): M.Coordinator(plan, backend).baseline()

    def test_bad_topology_and_control_only_package_change(self):
        for mutate in (lambda p: p["participants"].pop(),
                       lambda p: p["participants"].reverse(),
                       lambda p: p["participants"][-1].update(after_package_sha256="b" * 64)):
            plan = fixture(); mutate(plan)
            with self.assertRaises(M.Refusal): M.Coordinator(plan, Backend(plan))

    def test_preserved_or_inactive_baseline_drift_prevents_any_start(self):
        for unit, values in (("pve-guests.service", {"active": False, "substate": "dead"}),
                             ("pve-sharedlvmthin-thin-guard.service", {"masked": True}),
                             ("spiceproxy.service", {"masked": False}),
                             ("pvescheduler.service", {"active": True, "substate": "running"})):
            with self.subTest(unit=unit):
                plan, backend = self.enrolled()
                backend.records["pve04"]["services"][unit].update(values)
                with self.assertRaises(M.Refusal): M.Coordinator(plan, backend).restore()
                self.assertEqual(backend.effects, [])


if __name__ == "__main__": unittest.main()
