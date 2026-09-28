import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


M = load("all_certified_v2", ROOT / "experiments/thick-generations/layout-migration-all-certified-v2.py")
H = load("cert_v2_fixtures", ROOT / "tests/unit/test_layout_migration_release_certificate_v2.py")


def manifest_bytes(context, node):
    return M.canonical({"schema": "slt-package-maintenance/v1", "phase": "CONFIG_COMMITTED",
                        "tx": context["tx"], "generation": context["generation"], "node": node})


class AllCertifiedV2Tests(unittest.TestCase):
    def fixture(self):
        baseline, target, topology, context, configured, refresh, ready, release, ready_records, now = H.Tests().fixture()
        for record in ready_records[:3]:
            record["hold"]["manifest_sha256"] = hashlib.sha256(manifest_bytes(context, record["node"])).hexdigest()
        ready = M.CERT.READY.evaluate(context, topology, baseline, target, configured, refresh, ready_records, now)
        release["all_ready_plan_sha256"] = M.CERT.digest(ready)
        cert, raw = M.CERT.evaluate(context, topology, baseline, target, configured, refresh, ready,
                                    release, ready_records, now)
        raw_sha = hashlib.sha256(raw).hexdigest()
        names = [node["name"] for node in context["nodes"]]
        boots = {node["name"]: node["boot_id"] for node in context["nodes"]}
        collection = {"schema": "slt-layout-certificate-collection/v2", "collection_id": "f" * 32,
                      "tx": context["tx"], "generation": context["generation"],
                      "context_sha256": context["context_sha256"], "commit_id": cert["commit_id"],
                      "certificate_bytes_sha256": raw_sha, "candidate": copy.deepcopy(context["candidate"]),
                      "participant_boots": boots, "node_roles": copy.deepcopy(context["node_roles"]),
                      "collector_sha256": "7" * 64, "issued_at": now, "expires_at": now + 100,
                      "challenges": {node: str(i + 1) * 32 for i, node in enumerate(names)}, "allowed_effects": []}
        records = []
        for i, node in enumerate(names):
            role = context["node_roles"][node]
            san = role == "SAN_PARTICIPANT"
            candidate = context["candidate"]
            current = ready_records[i]
            guard = copy.deepcopy(current["thinguard"])
            if context["thinguard_required_by_node"][node]:
                for offset, sample in enumerate(guard["samples"], 1):
                    sample["observed_at"] = now + offset
            record = {"schema": "slt-layout-certificate-ack/v2" if san else "slt-layout-certificate-current-observation/v2",
                      "action": "VERIFY_DURABLE_CERTIFICATE_AND_HOLD" if san else "VERIFY_CURRENT_ONLY",
                      "collection_id": collection["collection_id"], "collection_plan_sha256": M.digest(collection),
                      "challenge": collection["challenges"][node], "collector_sha256": collection["collector_sha256"],
                      "node": node, "role": role, "boot_id_start": boots[node], "boot_id_end": boots[node],
                      "tx": context["tx"], "generation": context["generation"], "context_sha256": context["context_sha256"],
                      "commit_id": cert["commit_id"], "certificate_bytes_sha256": raw_sha, "certificate_size": len(raw),
                      "verification_started_at": now, "verification_finished_at": now + 3,
                      "control_plane": {"cluster_name": context["cluster_name"], "cluster_nodes": names, "quorate": True,
                                        "target_storage_cfg_sha256": context["target_storage_cfg_sha256"]},
                      "installed": {**{key: candidate[key] for key in ("package", "version", "flavor", "artifact_sha256")},
                                    "dpkg_state": "installed"},
                      "payload": {"dpkg_verify_complete": True, "dpkg_verify_clean": True, "package_file_list_sha256": "8" * 64},
                      "workers": copy.deepcopy(current["workers"]), "thinguard": guard,
                      "vg_identities": copy.deepcopy(current["vg_identities"])}
            if san:
                record["durability"] = {"result": "CERTIFICATE_DURABLE_LOCAL", "file_fsync": True,
                                        "directory_fsync": True, "parent_fsync": True, "exact_reread": True}
                ns = {"directory": M.CERTIFICATE_DIRECTORY, "decision_name": f"{context['tx']}-{context['generation']}.json",
                      "components": [{"path": path, "identity": {"type": "directory", "dev": 100 + i,
                                      "ino": 1000 + j, "uid": 0, "mode": 0o755 if j < 3 else 0o700, "nlink": 2}}
                                     for j, path in enumerate(M.COMPONENT_PATHS)],
                      "certificate": {"type": "regular", "dev": 100 + i, "ino": 2000 + i, "uid": 0,
                                      "mode": 0o600, "nlink": 1, "size": len(raw), "sha256": raw_sha}}
                record["namespace_before"] = ns
                record["namespace_after"] = copy.deepcopy(ns)
                record["hold"] = {"path": M.ACTIVE_MANIFEST, "before": copy.deepcopy(current["hold"]),
                                  "after": copy.deepcopy(current["hold"]), "exact_reread": True}
                hold_file = {"type": "regular", "dev": current["hold"]["device"], "ino": current["hold"]["inode"],
                             "uid": 0, "mode": 0o600, "nlink": 1, "size": len(manifest_bytes(context, node)),
                             "sha256": current["hold"]["manifest_sha256"]}
                record["hold"]["identity_before"] = hold_file
                record["hold"]["identity_after"] = copy.deepcopy(hold_file)
            else:
                record["local_effects"] = {key: False for key in ("certificate_written", "hold_created", "hold_changed",
                                                                   "hold_released", "package_changed", "services_changed")}
                record["local_certificate_present"] = False
                record["hold"] = copy.deepcopy(current["hold"])
            records.append(record)
        return [context, topology, baseline, target, configured, refresh, ready, release,
                ready_records, raw, collection, records, now + 3]

    def test_exact_role_barrier_is_non_authorizing_and_has_only_three_targets(self):
        args = self.fixture()
        out = M.evaluate(*args)
        self.assertEqual(out["phase"], "ALL_CERTIFIED")
        self.assertEqual(out["authorization"], "NONE")
        self.assertFalse(out["hold_released"])
        self.assertFalse(out["mutation_performed"])
        self.assertEqual(out["release_nodes"], ["pve01", "pve02", "pve03"])
        self.assertEqual(out["verify_only_nodes"], ["pve04"])
        self.assertEqual(sorted(out["hold_by_node"]), out["release_nodes"])
        self.assertEqual(out["certificate_sha256"], hashlib.sha256(args[9]).hexdigest())
        self.assertEqual(set(out), {"schema", "phase", "verdict", "authorization", "hold_released", "mutation_performed",
            "tx", "generation", "context_sha256", "commit_id", "certificate_sha256", "candidate", "nodes", "node_roles",
            "participant_boots", "release_nodes", "verify_only_nodes", "node_ack_sha256", "latest_verification_finished_at",
            "release_not_after", "hold_by_node", "plan_sha256"})
        for node, target in out["hold_by_node"].items():
            original = next(record["hold"] for record in args[8] if record["node"] == node)
            self.assertEqual(target["manifest_sha256"], original["manifest_sha256"])
            self.assertEqual(target["source_identity"]["dev"], original["device"])
            self.assertEqual(target["source_identity"]["ino"], original["inode"])
            self.assertEqual(set(target), {"manifest_sha256", "source_identity", "certificate_identity"})
            for key in ("source_identity", "certificate_identity"):
                self.assertEqual(set(target[key]), {"dev", "ino", "uid", "mode", "nlink", "size", "sha256"})

    def test_exact_certificate_bytes_not_embedded_digest_or_reformatted_json(self):
        args = self.fixture()
        parsed = json.loads(args[9])
        self.assertNotEqual(parsed["certificate_sha256"], hashlib.sha256(args[9]).hexdigest())
        for bad in (args[9][:-1], json.dumps(parsed, indent=2).encode() + b"\n", args[9] + b"\n"):
            changed = copy.deepcopy(args); changed[9] = bad
            with self.assertRaises(M.Refusal): M.evaluate(*changed)
        args[10]["certificate_bytes_sha256"] = parsed["certificate_sha256"]
        with self.assertRaises(M.Refusal): M.evaluate(*args)

    def test_certificate_is_recomputed_not_just_self_hashed(self):
        args = self.fixture()
        args[6]["node_roles"]["pve04"] = "SAN_PARTICIPANT"
        body = dict(args[6]); body.pop("plan_sha256")
        args[6]["plan_sha256"] = M.digest(body)
        args[7]["all_ready_plan_sha256"] = M.digest(args[6])
        with self.assertRaises(M.Refusal): M.evaluate(*args)

    def test_ready_evidence_drift_after_certificate_refuses(self):
        args = self.fixture()
        args[8][0]["hold"]["inode"] += 1
        with self.assertRaises(M.Refusal): M.evaluate(*args)

    def test_missing_duplicate_and_foreign_participants_refuse(self):
        for change in (lambda rows: rows.pop(), lambda rows: rows.__setitem__(2, copy.deepcopy(rows[0])),
                       lambda rows: rows[3].update(node="pve99")):
            args = self.fixture(); change(args[11])
            with self.assertRaises(M.Refusal): M.evaluate(*args)

    def test_control_cannot_supply_san_ack_or_certificate_durability(self):
        mutations = (lambda row: row.update(schema="slt-layout-certificate-ack/v2"),
                     lambda row: row.update(action="VERIFY_DURABLE_CERTIFICATE_AND_HOLD"),
                     lambda row: row.update(durability={"file_fsync": True}),
                     lambda row: row.update(local_certificate_present=True),
                     lambda row: row.update(role="SAN_PARTICIPANT"))
        for mutate in mutations:
            args = self.fixture(); mutate(args[11][3])
            with self.assertRaises(M.Refusal): M.evaluate(*args)

    def test_every_control_effect_must_be_exactly_false(self):
        for key in self.fixture()[11][3]["local_effects"]:
            for value in (True, 0, None):
                args = self.fixture(); args[11][3]["local_effects"][key] = value
                with self.subTest(key=key, value=value), self.assertRaises(M.Refusal): M.evaluate(*args)
        args = self.fixture(); args[11][3]["hold"]["device"] = False
        with self.assertRaises(M.Refusal): M.evaluate(*args)

    def test_san_requires_all_durability_flags_and_exact_namespace(self):
        mutations = [lambda r, k=key: r["durability"].update({k: False}) for key in
                     ("file_fsync", "directory_fsync", "parent_fsync", "exact_reread")]
        mutations += [lambda r: r["namespace_after"]["certificate"].update(nlink=2),
                      lambda r: r["namespace_after"]["certificate"].update(mode=0o644),
                      lambda r: r["namespace_after"]["certificate"].update(ino=9999),
                      lambda r: r["namespace_after"]["components"][2]["identity"].update(mode=0o777),
                      lambda r: r["namespace_after"].update(decision_name="foreign.json"),
                      lambda r: r["namespace_after"]["components"].pop(),
                      lambda r: r["namespace_after"]["certificate"].update(sha256="0" * 64)]
        for mutate in mutations:
            args = self.fixture(); mutate(args[11][0])
            with self.assertRaises(M.Refusal): M.evaluate(*args)

    def test_replay_durability_still_requires_fsyncs(self):
        args = self.fixture()
        for row in args[11][:3]: row["durability"]["result"] = "VERIFIED_REPLAY"
        self.assertEqual(M.evaluate(*args)["authorization"], "NONE")
        args[11][0]["durability"]["file_fsync"] = False
        with self.assertRaises(M.Refusal): M.evaluate(*args)

    def test_hold_loss_same_bytes_new_inode_and_swapped_holds_refuse(self):
        mutations = (lambda r: r["hold"]["after"].update(active=False),
                     lambda r: r["hold"]["after"].update(inode=999),
                     lambda r: r["hold"]["before"].update(device=999),
                     lambda r: r["hold"]["after"].update(manifest_sha256="0" * 64),
                     lambda r: r["hold"]["identity_after"].update(mode=0o644),
                     lambda r: r["hold"]["identity_after"].update(nlink=2),
                     lambda r: r["hold"]["identity_after"].update(size=999),
                     lambda r: r["hold"]["identity_after"].update(ino=999),
                     lambda r: r["hold"].update(exact_reread=False))
        for mutate in mutations:
            args = self.fixture(); mutate(args[11][0])
            with self.assertRaises(M.Refusal): M.evaluate(*args)
        args = self.fixture(); args[11][0]["hold"] = copy.deepcopy(args[11][1]["hold"])
        with self.assertRaises(M.Refusal): M.evaluate(*args)

    def test_identity_challenge_expiry_and_collection_binding(self):
        mutations = (lambda a: a[11][0].update(boot_id_end="0" * 36),
                     lambda a: a[11][0].update(challenge="0" * 32),
                     lambda a: a[10].update(collection_id="0" * 32),
                     lambda a: a[10]["challenges"].update(pve04=a[10]["challenges"]["pve01"]),
                     lambda a: a[10].update(expires_at=a[12]-1),
                     lambda a: a[11][0].update(verification_started_at=a[12]+1),
                     lambda a: a[11][0].update(verification_finished_at=a[12]+1),
                     lambda a: a[10]["allowed_effects"].append("remove-hold"),
                     lambda a: a[11][0]["control_plane"].update(quorate=False))
        for mutate in mutations:
            args = self.fixture(); mutate(args)
            with self.assertRaises(M.Refusal): M.evaluate(*args)

    def test_stale_long_running_verification_and_skew_refuse(self):
        args = self.fixture()
        args[12] += 40
        with self.assertRaises(M.Refusal): M.evaluate(*args, max_age=30)
        args = self.fixture()
        args[11][3]["verification_finished_at"] -= 2
        with self.assertRaises(M.Refusal): M.evaluate(*args, max_skew=1)

    def test_current_payload_workers_guard_and_control_vg_fail_closed(self):
        mutations = (lambda r: r["installed"].update(dpkg_state="unpacked"),
                     lambda r: r["payload"].update(dpkg_verify_clean=False),
                     lambda r: r["payload"].update(package_file_list_sha256="0" * 64),
                     lambda r: r["workers"]["pve_tasks"].append("busy"),
                     lambda r: r["thinguard"].update(daemon_pid=r["thinguard"]["daemon_pid"]+1),
                     lambda r: r["thinguard"]["samples"][0].update(observed_at=1))
        for mutate in mutations:
            args = self.fixture(); mutate(args[11][0])
            with self.assertRaises(M.Refusal): M.evaluate(*args)
        args = self.fixture(); args[11][3]["vg_identities"] = copy.deepcopy(args[11][0]["vg_identities"])
        with self.assertRaises(M.Refusal): M.evaluate(*args)

    def test_numeric_aliases_extra_fields_and_bad_limits_refuse(self):
        for key in ("generation", "certificate_size", "verification_started_at"):
            args = self.fixture(); args[11][0][key] = float(args[11][0][key])
            with self.assertRaises(M.Refusal): M.evaluate(*args)
        args = self.fixture(); args[11][0]["release_authorized"] = True
        with self.assertRaises(M.Refusal): M.evaluate(*args)
        for extra in ({"ready_max_age": 901}, {"max_age": True}, {"max_skew": 121}):
            with self.assertRaises(M.Refusal): M.evaluate(*self.fixture(), **extra)

    def test_custom_scalars_containers_and_bytes_are_refused_without_callbacks(self):
        calls = []
        class EvilStr(str):
            def __eq__(self, other): calls.append("eq"); return True
            __hash__ = str.__hash__
        class EvilDict(dict):
            def __deepcopy__(self, memo): calls.append("copy"); return self
        class EvilBytes(bytes):
            def __eq__(self, other): calls.append("bytes-eq"); return True
        variants = []
        args = self.fixture(); args[11][0]["node"] = EvilStr("pve01"); variants.append(args)
        args = self.fixture(); args[10] = EvilDict(args[10]); variants.append(args)
        args = self.fixture(); args[11][0][EvilStr("extra")] = None; variants.append(args)
        args = self.fixture(); args[9] = EvilBytes(args[9]); variants.append(args)
        for args in variants:
            with self.assertRaises(M.Refusal): M.evaluate(*args)
        self.assertEqual(calls, [])

    def test_inputs_and_returned_targets_are_independent(self):
        args = self.fixture(); original = copy.deepcopy(args)
        result = M.evaluate(*args)
        result["hold_by_node"]["pve01"]["source_identity"]["ino"] = 0
        self.assertEqual(args, original)
        self.assertGreater(M.evaluate(*args)["hold_by_node"]["pve01"]["source_identity"]["ino"], 0)

    def test_output_is_accepted_by_archive_pure_request_derivation(self):
        adapter = load("certified_archive_contract", ROOT / "experiments/thick-generations/layout-migration-live-hold-archive.py")
        args = self.fixture(); plan = M.evaluate(*args); now = args[12]
        for node in plan["release_nodes"]:
            hold = plan["hold_by_node"][node]
            auth = {"schema": "slt-live-hold-archive-authorization/v2", "tx": plan["tx"],
                    "generation": plan["generation"], "context_sha256": plan["context_sha256"],
                    "commit_id": plan["commit_id"], "node": node, "boot_id": plan["participant_boots"][node],
                    "all_certified_plan_sha256": plan["plan_sha256"], "certificate_sha256": plan["certificate_sha256"],
                    "manifest_sha256": hold["manifest_sha256"], "source_identity_sha256": M.digest(hold["source_identity"]),
                    "certificate_identity_sha256": M.digest(hold["certificate_identity"]), "operation_id": "9" * 32,
                    "issued_at": now, "expires_at": now + 30, "effect": "archive-exact-active-once"}
            derived = adapter.derive_request(plan, auth, manifest_bytes(args[0], node), args[9],
                                             node, plan["participant_boots"][node], now, 0)
            self.assertEqual(derived["source_identity"], hold["source_identity"])
            self.assertEqual(derived["certificate_identity"], hold["certificate_identity"])


if __name__ == "__main__":
    unittest.main()
