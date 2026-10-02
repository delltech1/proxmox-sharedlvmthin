"""Pure grant/phase tests; no external process or host operation."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_layout_migration_exact_integration_model as F


class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): F.Tests.setUpClass()

    @classmethod
    def tearDownClass(cls): F.Tests.tearDownClass()

    def setUp(self):
        import subprocess
        patcher = mock.patch.object(subprocess, "Popen", side_effect=AssertionError("no real process"))
        patcher.start(); self.addCleanup(patcher.stop)
        self.e = copy.deepcopy(F.Tests.fixture.e)
        self.m = F.I.IntegrationModel.for_simulation(self.e, F.pins(self.e["plan"]), F.pins(self.e["plan"], True),
            F.Tests.sources, F.Tests.hashes, now=F.Tests.fixture.now)
        self.addCleanup(self.m.close)
        hit = self.m.trace.hit
        def stop(name, payload=None):
            if name == "journal-before:coordinator:transport:PVE01:qmeventd.service:UNMASK:intent": raise F.I.Cut("test checkpoint")
            hit(name, payload)
        with mock.patch.object(self.m.trace, "hit", side_effect=stop):
            with self.assertRaises(F.I.Cut): self.m.run()
        self.request = self.m.read(self.m.plan["tx"], "PVE01:qmeventd.service:UNMASK:intent")
        self.identity = self.m.source_identity("PVE01")
        self.before = self.m.unit("PVE01", "qmeventd.service")
        self.raw = self.m.V.canonical(self.m.current_evidence())
        self.now = self.m.time

    def provider(self, raw=None):
        return self.m.G.GrantProvider.for_model(self.m.code, self.m.plan, self.raw if raw is None else raw,
            self.m.journals["coordinator"], {n: self.m.journals[n] for n in self.m.T.NODES})

    def authorize(self, provider=None, now=None):
        return (provider or self.provider()).authorize(self.request, self.identity, self.before, now=self.now if now is None else now)

    def stored(self):
        return self.m.journals["PVE01"].read(self.m.plan["tx"], "PVE01:qmeventd.service:UNMASK:grant")

    def assert_refusal(self, call):
        with self.assertRaises(Exception) as caught: call()
        self.assertEqual(type(caught.exception).__name__, "Refusal", str(caught.exception))

    def test_exact_grant_is_durable_before_return_and_bracket_never_renews(self):
        provider = self.provider(); grant = self.authorize(provider)
        stored = self.stored()
        self.assertEqual(stored["grant"], grant["issued_grant"])
        self.assertEqual(stored["server_grant"], grant["server_grant"])
        self.assertEqual(stored, grant["issuance"])
        self.assertEqual(stored["phase_index"], 0)
        self.assertEqual(stored["phase_prefix_sha256"], F.I.digest([]))
        self.assertEqual(stored["authority"], "NONE")
        self.assertEqual(self.authorize(provider, self.now + 1), grant)
        self.assertEqual(self.authorize(self.provider(), self.now + 2), grant)
        self.assertEqual(self.stored(), stored)
        self.assertEqual(self.m.effects, [])
        self.assertEqual(sum(x.startswith("journal-stored:PVE01:PVE01:qmeventd.service:UNMASK:grant") for x in self.m.trace.trace), 1)

    def test_lost_ack_reopens_only_exact_stored_grant_and_expiry_is_inspection_only(self):
        provider = self.provider()
        self.m.trace.cut = len(self.m.trace.trace) + 1
        with self.assertRaises(F.I.Cut): self.authorize(provider)
        stored = self.stored(); self.assertIsNotNone(stored)
        self.assert_refusal(lambda: self.authorize(provider))
        self.m.trace.cut = None
        fresh = self.provider()
        self.assertEqual(self.authorize(fresh)["issued_grant"], stored["grant"])
        self.assert_refusal(lambda: self.authorize(self.provider(), stored["grant"]["expires_at"] + 1))
        self.assertEqual(self.provider().inspect_issued(self.request)["state"], "EXACT_ISSUANCE_OBSERVED")
        self.assertFalse(self.provider().inspect_issued(self.request)["effect_retry_allowed"])
        self.assertEqual(self.stored(), stored)

    def test_caller_authority_labels_and_partial_server_proofs_cannot_issue(self):
        original = self.m.V.strict_json(self.raw)
        for change in (lambda e: e.update(authority="GRANTED"), lambda e: e["archives"].pop("PVE03"),
                       lambda e: e["certified_inputs"]["records"][0]["payload"].update(dpkg_verify_clean=False)):
            e = copy.deepcopy(original); change(e)
            self.assert_refusal(lambda: self.authorize(self.provider(self.m.V.canonical(e))))
            self.assertIsNone(self.stored())

    def test_held_label_without_complete_barrier_and_archive_journal_is_not_authority(self):
        records = self.m.journals["coordinator"].journal.records
        for missing in ("barrier:11:OBSERVED_COMPLETE", "archive:PVE03:done", "flow:release", "baseline"):
            value = records.pop(missing)
            self.assert_refusal(lambda: self.authorize())
            records[missing] = value
            self.assertIsNone(self.stored())

    def test_future_phase_attempt_and_swapped_pending_intent_refuse(self):
        records = self.m.journals["coordinator"].journal.records
        future = dict(self.request, operation="START")
        records["PVE01:qmeventd.service:START:intent"] = future
        self.assert_refusal(lambda: self.authorize()); records.pop("PVE01:qmeventd.service:START:intent")
        pending = records["PVE01:qmeventd.service:UNMASK:intent"]
        records["PVE01:qmeventd.service:UNMASK:intent"] = dict(pending, node="PVE02")
        self.assert_refusal(lambda: self.authorize())
        self.assertIsNone(self.stored())

    def test_preexisting_mask_or_inactive_forbidden_step_cannot_hide_outside_phase_list(self):
        records = self.m.journals["coordinator"].journal.records
        for name in ("PVE04:spiceproxy.service:UNMASK:intent", "PVE03:pvescheduler.service:START:intent",
                     "PVE02:pve-guests.service:START:intent"):
            records[name] = {"unexpected": "old writer"}
            self.assert_refusal(lambda: self.authorize())
            records.pop(name)
            self.assertIsNone(self.stored())

    def test_same_phase_start_cannot_overtake_unmask_without_receipt(self):
        self.request["operation"] = "START"
        self.m.create_once(self.m.plan["tx"], "PVE01:qmeventd.service:START:intent", self.request)
        self.m.records["PVE01"]["services"]["qmeventd.service"]["masked"] = False
        self.m.namespaces["PVE01"]["/etc/systemd/system/qmeventd.service"] = None
        self.m.namespaces["PVE01"]["effective:qmeventd.service"] = copy.deepcopy(self.e["baseline_sources"]["PVE01"]["effective:qmeventd.service"])
        self.before = self.m.unit("PVE01", "qmeventd.service")
        self.raw = self.m.V.canonical(self.m.current_evidence())
        self.assert_refusal(lambda: self.authorize())

    def test_new_cohort_source_or_request_never_replaces_lost_ack_grant(self):
        self.authorize(); original = self.stored()
        evidence = self.m.V.strict_json(self.raw)
        evidence["cohort_id"] = "f" * 32
        for row in evidence["current"].values(): row["cohort_id"] = evidence["cohort_id"]
        self.assert_refusal(lambda: self.authorize(self.provider(self.m.V.canonical(evidence))))
        self.identity["extra-source"] = "0" * 64
        self.assert_refusal(lambda: self.authorize())
        self.assertEqual(self.stored(), original)

    def test_grant_tamper_self_hash_rewrite_and_extra_fields_refuse(self):
        self.authorize(); original = self.stored()
        records = self.m.journals["PVE01"].journal.records
        name = "PVE01:qmeventd.service:UNMASK:grant"
        for change in (lambda v: v["grant"].update(expires_at=v["grant"]["expires_at"] + 1),
                       lambda v: v.update(phase_index=True), lambda v: v.update(authority="GRANTED"),
                       lambda v: v.update(retry_allowed=True)):
            value = copy.deepcopy(original); change(value)
            value.pop("record_sha256"); value["record_sha256"] = F.I.digest(value)
            records[name] = value
            self.assert_refusal(lambda: self.authorize())
        records[name] = original

    def test_grant_is_before_local_intent_and_full_predecessor_allows_next_step(self):
        # Continue only the explicitly simulated first local effect. The actual
        # executor writes its intent and completion; no systemctl is called.
        self.m.poisoned = False; self.m.pending = copy.deepcopy(self.request)
        receipt = self.m.local_effect(self.request)
        name = "PVE01:qmeventd.service:UNMASK"
        self.m.create_once(self.m.plan["tx"], "receipt:" + name, receipt)
        self.m.create_once(self.m.plan["tx"], name + ":done", {"request_sha256": F.I.digest(self.request), "result": "CONFIRMED"})
        self.m.pending = None
        self.request = dict(self.request, operation="START")
        self.m.create_once(self.m.plan["tx"], "PVE01:qmeventd.service:START:intent", self.request)
        self.before = self.m.unit("PVE01", "qmeventd.service")
        self.raw = self.m.V.canonical(self.m.current_evidence())
        result = self.authorize()
        self.assertEqual(result["issued_grant"]["allowed_effect"], "START")
        stored = self.m.journals["PVE01"].read(self.m.plan["tx"], "PVE01:qmeventd.service:START:grant")
        self.assertEqual(stored["phase_index"], 1)
        self.assertEqual(len(self.m.effects), 1, "grant issuance itself must never run START")

    def test_default_constructor_and_forged_nonfresh_provider_refuse(self):
        with self.assertRaises(self.m.G.Refusal): self.m.G.GrantProvider()
        original = self.m.G.__frozen_source_sha256__
        self.m.G.__frozen_source_sha256__ = "0" * 64
        with self.assertRaises(self.m.G.Refusal): self.provider()
        self.m.G.__frozen_source_sha256__ = original

    def test_concurrent_exact_creator_is_not_blindly_adopted_in_same_attempt(self):
        journal = self.m.journals["PVE01"]; create = journal.create_once
        def competitor(tx, name, value):
            create(tx, name, value)
            return create(tx, name, value)
        provider = self.provider()
        with mock.patch.object(journal, "create_once", side_effect=competitor):
            self.assert_refusal(lambda: self.authorize(provider))
        stored = self.stored()
        self.assertIsNotNone(stored)
        self.assertEqual(self.authorize(self.provider())["issued_grant"], stored["grant"])
        self.assertEqual(self.m.effects, [])

    def test_phase_change_after_grant_write_refuses_without_effect(self):
        journal = self.m.journals["PVE01"]; create = journal.create_once
        def changed(tx, name, value):
            ack = create(tx, name, value)
            self.m.journals["coordinator"].journal.records["PVE01:qmeventd.service:UNMASK:done"] = {
                "request_sha256": F.I.digest(self.request), "result": "CONFIRMED"}
            return ack
        with mock.patch.object(journal, "create_once", side_effect=changed):
            self.assert_refusal(lambda: self.authorize())
        self.assertIsNotNone(self.stored())
        self.assertEqual(self.m.effects, [])
        self.assert_refusal(lambda: self.authorize(self.provider()))

    @unittest.skipUnless(os.name == "posix", "durable grant filesystem test needs POSIX")
    def test_native_create_only_grant_lost_ack_exact_readback_in_private_journals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            journal_class = self.m.J.Journal
            ports = {}
            try:
                for name, existing in self.m.journals.items():
                    actual = journal_class.for_test(self.m.plan, root / name, create=True)
                    ports[name] = F.I.JournalPort(actual, self.m.trace, name)
                    for record_key, value in existing.journal.records.items():
                        actual.create_once(self.m.plan["tx"], record_key, value)
                self.m.journals = ports
                self.m.trace.cut = len(self.m.trace.trace) + 1
                with self.assertRaises(F.I.Cut): self.authorize()
                stored = self.stored()
                self.m.trace.cut = None
                self.assertEqual(self.authorize(self.provider())["issued_grant"], stored["grant"])
                with self.assertRaises(FileExistsError):
                    ports["PVE01"].journal.create_once(self.m.plan["tx"], "PVE01:qmeventd.service:UNMASK:grant", stored)
                self.assertEqual(self.m.effects, [])
            finally:
                for port in ports.values(): port.journal.close()


if __name__ == "__main__": unittest.main()
