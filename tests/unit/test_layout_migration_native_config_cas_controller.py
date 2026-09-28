import copy
import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("native_cas_controller",
    ROOT / "experiments/thick-generations/layout-migration-native-config-cas-controller.py")
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def request():
    nodes = [{"node": "pve0" + str(i), "boot_id": str(i) * 8 + "-1111-4111-8111-" + str(i) * 12,
              "role": "CONTROL_ONLY" if i == 4 else "SAN_PARTICIPANT"} for i in range(1, 5)]
    modules = ["PVE/Storage.pm", "PVE/Storage/Plugin.pm", "PVE/SectionConfig.pm", "PVE/Cluster.pm",
               "PVE/Tools.pm", "PVE/JSONSchema.pm", "PVE/Storage/Custom/SharedLvmThinPlugin.pm"]
    result = {"schema": "slt-native-config-cas-controller/v1", "tx": "a" * 32,
              "generation": "1", "attempt_id": "b" * 32,
              "executor": {key: nodes[0][key] for key in ("node", "boot_id")},
              "context_sha256": "c" * 64,
              "candidate": {"package": "pve-sharedlvmthin", "version": "1.0~tg35", "flavor": "dual",
                            "deb_sha256": "d" * 64, "artifact_sha256": "e" * 64},
              "config": {"baseline_hex": b"baseline\n".hex(), "target_hex": b"target\n".hex(),
                         "baseline_native_digest": "f" * 40},
              "changes": [{"storage_id": "thin", "property": "slt-vg-layout", "old_value": None, "new_value": "mixed"}],
              "participants": nodes,
              "evidence": {"barrier_sha256": "1" * 64, "prepare_manifest_sha256": "2" * 64,
                           "preinst_sha256": {n["node"]: "3" * 64 for n in nodes[:3]},
                           "payload_sha256": {n["node"]: "4" * 64 for n in nodes}},
              "serializer": {"helper_sha256": "5" * 64, "perl_sha256": "6" * 64,
                             "perl_hash_seed": "0", "perl_perturb_keys": "0",
                             "modules": [{"name": n, "path": "/usr/share/perl5/" + n, "sha256": "7" * 64}
                                         for n in sorted(modules)],
                             "registered_plugins": ["PVE::Storage::Custom::SharedLvmThinPlugin"]}}
    result["authorization"] = {"action": "CONTROLLER_MODEL_ONE_CAS_ONLY",
        "request_body_sha256": M.request_body_sha256(result), "issued_wall_ns": "1000", "expires_wall_ns": "2000"}
    return result


