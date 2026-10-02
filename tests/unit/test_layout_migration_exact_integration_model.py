"""Full source-only chain. No real SSH, systemctl, SAN or package operations."""
import base64
import copy
import hashlib
import importlib.util
import inspect
import os
from pathlib import Path
import struct
import sys
import tempfile
import textwrap
import types
import unittest
from unittest import mock

import test_layout_migration_exact_release_validator as F

ROOT = Path(__file__).resolve().parents[2] / "experiments/thick-generations"
SPEC = importlib.util.spec_from_file_location("exact_integration", ROOT / "layout-migration-exact-integration-model.py")
I = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(I)


def uppercase_fixtures():
    """Clone fixture factories only: production transport requires exact PVE01.

    Re-running every builder regenerates all hashes/certificates correctly; we
    never rename an already-issued node/receipt or rewrite production source.
    """
    def clone(module):
        result = types.SimpleNamespace(**vars(module))
        if hasattr(module, "H"): result.H = clone(module.H)
        for clsname in ("Tests", "AllCertifiedV2Tests"):
            if not hasattr(module, clsname): continue
            cls = getattr(module, clsname)
            methods = {}
            scope = dict(vars(module))
            if hasattr(result, "H"): scope["H"] = result.H
            for name, value in vars(cls).items():
                if inspect.isfunction(value):
                    body = textwrap.dedent(inspect.getsource(value)).replace("pve0", "PVE0")
                    exec(compile(body, str(Path(module.__file__)), "exec"), scope)
                    methods[name] = scope[name]
            setattr(result, clsname, type(clsname, (unittest.TestCase,), methods))
        return result
    fixture_globals = dict(vars(F)); fixture_globals["fixtures"] = clone(F.fixtures)
    body = textwrap.dedent(inspect.getsource(F.Tests.setUp)).replace("pve0", "PVE0")
    exec(compile(body, str(Path(F.__file__)), "exec"), fixture_globals)
    fixture_class = type("UppercaseReleaseFixture", (unittest.TestCase,), {"setUp": fixture_globals["setUp"]})
    case = fixture_class(); case.setUp()
    return case


def pins(plan, after=False):
    rows = []
    for index, participant in enumerate(plan["participants"], 1):
        raw = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + bytes([index]) * 32
        key = "ssh-ed25519 " + base64.b64encode(raw).decode()
        rows.append({"node": participant["node"], "host": f"192.0.2.{index}", "boot_id": participant["boot_id"],
            "ssh_host_key": key, "ssh_host_fingerprint": "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("="),
            "helper_sha256": "f" * 64, "source_package": {"name": "pve-sharedlvmthin", "version": "1",
                "artifact_sha256": participant["after_package_sha256" if after else "before_package_sha256"],
                "runtime_build_id": "c" * 64, "sources_sha256": SOURCE_COHORT_SHA256}})
    return {"schema": "slt-exact-transport-pins/v1", "authority": "NONE", "plan_sha256": I.digest(plan),
            "helper_path": "/usr/libexec/pve-sharedlvmthin/sharedlvmthin-exact-restore-transport", "participants": rows}


