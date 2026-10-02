import copy
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

from test_layout_migration_exact_restore_model import fixture, Backend, M

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("local_restore", ROOT / "experiments/thick-generations/layout-migration-exact-local-restore.py")
E = importlib.util.module_from_spec(SPEC)
with mock.patch.dict(sys.modules, {} if os.name == "posix" else {
        "fcntl": types.ModuleType("fcntl"), "resource": types.ModuleType("resource")}):
    SPEC.loader.exec_module(E)


class Tests(unittest.TestCase):
    def setUp(self):
        self.plan = fixture(); self.backend = Backend(self.plan)
        self.baseline = M.Coordinator(self.plan, self.backend).baseline()
        self.unit = "pvedaemon.service"
        self.state = {"active": False, "masked": True, "substate": "dead", "source": {
            "/usr/lib/systemd/system/" + self.unit: {"inode": 1},
            "/run/systemd/system/" + self.unit: None,
            "/etc/systemd/system/" + self.unit: {"mask_inode": 2},
            "effective:" + self.unit: {"FragmentPath": "/dev/null"}}}
        self.source = {"schema": "slt-local-restore-source/v1", "authority": "NONE",
                       "package": {"package-artifact-sha256": {"sha256": "b" * 64}}, "self_hash": "c" * 64}
        self.request = {"tx": self.plan["tx"], "plan_sha256": M.digest(self.plan), "node": "pve01",
            "boot_id": self.plan["participants"][0]["boot_id"], "unit": self.unit, "operation": "UNMASK",
            "baseline_sha256": M.digest(self.baseline), "release_sha256": "f" * 64}
        self.effects, self.records = [], {}
        self.time, self.writes, self.cut, self.lost_ack = 200, 0, None, False
        self.before_dispatch = None
        self.root1 = mock.patch.object(E.os, "getuid", return_value=0, create=True)
        self.root2 = mock.patch.object(E.os, "geteuid", return_value=0, create=True)
        self.root1.start(); self.root2.start()
        self.addCleanup(self.root1.stop); self.addCleanup(self.root2.stop)

    def after_source(self):
        value = copy.deepcopy(self.state["source"])
        if self.request["operation"] == "UNMASK":
            value["/etc/systemd/system/" + self.unit] = None
            value["effective:" + self.unit] = {"FragmentPath": "/usr/lib/systemd/system/" + self.unit}
        return value

    def grant(self, request, identity, before):
        return {"schema": "slt-local-restore-explicit-grant/v1", "request_sha256": M.digest(request),
            "executor_sha256": M.digest(identity), "before_sha256": M.digest(before),
            "after_source_sha256": M.digest(self.after_source()), "issued_at": 100, "expires_at": 300,
            "allowed_effect": request["operation"], "authorization_sha256": "d" * 64}

    def create(self, tx, key, value):
        if key in self.records: raise E.Refusal("create-only conflict")
        self.records[key] = copy.deepcopy(value); self.writes += 1
        if self.before_dispatch and key.endswith(":intent"): self.before_dispatch()
        if self.writes == self.cut: raise OSError("stored receipt lost ACK")
        return {"durable": True, "sha256": M.digest(value)}

    def command(self, operation, unit):
        self.effects.append((operation, unit))
        self.state["source"] = self.after_source()
        self.state["masked"] = False
        if operation == "START": self.state.update(active=True, substate="running")
        if self.lost_ack: raise OSError("systemctl lost ACK")
        return {"exit_code": 0, "stdout_sha256": "0" * 64, "stderr_sha256": "0" * 64}

    def executor(self, journal=None, **overrides):
        journal = journal or types.SimpleNamespace(read=lambda tx, key: copy.deepcopy(self.records.get(key)), create_once=self.create)
        args = dict(authorize=self.grant, baseline_reader=lambda: copy.deepcopy(self.baseline),
            identity_reader=lambda: copy.deepcopy(self.source), unit_reader=lambda _: copy.deepcopy(self.state),
            command=self.command, clock=lambda: self.time, node_reader=lambda: "pve01",
            boot_reader=lambda: self.request["boot_id"], process_reader=lambda: {"pid": 123, "starttime": 456})
        args.update(overrides)
        return E.Executor(self.plan, journal, **args)

    @unittest.skipUnless(os.name == "posix", "native fsync/flock executor composition")
    def test_real_private_local_journal_and_fresh_process_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "restore"
            with E.LocalJournal.for_test(self.plan, root, create=True) as journal:
                result = self.executor(journal=journal).execute(self.request)
            with E.LocalJournal.for_test(self.plan, root) as fresh:
                self.assertEqual(self.executor(journal=fresh).execute(self.request), result)
            self.assertEqual(len(self.effects), 1)

    def test_one_effect_completed_replay_is_read_only_even_after_expiry(self):
        executor = self.executor(); result = executor.execute(self.request)
        self.assertEqual(result["state"], "LOCAL_EFFECT_CONFIRMED")
        self.assertEqual(result["authority"], "NONE")
        self.assertFalse(result["effect_retry_allowed"])
        with self.assertRaises(E.Refusal): executor.execute(self.request)
        self.time = 2000
        self.assertEqual(self.executor().execute(self.request), result)
        self.assertEqual(len(self.effects), 1)

    def test_positive_start_and_control_only_use_same_typed_contract(self):
        self.request["operation"] = "START"; self.state["masked"] = False
        result = self.executor().execute(self.request)
        self.assertEqual(result["state"], "LOCAL_EFFECT_CONFIRMED")
        self.assertEqual(self.effects, [("START", self.unit)])
        self.records.clear(); self.effects.clear(); self.state.update(active=False, substate="dead")
        self.request.update(node="pve04", boot_id=self.plan["participants"][-1]["boot_id"])
        self.source["package"]["package-artifact-sha256"]["sha256"] = "a" * 64
        result = self.executor(node_reader=lambda: "pve04").execute(self.request)
        self.assertEqual(result["state"], "LOCAL_EFFECT_CONFIRMED")
        self.assertEqual(self.effects, [("START", self.unit)])

    def test_process_reuse_foreign_receipt_and_changed_completed_source_refuse(self):
        process = {"pid": 123, "starttime": 1}
        self.before_dispatch = lambda: process.update(starttime=2)
        with self.assertRaises(E.Refusal): self.executor(process_reader=lambda: copy.deepcopy(process)).execute(self.request)
        self.assertEqual(self.effects, [])
        self.before_dispatch = None; self.records.clear()
        self.executor().execute(self.request)
        key = next(k for k in self.records if k.endswith(":done"))
        self.records[key]["unexpected"] = True
        with self.assertRaises(E.Refusal): self.executor().execute(self.request)
        self.assertEqual(len(self.effects), 1)
        del self.records[key]["unexpected"]
        self.source["self_hash"] = "new"
        with self.assertRaises(E.Refusal): self.executor().execute(self.request)
        self.assertEqual(len(self.effects), 1)

    def test_each_intent_done_lost_ack_and_effect_uncertainty_never_redispatch(self):
        for cut in (1, 2):
            with self.subTest(cut=cut):
                self.records.clear(); self.effects.clear(); self.writes = 0; self.cut = cut
                self.state.update(active=False, masked=True)
                with self.assertRaises(OSError): self.executor().execute(self.request)
                count = len(self.effects); self.cut = None
                result = self.executor().execute(self.request)
                self.assertEqual(result["state"], "UNKNOWN_RETAIN" if cut == 1 else "LOCAL_EFFECT_CONFIRMED")
                self.assertEqual(len(self.effects), count)
        self.records.clear(); self.effects.clear(); self.writes = 0; self.state["masked"] = True; self.lost_ack = True
        with self.assertRaises(OSError): self.executor().execute(self.request)
        self.assertEqual(self.executor().execute(self.request)["state"], "UNKNOWN_RETAIN")
        self.assertEqual(len(self.effects), 1)

    def test_absent_authorizer_bad_request_and_package_identity_have_zero_effects(self):
        with self.assertRaises(E.Refusal): self.executor(authorize=None).execute(self.request)
        for field, value in (("operation", "STOP"), ("unit", "evil.service"), ("node", "pve04"),
                             ("plan_sha256", "0" * 64), ("baseline_sha256", "0" * 64)):
            request = dict(self.request); request[field] = value
            with self.subTest(field=field), self.assertRaises(E.Refusal): self.executor().execute(request)
        self.source["package"]["package-artifact-sha256"]["sha256"] = "a" * 64
        with self.assertRaises(E.Refusal): self.executor().execute(self.request)
        self.assertEqual(self.records, {}); self.assertEqual(self.effects, [])

    def test_pre_dispatch_source_namespace_deadline_and_release_drift(self):
        for change in (lambda: self.source.update(self_hash="new"), lambda: setattr(self, "time", 400),
                       lambda: self.state["source"].update(foreign=True),
                       lambda: self.baseline.update(authority="foreign")):
            with self.subTest(change=change):
                old = copy.deepcopy((self.source, self.state, self.baseline))
                self.records.clear(); self.before_dispatch = change; self.time = 200
                with self.assertRaises(E.Refusal): self.executor().execute(self.request)
                self.assertEqual(self.effects, [])
                self.source, self.state, self.baseline = old

    def test_expiry_during_authorizer_recheck_prevents_command(self):
        calls = [0]
        def grant(*args):
            calls[0] += 1
            value = self.grant(*args)
            if calls[0] == 2: self.time = 301
            return value
        with self.assertRaisesRegex(E.Refusal, "immediately before"):
            self.executor(authorize=grant).execute(self.request)
        self.assertEqual(self.effects, [])

    def test_zero_exit_without_target_or_wrong_source_leaves_unknown(self):
        success = {"exit_code": 0, "stdout_sha256": "0" * 64, "stderr_sha256": "0" * 64}
        with self.assertRaises(E.Refusal): self.executor(command=lambda *_: success).execute(self.request)
        self.assertEqual(self.executor().execute(self.request)["state"], "UNKNOWN_RETAIN")
        self.records.clear()
        def changed(*args):
            result = self.command(*args); self.state["source"]["foreign"] = True; return result
        with self.assertRaisesRegex(E.Refusal, "source target"):
            self.executor(command=changed).execute(self.request)
        self.assertEqual(len(self.records), 1)

    def test_start_never_applies_to_previously_inactive_or_pve_guests(self):
        self.request["operation"] = "START"; self.state["masked"] = False
        self.baseline["records"]["pve01"]["services"][self.unit].update(active=False, substate="dead")
        self.request["baseline_sha256"] = M.digest(self.baseline)
        with self.assertRaisesRegex(E.Refusal, "baseline forbids"):
            self.executor().execute(self.request)
        self.request["unit"] = "pve-guests.service"
        with self.assertRaises(E.Refusal): self.executor().execute(self.request)
        self.assertEqual(self.effects, [])

    def test_unmask_cannot_expose_an_undrained_daemon(self):
        self.state.update(active=True, substate="running")
        with self.assertRaisesRegex(E.Refusal, "drained/preserved"):
            self.executor().execute(self.request)
        self.assertEqual(self.effects, []); self.assertEqual(self.records, {})

    def test_fixed_argv_no_stop_mask_storage_or_shell_and_nonzero_unknown(self):
        result = types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"Removed mask\n")
        with mock.patch.object(E.subprocess, "run", return_value=result) as run:
            E.run_closed("UNMASK", self.unit); E.run_closed("START", self.unit)
            self.assertEqual(run.call_args_list[0].args[0], ["/usr/bin/systemctl", "unmask", "--", self.unit])
            self.assertEqual(run.call_args_list[1].args[0], ["/usr/bin/systemctl", "--job-mode=fail", "start", "--", self.unit])
            for operation in ("STOP", "MASK", "RESTART", "/bin/sh"):
                with self.assertRaises(E.Refusal): E.run_closed(operation, self.unit)
            self.assertEqual(run.call_count, 2)
            self.assertNotIn("shell", run.call_args.kwargs)
        result.returncode = 1
        with mock.patch.object(E.subprocess, "run", return_value=result), self.assertRaises(E.Refusal):
            E.run_closed("START", self.unit)
        with mock.patch.object(E.subprocess, "run", side_effect=E.subprocess.TimeoutExpired("systemctl", 30)):
            with self.assertRaises(E.subprocess.TimeoutExpired): E.run_closed("START", self.unit)


if __name__ == "__main__": unittest.main()