class Fake:
    def __init__(self, req):
        self.req = copy.deepcopy(req)
        self.calls, self.counts, self.records, self.hooks, self.overrides = [], {}, {}, {}, {}
        self.before, self.after = {}, {}
        self.wall, self.mono = 1100, 5000
        self.raw = req["config"]["baseline_hex"]
        self.lock_mode, self.swallow = "once", False
        self.write_effect, self.write_raise = True, False
        self.saved_config = self.saved_callback = None
        self.in_lock = False
        self.cfg = {"ids": {"thin": {"type": "sharedlvmthin", "slt-vgname": "vg-a"},
                            "unrelated": {"type": "dir", "path": "/var/lib/vz"}},
                    "order": {"thin": 1, "unrelated": 2}}

    def backend(self):
        return M.Backend(**{name: getattr(self, name) for name in M.BOUNDARIES})

    def step(self, name, action, *args):
        self.calls.append(name)
        count = self.counts[name] = self.counts.get(name, 0) + 1
        if self.before.get(name) == count:
            raise RuntimeError("before effect")
        if (name, "before") in self.hooks:
            self.hooks[name, "before"](*args)
        result = action()
        if (name, "after") in self.hooks:
            self.hooks[name, "after"](*args)
        if self.after.get(name) == count:
            raise RuntimeError("after effect")
        return self.overrides.get(name, result)

    def observe_local_identity(self):
        return self.step("observe_local_identity", lambda: {**self.req["executor"], "quorate": True})

    def sample_clock(self):
        return self.step("sample_clock", lambda: {"wall_ns": self.wall, "monotonic_ns": self.mono})

    def verify_admission(self, req):
        return self.step("verify_admission", lambda: {"request_sha256": M.digest(req), "admitted": True}, req)

    def verify_serializer(self, manifest):
        return self.step("verify_serializer", lambda: {"manifest_sha256": M.digest(manifest), "verified": True}, manifest)

    def with_storage_config_lock(self, callback):
        self.saved_callback = callback
        def effect():
            self.in_lock = True
            try:
                for _ in range({"none": 0, "once": 1, "twice": 2}[self.lock_mode]):
                    if self.swallow:
                        try:
                            callback()
                        except Exception:
                            pass
                    else:
                        callback()
            finally:
                self.in_lock = False
            return {"released": True}
        return self.step("with_storage_config_lock", effect)

    def read_native_config(self):
        return self.step("read_native_config", lambda: {"digest": self.req["config"]["baseline_native_digest"],
                                                       "config": copy.deepcopy(self.cfg)})

    def read_raw_config(self):
        return self.step("read_raw_config", lambda: {"raw_hex": self.raw})

    def render_native_config(self, config):
        self.saved_config = config
        return self.step("render_native_config", lambda: {"raw_hex": self.req["config"]["target_hex"], "warnings": []}, config)

    def write_native_config_once(self, config):
        def effect():
            if not self.in_lock:
                raise AssertionError("write outside lock")
            if self.write_effect:
                self.raw = self.req["config"]["target_hex"]
            if self.write_raise:
                raise RuntimeError("simulated write error")
            return {"status": "RETURNED"}
        return self.step("write_native_config_once", effect, config)

    def persist_record_once(self, kind, record):
        def effect():
            if kind in self.records:
                raise RuntimeError("existing immutable reservation")
            self.records[kind] = copy.deepcopy(record)
            return {"record_sha256": M.digest(record), "created": True,
                    "file_synced": True, "directory_synced": True}
        return self.step("persist_record_once", effect, kind, record)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.req = request()
        self.fake = Fake(self.req)

    def controller(self):
        return M.Controller(self.req, self.fake.backend())

    def assert_no_authority(self, result):
        self.assertEqual(result["authorization"], "NONE")
        self.assertTrue(result["model_only"])
        for key in ("retry_authorized", "hold_transition_authorized", "release_authorized",
                    "runtime_qualified", "settlement_proven"):
            self.assertIs(result[key], False)

    def test_success_order_and_only_inline_observation(self):
        result = self.controller().run()
        self.assertEqual(result["classification"], "INLINE_OBSERVATION_ONLY")
        self.assertEqual(result["inline_observation"], "TARGET")
        self.assertTrue(result["intent_durable"] and result["outcome_durable"])
        self.assertEqual(self.fake.counts["write_native_config_once"], 1)
        calls = self.fake.calls
        self.assertLess(calls.index("with_storage_config_lock"), calls.index("read_native_config"))
        self.assertLess(calls.index("render_native_config"), calls.index("persist_record_once"))
        self.assertLess(calls.index("persist_record_once"), calls.index("write_native_config_once"))
        self.assertEqual(calls[calls.index("write_native_config_once") + 1], "read_raw_config")
        self.assertEqual(set(self.fake.records), {"INTENT", "OUTCOME"})
        self.assert_no_authority(result)

    def test_fault_before_and_after_every_observed_boundary(self):
        self.controller().run()
        counts = self.fake.counts.copy()
        for when in ("before", "after"):
            for name, count in counts.items():
                for nth in range(1, count + 1):
                    with self.subTest(when=when, name=name, nth=nth):
                        fake = Fake(self.req)
                        getattr(fake, when)[name] = nth
                        controller = M.Controller(self.req, fake.backend())
                        result = controller.run()
                        self.assertLessEqual(fake.counts.get("write_native_config_once", 0), 1)
                        self.assert_no_authority(result)
                        # Write exceptions are retained and can yield a durable,
                        # explicitly non-settlement readback. Other faults poison.
                        if name != "write_native_config_once":
                            self.assertEqual(result["classification"], "UNKNOWN_RETAIN")
                        with self.assertRaises(M.Refusal):
                            controller.run()

    def test_write_outcome_matrix(self):
        for effect in (False, True):
            for raised in (False, True):
                with self.subTest(effect=effect, raised=raised):
                    fake = Fake(self.req); fake.write_effect = effect; fake.write_raise = raised
                    result = M.Controller(self.req, fake.backend()).run()
                    self.assertTrue(result["write_entered"])
                    self.assertEqual(result["write_outcome"], "RAISED" if raised else "RETURNED")
                    self.assertEqual(result["inline_observation"], "TARGET" if effect else "BASELINE")
                    self.assertEqual(result["classification"],
                        "UNKNOWN_RETAIN" if not effect and not raised else "INLINE_OBSERVATION_ONLY")
                    self.assert_no_authority(result)

    def test_foreign_before_dispatch_and_after_effect(self):
        self.fake.raw = b"foreign".hex()
        result = self.controller().run()
        self.assertFalse(result["write_entered"])
        self.assertNotIn("INTENT", self.fake.records)
        self.fake = Fake(self.req)
        self.fake.hooks["write_native_config_once", "after"] = lambda cfg: setattr(self.fake, "raw", b"foreign".hex())
        result = self.controller().run()
        self.assertEqual(result["classification"], "UNKNOWN_RETAIN")
        self.assertEqual(result["inline_observation"], "FOREIGN")

    def test_stale_native_digest_and_warning_or_target_drift(self):
        variants = (("read_native_config", {"digest": "0" * 40, "config": self.fake.cfg}),
                    ("render_native_config", {"raw_hex": self.req["config"]["target_hex"], "warnings": ["dropped property"]}),
                    ("render_native_config", {"raw_hex": b"other".hex(), "warnings": []}))
        for name, value in variants:
            with self.subTest(name=name):
                fake = Fake(self.req); fake.overrides[name] = value
                result = M.Controller(self.req, fake.backend()).run()
                self.assertFalse(result["write_entered"])
                self.assertFalse(result["reservation_entered"])

    def test_baseline_changes_during_native_read_or_intent(self):
        for boundary in ("read_native_config", "persist_record_once"):
            fake = Fake(self.req)
            fake.hooks[boundary, "after"] = lambda *args: setattr(fake, "raw", b"foreign".hex())
            result = M.Controller(self.req, fake.backend()).run()
            self.assertFalse(result["write_entered"])
            self.assertEqual(result["classification"], "UNKNOWN_RETAIN")

    def test_missing_double_and_swallowed_lock_callbacks(self):
        for mode in ("none", "twice"):
            self.fake = Fake(self.req); self.fake.lock_mode = mode; self.fake.swallow = True
            result = self.controller().run()
            self.assertEqual(result["classification"], "UNKNOWN_RETAIN")
            self.assertLessEqual(self.fake.counts.get("write_native_config_once", 0), 1)
            self.assertNotIn("OUTCOME", self.fake.records)
        self.fake = Fake(self.req); self.fake.swallow = True
        self.fake.before["read_native_config"] = 1
        result = self.controller().run()
        self.assertEqual(result["classification"], "UNKNOWN_RETAIN")
        self.assertFalse(result["write_entered"])
        self.assertNotIn("OUTCOME", self.fake.records)

    def test_stale_lock_callback_cannot_dispatch_again(self):
        result = self.controller().run()
        self.assertTrue(result["write_entered"])
        with self.assertRaises(M.Refusal):
            self.fake.saved_callback()
        self.assertEqual(self.fake.counts["write_native_config_once"], 1)

    def test_swallowed_reentry_before_and_after_write_poison(self):
        for boundary in ("verify_admission", "persist_record_once", "write_native_config_once"):
            with self.subTest(boundary=boundary):
                fake = Fake(self.req)
                controller = M.Controller(self.req, fake.backend())
                def reenter(*args):
                    try:
                        controller.run()
                    except M.Refusal:
                        pass
                fake.hooks[boundary, "after"] = reenter
                result = controller.run()
                self.assertEqual(result["classification"], "UNKNOWN_RETAIN")
                self.assertLessEqual(fake.counts.get("write_native_config_once", 0), 1)
                self.assertEqual(result["write_entered"], boundary == "write_native_config_once")

    def test_prepared_config_late_drift_is_blocked(self):
        def mutate(kind, record):
            self.fake.saved_config["ids"]["thin"]["slt-vgname"] = "other-vg"
        self.fake.hooks["persist_record_once", "after"] = mutate
        result = self.controller().run()
        self.assertEqual(result["classification"], "UNKNOWN_RETAIN")
        self.assertFalse(result["write_entered"])
        self.assertTrue(result["reservation_entered"])

    def test_post_write_config_drift_preserves_entered_latch(self):
        self.fake.hooks["write_native_config_once", "after"] = lambda cfg: cfg["ids"].clear()
        result = self.controller().run()
        self.assertEqual(result["classification"], "UNKNOWN_RETAIN")
        self.assertTrue(result["write_entered"])
        self.assertEqual(self.fake.raw, self.req["config"]["target_hex"])

    def test_malformed_write_ack_cannot_be_success(self):
        for value in (None, True, {"status": "FAILED"}, {"status": "RETURNED", "extra": 1}):
            fake = Fake(self.req); fake.overrides["write_native_config_once"] = value
            result = M.Controller(self.req, fake.backend()).run()
            self.assertEqual(result["classification"], "UNKNOWN_RETAIN")
            self.assertTrue(result["write_entered"])

    def test_expiry_wall_rollback_and_monotonic_expiry(self):
        for which in ("expired", "rollback", "mono-expired"):
            fake = Fake(self.req)
            def move(kind, record):
                if which == "expired": fake.wall = 2001
                elif which == "rollback": fake.wall = 1099
                else: fake.mono = 5901
            fake.hooks["persist_record_once", "after"] = move
            result = M.Controller(self.req, fake.backend()).run()
            self.assertEqual(result["classification"], "UNKNOWN_RETAIN")
            self.assertFalse(result["write_entered"])
        self.fake.hooks["write_native_config_once", "after"] = lambda cfg: setattr(self.fake, "mono", 5901)
        result = self.controller().run()
        self.assertEqual(result["classification"], "UNKNOWN_RETAIN")
        self.assertTrue(result["write_entered"])

    def test_journal_bad_ack_after_effect_and_duplicate_reservation(self):
        self.fake.overrides["persist_record_once"] = {"record_sha256": "0" * 64, "created": True,
                                                      "file_synced": True, "directory_synced": True}
        result = self.controller().run()
        self.assertEqual(result["classification"], "UNKNOWN_RETAIN")
        self.assertIn("INTENT", self.fake.records)
        self.assertFalse(result["write_entered"])
        del self.fake.overrides["persist_record_once"]
        result = self.controller().run()
        self.assertEqual(result["classification"], "UNKNOWN_RETAIN")
        self.assertFalse(result["write_entered"])

    def test_returned_request_and_result_do_not_share_mutable_state(self):
        controller = self.controller()
        self.req["candidate"]["version"] = "foreign"
        result = controller.run()
        self.assertEqual(result["classification"], "INLINE_OBSERVATION_ONLY")
        result["release_authorized"] = True
        self.assertFalse(controller.snapshot()["release_authorized"])
        with self.assertRaises(M.Refusal): controller._state = "NEW"
        with self.assertRaises(M.Refusal): controller.run()

    def test_callback_argument_mutation_refuses(self):
        self.fake.hooks["verify_admission", "after"] = lambda req: req.update(attempt_id="9" * 32)
        result = self.controller().run()
        self.assertEqual(result["classification"], "UNKNOWN_RETAIN")
        self.assertFalse(result["write_entered"])

    def test_request_schema_types_binding_and_required_manifest(self):
        variants = []
        for value in (True, 1, 1.0, "01", "-1", "1e2"):
            req = copy.deepcopy(self.req); req["generation"] = value; variants.append(req)
        req = copy.deepcopy(self.req); req["unknown"] = True; variants.append(req)
        req = copy.deepcopy(self.req); req["authorization"]["action"] = "ONE_STORAGE_CONFIG_CAS"; variants.append(req)
        req = copy.deepcopy(self.req); req["candidate"]["version"] = "drift"; variants.append(req)
        req = copy.deepcopy(self.req); req["serializer"]["modules"] = []; variants.append(req)
        req = copy.deepcopy(self.req); req["evidence"]["preinst_sha256"]["pve04"] = "0" * 64; variants.append(req)
        req = copy.deepcopy(self.req); req["changes"][0]["property"] = "slt-vgname"; variants.append(req)
        for req in variants:
            with self.subTest(req=req), self.assertRaises(M.Refusal):
                M.Controller(req, self.fake.backend())
        self.assertEqual(self.fake.calls, [])

    def test_custom_values_never_dispatch_custom_callbacks(self):
        calls = []
        class EvilStr(str):
            def __eq__(self, other): calls.append("eq"); return True
            __hash__ = str.__hash__
        class EvilDict(dict):
            def items(self): calls.append("items"); return super().items()
        class EvilInt(int):
            def __le__(self, other): calls.append("le"); return True
        for value in (EvilDict(self.req), {**self.req, "tx": EvilStr("a" * 32)},
                      {**self.req, EvilStr("extra"): None}):
            with self.assertRaises(M.Refusal): M.Controller(value, self.fake.backend())
        self.fake.overrides["sample_clock"] = {"wall_ns": EvilInt(1100), "monotonic_ns": 5000}
        result = self.controller().run()
        self.assertEqual(result["classification"], "UNKNOWN_RETAIN")
        self.assertEqual(calls, [])

    def test_real_module_inventory_bound_and_request_byte_bound(self):
        for count in (247, 1024, 1025):
            req = request()
            modules = req["serializer"]["modules"]
            modules.extend({"name": f"Extra/Module{i:04d}.pm", "path": f"/usr/share/perl5/Extra/Module{i:04d}.pm",
                            "sha256": "8" * 64} for i in range(count - len(modules)))
            modules.sort(key=lambda item: item["name"])
            with self.subTest(count=count):
                if count <= 1024:
                    req["authorization"]["request_body_sha256"] = M.request_body_sha256(req)
                    M.validate_request(req)
                else:
                    with self.assertRaises(M.Refusal):
                        M.validate_request(req)
        oversized = request()
        oversized["config"]["baseline_hex"] = "61" * M.MAX_BYTES
        oversized["config"]["target_hex"] = "62" * M.MAX_BYTES
        with self.assertRaisesRegex(M.Refusal, "4 MiB"):
            M.canonical(oversized)


if __name__ == "__main__":
    unittest.main()