class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global SOURCE_COHORT_SHA256, I
        cls.fixture = uppercase_fixtures()
        cls.sources = {n: (ROOT / n).read_bytes() for n in I.FILES}
        cls.hashes = {n: hashlib.sha256(v).hexdigest() for n, v in cls.sources.items()}
        I = I.Sources(cls.sources, cls.hashes).load("layout-migration-exact-integration-model.py")
        SOURCE_COHORT_SHA256 = I.digest(cls.hashes)
        # The certificate's CONTROL_ONLY guard is positively inactive. Unlike
        # the generic restore fixture, bind the exact same current lifecycle.
        e = cls.fixture.e; guard = "pve-sharedlvmthin-thin-guard.service"
        for record in (e["baseline"]["records"]["PVE04"], e["baseline_captures"]["PVE04"]["observation"],
                       e["current"]["PVE04"]["observation"]):
            record["services"][guard].update(active=False, substate="dead")
        row = next(r for r in e["current"]["PVE04"]["runtime"]["services"] if r["unit"] == guard)
        row.update(active_state="inactive", sub_state="dead", main_pid=0, cgroup_empty=True)

    @classmethod
    def tearDownClass(cls): cls.fixture.doCleanups()

    def setUp(self):
        # A bug that reaches ANY external executable fails this source-only test.
        import subprocess
        p = mock.patch.object(subprocess, "Popen", side_effect=AssertionError("real subprocess forbidden"))
        p.start(); self.addCleanup(p.stop)
        self.e = copy.deepcopy(self.fixture.e)

    def model(self, **kwargs):
        m = I.IntegrationModel.for_simulation(self.e, pins(self.e["plan"]), pins(self.e["plan"], True),
            self.sources, self.hashes, now=self.fixture.now, **kwargs)
        self.addCleanup(m.close)
        return m

    def golden(self):
        if not hasattr(type(self), "golden_model"):
            model = self.model()
            type(self).golden_result = model.run()
            type(self).golden_model = model
        return type(self).golden_model

    def test_full_four_node_chain_actual_validator_transport_executor_and_final_state(self):
        model = self.golden(); out = type(self).golden_result
        self.assertEqual(out["state"], "INTEGRATION_MODEL_EXACT_RESTORED")
        self.assertEqual(out["authority"], "NONE")
        self.assertFalse(out["server_qualified"])
        self.assertEqual(len(model.effects), len(set(model.effects)))
        self.assertEqual(out["receipt_count"], len(model.effects))
        for node in model.T.NODES:
            self.assertEqual(model.records[node]["services"], self.e["baseline"]["records"][node]["services"])
        self.assertEqual(model.records["PVE04"]["package_sha256"], self.e["baseline"]["records"]["PVE04"]["package_sha256"])
        self.assertFalse(any(unit == "spiceproxy.service" or (op == "START" and unit in ("pvescheduler.service", "pve-guests.service")) for _, unit, op in model.effects))
        self.assertFalse(any(out[k] for k in ("runtime_authorized", "rollout_authorized", "release_authorized", "mutation_performed")))
        count = len(model.effects)
        with self.assertRaises(I.Refusal): model.run()
        self.assertEqual(len(model.effects), count)
        self.assertFalse(model.inspect()["effect_retry_allowed"])

    def test_production_constructor_absent_and_source_closure_tamper_refused(self):
        with self.assertRaises(I.Refusal): I.IntegrationModel()
        changed = dict(self.sources); changed[I.FILES[-1]] += b"\n# swapped\n"
        with self.assertRaises(I.Refusal):
            I.IntegrationModel.for_simulation(self.e, pins(self.e["plan"]), pins(self.e["plan"], True), changed, self.hashes, now=self.fixture.now)

    def test_every_successful_trace_prefix_has_no_resume_or_effect_authority(self):
        model = self.golden(); checker = I.PrefixInspector(model.plan)
        prefixes = set(); counts = {}
        for item in model.trace.audit:
            result = checker.accept(item)
            prefix = item["event"].partition(":")[0]
            prefixes.add(prefix); counts[prefix] = counts.get(prefix, 0) + 1
            self.assertFalse(result["effect_retry_allowed"])
            self.assertEqual(result["authority"], "NONE")
            self.assertEqual(result["state"], "INSPECTION_ONLY")
            self.assertFalse(hasattr(checker, "run"))
        self.assertGreater(counts["transport-before"], 56)
        self.assertEqual(counts["effect-before"], len(model.effects))
        self.assertEqual(counts["effect-applied"], len(model.effects))
        self.assertEqual(checker.inspect()["unknown_effects"], [])
        self.assertTrue({"journal-before", "journal-stored", "journal-ack", "transport-result", "transport-ack",
                         "effect-before", "effect-applied", "effect-ack", "archive-model-applied"} <= prefixes)

    def test_trace_oracle_rejects_completion_before_effect_or_missing_intent(self):
        model = self.golden()
        target = next(i for i, item in enumerate(model.trace.audit) if item["event"].startswith("effect-before:"))
        checker = I.PrefixInspector(model.plan)
        with self.assertRaises(I.Refusal): checker.accept(model.trace.audit[target])
        for item in model.trace.audit[:target]: checker.accept(item)
        # The first applied effect is followed by its command ACK; skipping the
        # pre-effect check must not make a receipt appear authoritative.
        applied = model.trace.audit[target + 1]
        self.assertTrue(applied["event"].startswith("effect-applied:"))
        with self.assertRaises(I.Refusal): checker.accept(applied)

    def test_actual_lost_ack_before_after_first_local_effect_and_journal(self):
        golden = self.golden()
        names = ["journal-before:coordinator:flow:started", "journal-stored:coordinator:flow:started",
                 "transport-before:PVE01:READ_OBSERVATION", "transport-result:PVE01:READ_OBSERVATION",
                 "effect-before:PVE01:qmeventd.service:UNMASK", "effect-applied:PVE01:qmeventd.service:UNMASK",
                 "journal-stored:PVE01:PVE01:qmeventd.service:UNMASK:done",
                 "transport-result:PVE01:RESTORE_EFFECT:qmeventd.service:UNMASK",
                 "journal-stored:coordinator:receipt:PVE01:qmeventd.service:UNMASK"]
        for name in names:
            with self.subTest(prefix=name):
                cut = golden.trace.trace.index(name)
                model = self.model(cut=cut)
                with self.assertRaises(I.Cut): model.run()
                count = len(model.effects)
                self.assertLessEqual(count, 1)
                self.assertFalse(model.inspect()["effect_retry_allowed"])
                with self.assertRaises(I.Refusal): model.run()
                self.assertEqual(len(model.effects), count)
                checker = I.PrefixInspector(model.plan)
                for item in model.trace.audit: checker.accept(item)

    def test_partial_cohort_and_control_only_package_drift_refuse_before_model_effects(self):
        original = copy.deepcopy(self.e)
        for field in ("current", "baseline_captures", "archives"):
            self.e = copy.deepcopy(original); self.e[field].pop(next(iter(self.e[field])))
            with self.assertRaises(I.Refusal): self.model()
        self.e = original
        bad = pins(self.e["plan"], True); bad["participants"][3]["source_package"]["runtime_build_id"] = "e" * 64
        with self.assertRaises(I.Refusal):
            I.IntegrationModel.for_simulation(self.e, pins(self.e["plan"]), bad, self.sources, self.hashes, now=self.fixture.now)

    def test_reboot_expiry_config_workload_package_source_and_consumers_before_dispatch(self):
        for kind in ("boot_id", "storage_cfg_sha256", "workload_sha256", "package_sha256", "source", "expiry", "consumers", "quorum"):
            with self.subTest(kind=kind):
                model = self.model(); hit = model.trace.hit; injected = []
                def changed(name, payload=None):
                    if name == "transport-before:PVE01:RESTORE_EFFECT:qmeventd.service:UNMASK" and not injected:
                        injected.append(True)
                        if kind == "source": model.namespaces["PVE02"]["/usr/lib/systemd/system/pvedaemon.service"]["sha256"] = "0" * 64
                        elif kind == "expiry": model.time = model.plan["expires_at"] + 1
                        elif kind == "consumers": model.records["PVE02"]["consumers"] = [999]
                        elif kind == "quorum": model.records["PVE02"]["quorate"] = False
                        else: model.records["PVE02"][kind] = "0" * 64
                    hit(name, payload)
                with mock.patch.object(model.trace, "hit", side_effect=changed):
                    with self.assertRaises(Exception) as caught: model.run()
                self.assertEqual(type(caught.exception).__name__, "Refusal")
                self.assertTrue(injected)
                self.assertEqual(model.effects, [])
                self.assertFalse(model.inspect()["effect_retry_allowed"])

    def test_certified_control_guard_cannot_disagree_with_current_or_baseline(self):
        guard = "pve-sharedlvmthin-thin-guard.service"
        for record in (self.e["baseline"]["records"]["PVE04"], self.e["baseline_captures"]["PVE04"]["observation"],
                       self.e["current"]["PVE04"]["observation"]):
            record["services"][guard].update(active=True, substate="running")
        row = next(r for r in self.e["current"]["PVE04"]["runtime"]["services"] if r["unit"] == guard)
        row.update(active_state="active", sub_state="running", main_pid=101, cgroup_empty=False)
        model = self.model()
        with self.assertRaisesRegex(I.Refusal, "current/certified Thin guard lifecycle"):
            model.run()
        self.assertEqual(model.effects, [])

    def test_transport_source_pins_bind_actual_loaded_closure(self):
        before, after = pins(self.e["plan"]), pins(self.e["plan"], True)
        for cohort in (before, after):
            for row in cohort["participants"]: row["source_package"]["sources_sha256"] = "0" * 64
        with self.assertRaisesRegex(I.Refusal, "actual immutable closure"):
            I.IntegrationModel.for_simulation(self.e, before, after, self.sources, self.hashes, now=self.fixture.now)

    @unittest.skipUnless(os.name == "posix", "real local journal requires POSIX")
    def test_full_chain_real_private_posix_journals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            model = self.model(journal_root=root)
            result = model.run()
            self.assertEqual(result["receipt_count"], len(model.effects))
            for key, receipt in model.receipts.items():
                node = key.split(":")[0]
                self.assertEqual(model.journals[node].read(model.plan["tx"], key + ":done"), receipt)
            model.close()


if __name__ == "__main__": unittest.main()
