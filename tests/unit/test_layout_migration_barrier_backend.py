import copy
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("barrier_backend", ROOT / "experiments/thick-generations/layout-migration-barrier-backend.py")
B = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(B)


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(B.JOURNAL.BASE, "PARENT", self.tmp.name)
        self.patch.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.patch.stop)
        self.names = ["pve01", "pve02", "pve03", "pve04"]
        self.cfg = "sharedlvmthin: lab\n\tvgname vg\n"
        self.guest_cfg = "scsi0: lab:vm-100-disk-0\n"
        self.workload = {"schema": "slt-workload-prepare/v1", "observed_at": 1000,
                         "managed_storages": ["lab"], "running_consumers": [],
                         "storage_cfg_sha256": B.digest(self.cfg.encode()),
                         "resources_sha256": "f" * 64,
                         "consumers": [{"vmid": 100, "node": "pve01", "type": "qemu", "name": "lab", "status": "stopped",
                                        "disks": [{"key": "scsi0", "storage": "lab", "volume": "vm-100-disk-0"}]}],
                         "config_sha256": {"100": B.digest(self.guest_cfg.encode())},
                         "verdict": "SNAPSHOT_READY", "authorization": "NONE", "mutation_performed": False,
                         "limitations": ["snapshot only"]}
        self.workload["evidence_sha256"] = B.digest(B.WORKLOAD.canonical(self.workload))
        self.plan = {"schema": "slt-maintenance-barrier-model/v1", "tx": "a" * 32,
                     "participants": [{"node": n, "boot_id": f"0000000{i}-0000-4000-8000-00000000000{i}", "san_role": i < 4} for i, n in enumerate(self.names, 1)],
                     "storage_cfg_sha256": B.digest(self.cfg.encode()),
                     "workload_snapshot_sha256": self.workload["evidence_sha256"],
                     "candidate_sha256": "c" * 64, "issued_at": 900, "deadline": 1100,
                     "actions": [{"step": s, "node": n} for s in B.MODEL.STEPS for n in self.names]}
        self.states = {n: self.raw(n) for n in self.names}
        self.commands = []
        self.agents = {n: B.NodeAgent(self.plan, n, lambda n=n: copy.deepcopy(self.states[n]),
                                     lambda argv, n=n: self.execute(n, argv), lambda: 1000) for n in self.names}

    def raw(self, node):
        boot = next(p["boot_id"] for p in self.plan["participants"] if p["node"] == node)
        obs = {"node": node, "boot_id": boot, "membership": self.names,
               "storage_cfg_sha256": self.plan["storage_cfg_sha256"],
               "workload_snapshot_sha256": self.plan["workload_snapshot_sha256"],
               "quorate": True, "ha_state": "EMPTY_IDLE", "tasks": [], "workers": [], "pending_jobs": []}
        guests = [{"vmid": 100, "type": "qemu", "node": node, "status": "stopped", "config": self.guest_cfg}] if node == "pve01" else []
        if node == "pve02":
            guests = [{"vmid": 103, "type": "qemu", "node": node, "status": "running", "config": "scsi0: local-lvm:vm-103-disk-0\n"}]
        services = []
        for unit in B.WATCHED:
            oneshot = unit == "pve-guests.service"
            inactive = unit == "pve-sharedlvmthin-thin-guard.service" or (unit == "multipathd.service" and node == "pve04")
            services.append({"unit": unit, "active_state": "inactive" if inactive else "active",
                             "sub_state": "dead" if inactive else ("exited" if oneshot else "running"),
                             "main_pid": 0 if inactive or oneshot else 999,
                             "control_pid": 0, "cgroup_empty": inactive or oneshot,
                             "job": None, "mask_target": None})
        return {"observation": obs, "storage_config": self.cfg, "workload": copy.deepcopy(self.workload),
                "guests": guests, "inventory_complete": True, "managed_mappers": [], "managed_open_lvs": [], "services": services}

    def execute(self, node, argv):
        self.commands.append((node, argv))
        self.assertEqual(argv[0], "/usr/bin/systemctl")
        self.assertEqual(argv[2], "--")
        self.assertIn(argv[1], ("mask", "stop"))
        for unit in argv[3:]:
            row = next(s for s in self.states[node]["services"] if s["unit"] == unit)
            if argv[1] == "mask":
                row["mask_target"] = "/etc/systemd/system:/dev/null"
            else:
                self.assertNotIn(unit, B.MODEL.FORBIDDEN_STOPS)
                row.update(active_state="inactive", sub_state="dead", main_pid=0, control_pid=0, cgroup_empty=True)
        if all(r["active_state"] == "inactive" for r in self.states[node]["services"] if r["unit"] in ("pve-ha-lrm.service", "pve-ha-crm.service")):
            self.states[node]["observation"]["ha_state"] = "DRAINED"
        return True

    def backend(self):
        coord = {k: self.plan["participants"][0][k] for k in ("node", "boot_id")}
        journal = B.JOURNAL.BarrierFileJournal("1" * 32, self.plan, coord)
        self.addCleanup(journal.close)
        agents = self.agents
        class Transport:
            def request(self, node, request):
                return agents[node].request(request)
        return B.Backend(self.plan, journal, Transport(), lambda: 1000)

    def test_four_node_real_journal_order_and_unrelated_guest_survives(self):
        backend = self.backend()
        result = B.MODEL.Controller(self.plan, backend, 1000).run()
        self.assertEqual(result["state"], "MODEL_HELD")
        self.assertFalse(result["rollout_authorized"])
        self.assertEqual(len(self.commands), 16)
        self.assertEqual([n for n, _ in self.commands[:8]], [n for n in self.names for _ in range(2)])
        self.assertEqual(self.states["pve02"]["guests"][0]["status"], "running")
        history = B.JOURNAL.inspect(backend.journal.artifact_path(), self.plan)
        self.assertEqual(history["state"], "MODEL_COMPLETED_HISTORICAL")
        self.assertEqual(len(history["events"]), 36)

    def test_effect_then_error_cannot_retry_or_replay(self):
        backend = self.backend()
        original = self.agents["pve01"].execute
        def failure(argv):
            original(argv)
            raise TimeoutError("SSH result unknown after service effect")
        self.agents["pve01"].execute = failure
        with self.assertRaises(TimeoutError): B.MODEL.Controller(self.plan, backend, 1000).run()
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(B.JOURNAL.inspect(backend.journal.artifact_path(), self.plan)["state"], "RECOVERY_REQUIRED")
        with self.assertRaises(B.Refusal): B.MODEL.Controller(self.plan, backend, 1000).run()
        self.assertEqual(len(self.commands), 1)

    def test_preflight_ambiguities_have_no_effect(self):
        for mutation in (
                lambda r: r.update(inventory_complete=False),
                lambda r: r.update(managed_mappers=["hidden-tpool"]),
                lambda r: r.update(managed_open_lvs=["head"]),
                lambda r: r["guests"].clear(),
                lambda r: r["guests"][0].update(status="running"),
                lambda r: r["guests"][0].update(config="scsi0: local:foreign\n"),
                lambda r: r["observation"].update(running_guests=[]),
                lambda r: r["observation"].update(ha_state="UNKNOWN"),
                lambda r: r["observation"].update(tasks=["task"]),
                lambda r: r["observation"].update(workers=["orphan"]),
                lambda r: r["services"].pop(),
                lambda r: r["services"][0].update(job="pending"),
                lambda r: r["services"][0].update(mask_target="/run/systemd/system:/dev/null")):
            raw = copy.deepcopy(self.states["pve01"])
            mutation(raw)
            with self.assertRaises((B.Refusal, B.WORKLOAD.Refusal)):
                B.scoped_observation(raw, self.plan, "pve01")
        self.assertEqual(self.commands, [])

    def test_new_managed_guest_is_not_misclassified_unrelated(self):
        raw = copy.deepcopy(self.states["pve02"])
        raw["guests"][0]["config"] = "scsi0: lab:vm-103-disk-0\n"
        with self.assertRaisesRegex(B.Refusal, "differs"):
            B.scoped_observation(raw, self.plan, "pve02")

    def test_pve_guests_active_executor_refuses(self):
        raw = copy.deepcopy(self.states["pve01"])
        row = next(s for s in raw["services"] if s["unit"] == "pve-guests.service")
        row.update(main_pid=500, cgroup_empty=False, sub_state="running")
        with self.assertRaisesRegex(B.Refusal, "active executor"):
            B.scoped_observation(raw, self.plan, "pve01")

    def test_inactive_service_with_orphan_refuses(self):
        raw = copy.deepcopy(self.states["pve01"])
        raw["services"][0].update(active_state="inactive", sub_state="dead", main_pid=0, cgroup_empty=False)
        with self.assertRaisesRegex(B.Refusal, "surviving executor"):
            B.scoped_observation(raw, self.plan, "pve01")

    def test_direct_dispatch_without_durable_intent_refuses(self):
        backend = self.backend()
        with self.assertRaisesRegex(B.Refusal, "durable matching"):
            backend.mask_persistent("pve01", B.MODEL.ENTRY_UNITS)
        self.assertEqual(self.commands, [])

    def test_generic_or_foreign_request_refuses(self):
        for operation in ("RELEASE", "START", "REBOOT", "rm -rf /"):
            agent = B.NodeAgent(self.plan, "pve01", lambda: self.states["pve01"], lambda argv: self.fail("must not execute"), lambda: 1000)
            with self.assertRaises(B.Refusal):
                agent.request({"tx": self.plan["tx"], "node": "pve01", "plan_sha256": B.MODEL.frozen_hash(self.plan), "operation": operation})

    def test_drain_before_entry_refuses(self):
        with self.assertRaisesRegex(B.Refusal, "entry barrier"):
            self.agents["pve01"].request({"tx": self.plan["tx"], "node": "pve01", "plan_sha256": B.MODEL.frozen_hash(self.plan), "operation": "SERVICE_DRAIN"})
        self.assertEqual(self.commands, [])

    def test_workload_digest_and_config_change_refuse(self):
        for field in ("workload", "storage_config"):
            raw = copy.deepcopy(self.states["pve01"])
            if field == "workload": raw[field]["consumers"][0]["status"] = "running"
            else: raw[field] += "\n"
            with self.assertRaises(B.Refusal): B.scoped_observation(raw, self.plan, "pve01")

    def test_deadline_between_mask_and_stop_never_dispatches_stop(self):
        backend = self.backend()
        agent = self.agents["pve01"]
        original = agent.execute
        def late(argv):
            original(argv)
            agent.clock = lambda: 1101
            return True
        agent.execute = late
        with self.assertRaisesRegex(B.Refusal, "deadline"):
            B.MODEL.Controller(self.plan, backend, 1000).run()
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.commands[0][1][1], "mask")

    def test_foreign_transport_response_is_never_acknowledged(self):
        backend = self.backend()
        original = backend.transport.request
        def foreign(node, request):
            response = original(node, request)
            response["request"]["node"] = "pve99"
            return response
        backend.transport.request = foreign
        with self.assertRaisesRegex(B.Refusal, "another request"):
            B.MODEL.Controller(self.plan, backend, 1000).run()
        self.assertEqual(self.commands, [])

    def test_success_without_service_effect_refuses(self):
        backend = self.backend()
        self.agents["pve01"].execute = lambda argv: True
        with self.assertRaisesRegex(B.Refusal, "postcondition"):
            B.MODEL.Controller(self.plan, backend, 1000).run()
        self.assertEqual(B.JOURNAL.inspect(backend.journal.artifact_path(), self.plan)["state"], "RECOVERY_REQUIRED")

    def test_caught_reentry_poison_prevents_next_command(self):
        backend = self.backend()
        agent = self.agents["pve01"]
        original = agent.execute
        request = {"tx": self.plan["tx"], "node": "pve01", "plan_sha256": B.MODEL.frozen_hash(self.plan), "operation": "OBSERVE"}
        def reenter(argv):
            original(argv)
            try: agent.request(request)
            except B.Refusal: pass
            return True
        agent.execute = reenter
        with self.assertRaises(B.Refusal): B.MODEL.Controller(self.plan, backend, 1000).run()
        self.assertEqual(len(self.commands), 1)


if __name__ == "__main__": unittest.main()
