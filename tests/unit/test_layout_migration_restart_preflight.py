import copy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


H = load("restart_fixture", ROOT / "tests/unit/test_layout_migration_v2_v1_adapter.py")
BH = load("restart_barrier_fixture", ROOT / "tests/unit/test_layout_migration_barrier_receipt_adapter.py")
A = H.M
R = A.RESTART
C = load("restart_composer_test", ROOT / "experiments/thick-generations/layout-migration-offline-composer.py")


def restart_rows(f):
    base, target, template, context, rows, barrier, auth, now = f
    old = A.project(context, template, base, target, rows, barrier, auth, now)["manifest"]
    old.update(tx="1" * 32, issued_at=now - 200, expires_at=now - 100)
    old_raw = A.canonical(old)
    for row in rows:
        if row["role"] == "CONTROL_ONLY": continue
        package = {**{key: row["candidate"][key] for key in ("package", "version", "flavor", "artifact_sha256")},
                   "dpkg_state": "unpacked", "config_version": "0.9.0~rc5.12~tg33"}
        receipt = {"schema": "slt-package-maintenance-receipt/v1", "tx": old["tx"], "generation": old["generation"],
            "phase": "PREINST_ACCEPTED", "node": row["node"], "boot_id": row["role_evidence"]["boot_id"],
            **{key: package[key] for key in ("package", "version", "flavor", "artifact_sha256")},
            "manifest_sha256": R.digest(old_raw), "storage_cfg_sha256": context["baseline_storage_cfg_sha256"], "recorded_at": now - 150}
        receipt_raw = A.canonical(receipt) + b"\n"
        def directory(ino): return {"kind": "directory", "dev": 1, "ino": ino, "uid": 0, "mode": 0o700}
        def file(ino, raw): return {"kind": "file", "identity": [1, ino, 0, 0o100600, 1, len(raw), 1, 1], "sha256": R.digest(raw)}
        tree = {"": directory(1), "active.json": file(2, old_raw), ".maintenance.lock": file(3, b""),
                "attempts": directory(4), "sidecars": directory(5), "receipts": directory(6),
                "receipts/" + old["tx"] + ".json": file(7, receipt_raw)}
        request = {"schema": "slt-expired-prepare-archive/v1", "operation_id": "2" * 32,
            "node": row["node"], "boot_id": row["role_evidence"]["boot_id"], "manifest_sha256": R.digest(old_raw),
            "evidence_tree_sha256": R.digest(R.canonical(tree)), "storage_cfg_sha256": context["baseline_storage_cfg_sha256"],
            "expected_package": copy.deepcopy(package), "receipt_sha256": {old["tx"] + ".json": R.digest(receipt_raw)}}
        row.update(schema=R.SCHEMA, installed=package, recovery={"schema": R.RECOVERY, "tx": context["tx"],
            "generation": context["generation"], "observed_at": now - 2,
            "archive_path": "/var/lib/pve-sharedlvmthin/maintenance.expired-" + old["tx"] + "-" + "2" * 32,
            "archive_tree": tree, "abort_request": request, "predecessor_manifest_hex": old_raw.hex(),
            "preinst_receipt_hex": receipt_raw.hex(), "maintenance_absent": True, "cas_tree": {"": directory(8)},
            "blocking_processes": [], "authorization": "NONE", "mutation_performed": False})
    return f


class Tests(unittest.TestCase):
    def fixture(self): return restart_rows(list(H.Tests().fixture()))

    def project(self, f): return A.project(f[3], f[2], f[0], f[1], f[4], f[5], f[6], f[7])

    def test_explicit_unpacked_restart_projects_new_prepare_and_keeps_provenance(self):
        f = self.fixture(); out = self.project(f)
        self.assertEqual(out["manifest"]["tx"], "c" * 32)
        self.assertEqual(len(out["sidecar"]["plan"]["recovery_by_node"]), 3)
        self.assertEqual(f[4][0]["installed"]["dpkg_state"], "unpacked")
        self.assertEqual(f[4][-1]["installed"]["dpkg_state"], "installed")
        self.assertEqual(out["sidecar"]["actions"][-1]["action"], "VERIFY_CURRENT_ONLY")

    def test_strictly_newer_replacement_keeps_exact_unpacked_predecessor(self):
        f = self.fixture()
        for row in f[4]:
            row["candidate"].update(
                version="0.9.0~rc5.16~tg37", artifact_sha256="a" * 64,
                deb_sha256="b" * 64)
            if row["role"] == "SAN_PARTICIPANT":
                row["recovery"]["schema"] = R.RECOVERY_REPLACEMENT
                row["recovery"]["successor_version_order"] = "LT"
            else:
                row["installed"].update(
                    version="0.9.0~rc5.16~tg37", artifact_sha256="a" * 64)
        f[3] = A.CTX.build_context(
            {"tx": "c" * 32, "generation": 1}, f[4][0]["candidate"],
            f[2], f[0], f[1])
        f[5]["context_sha256"] = f[3]["context_sha256"]
        f[6]["context_sha256"] = f[3]["context_sha256"]
        out = self.project(f)
        self.assertEqual(len(out["sidecar"]["plan"]["recovery_by_node"]), 3)
        self.assertTrue(all(
            row["installed"]["version"] != row["candidate"]["version"]
            for row in f[4] if row["role"] == "SAN_PARTICIPANT"))

    def test_replacement_may_correct_candidate_serialized_target(self):
        f = self.fixture()
        for row in f[4]:
            row["candidate"].update(version="0.9.0~rc5.17~tg38",
                                    artifact_sha256="a" * 64, deb_sha256="b" * 64)
            if row["role"] == "CONTROL_ONLY":
                row["installed"].update(version="0.9.0~rc5.17~tg38", artifact_sha256="a" * 64)
                continue
            recovery = row["recovery"]
            recovery["schema"] = R.RECOVERY_REPLACEMENT
            recovery["successor_version_order"] = "LT"
            old = R.decode(bytes.fromhex(recovery["predecessor_manifest_hex"]))
            old["target_storage_cfg_sha256"] = "9" * 64
            old_raw = R.canonical(old)
            receipt = R.decode(bytes.fromhex(recovery["preinst_receipt_hex"]))
            receipt["manifest_sha256"] = R.digest(old_raw)
            receipt_raw = R.canonical(receipt) + b"\n"
            recovery["predecessor_manifest_hex"] = old_raw.hex()
            recovery["preinst_receipt_hex"] = receipt_raw.hex()
            request = recovery["abort_request"]
            request["manifest_sha256"] = R.digest(old_raw)
            request["receipt_sha256"] = {old["tx"] + ".json": R.digest(receipt_raw)}
            tree = recovery["archive_tree"]
            tree["active.json"].update(sha256=R.digest(old_raw))
            tree["receipts/" + old["tx"] + ".json"].update(
                sha256=R.digest(receipt_raw), identity=[1, 7, 0, 0o100600, 1, len(receipt_raw), 1, 1])
            request["evidence_tree_sha256"] = R.digest(R.canonical(tree))
        f[3] = A.CTX.build_context({"tx": "c" * 32, "generation": 1}, f[4][0]["candidate"],
                                   f[2], f[0], f[1])
        f[5]["context_sha256"] = f[3]["context_sha256"]
        f[6]["context_sha256"] = f[3]["context_sha256"]
        self.assertEqual(len(self.project(f)["sidecar"]["plan"]["recovery_by_node"]), 3)

    def test_replacement_requires_live_order_proof_and_distinct_successor(self):
        for mutate in (
                lambda row: row["recovery"].update(successor_version_order="UNKNOWN"),
                lambda row: row["candidate"].update(version=row["installed"]["version"]),
                lambda row: row["candidate"].update(artifact_sha256=row["installed"]["artifact_sha256"])):
            f = self.fixture(); row = f[4][0]
            row["candidate"].update(
                version="0.9.0~rc5.16~tg37", artifact_sha256="a" * 64,
                deb_sha256="b" * 64)
            row["recovery"]["schema"] = R.RECOVERY_REPLACEMENT
            row["recovery"]["successor_version_order"] = "LT"
            mutate(row)
            with self.assertRaises(A.Refusal):
                self.project(f)

    def test_composer_preserves_recovery_even_in_non_authorizing_draft(self):
        f = self.fixture(); topology = {**f[2], "storage_cfg_sha256": R.digest(f[0])}
        evidence = C.CTX.TOPO.validate(topology, f[0])
        out = C.compose({"tx": f[3]["tx"], "generation": 1}, topology, evidence, f[4], f[0], f[1], "pve01", f[7])
        self.assertEqual(len(out["recovery_by_node"]), 3)
        self.assertIsNone(out["prepare"])
        out = C.compose({"tx": f[3]["tx"], "generation": 1}, topology, evidence, f[4], f[0], f[1], "pve01", f[7],
            barrier=f[5], adapter_auth_fields={key: f[6][key] for key in ("authorization_id", "issued_at", "expires_at")})
        self.assertEqual(len(out["prepare"]["sidecar"]["plan"]["recovery_by_node"]), 3)
        self.assertFalse(out["execution_authorized"])

    def test_fresh_receipt_barrier_accepts_explicit_restart_cohort(self):
        f = self.fixture(); bf = BH.Tests().fixture()
        bf[6] = f[4]
        result = BH.M.project(*bf)
        self.assertEqual(result["authorization"], "HOLD_ACTIVE")

    def test_relabeling_unpacked_or_control_restart_refuses(self):
        for mutate in (
                lambda f: f[4][0].update(schema="slt-layout-node-maintenance-evidence/v2"),
                lambda f: f[4][0]["installed"].update(dpkg_state="installed"),
                lambda f: f[4][-1].update(schema=R.SCHEMA, recovery=f[4][0]["recovery"]),
                lambda f: f[4][0]["installed"].update(config_version=f[4][0]["candidate"]["version"])):
            f = self.fixture(); mutate(f)
            with self.assertRaises(A.Refusal): self.project(f)

    def test_foreign_archive_receipt_or_package_refuses(self):
        for mutate in (
                lambda r: r.update(archive_path="/tmp/archive"),
                lambda r: r["abort_request"].update(evidence_tree_sha256="f" * 64),
                lambda r: r["abort_request"]["expected_package"].update(config_version="foreign"),
                lambda r: r.update(preinst_receipt_hex=R.canonical({"phase": "PACKAGE_CONFIGURED_DEFERRED"}).hex()),
                lambda r: r["archive_tree"]["active.json"].update(sha256="e" * 64)):
            f = self.fixture(); mutate(f[4][0]["recovery"])
            with self.assertRaises(A.Refusal): self.project(f)

    def test_new_transaction_current_no_cas_no_hold_and_freshness_are_required(self):
        for mutate in (
                lambda r: r.update(tx="1" * 32),
                lambda r: r.update(generation=True),
                lambda r: r.update(observed_at=1),
                lambda r: r.update(maintenance_absent=False),
                lambda r: r.update(blocking_processes=["dpkg"]),
                lambda r: r["cas_tree"].update({"INTENT.json": {"kind": "file"}}),
                lambda r: r["cas_tree"].update({".OUTCOME.json.tmp": {"kind": "file"}})):
            f = self.fixture(); mutate(f[4][0]["recovery"])
            with self.assertRaises(A.Refusal): self.project(f)

    def test_recovery_changes_are_bound_into_manifest_plan(self):
        f = self.fixture(); before = self.project(f)["manifest"]["plan_sha256"]
        f[4][0]["recovery"]["cas_tree"][""]["ino"] = 42
        self.assertNotEqual(self.project(f)["manifest"]["plan_sha256"], before)

    def test_restart_cannot_silently_mix_normal_and_recovery_san_rows(self):
        f = self.fixture()
        f[4][1] = H.Tests().fixture()[4][1]
        with self.assertRaises(A.Refusal): self.project(f)

    def test_self_rehashed_unsafe_archive_or_cas_inode_refuses(self):
        for field in ("archive_tree", "cas_tree"):
            f = self.fixture(); recovery = f[4][0]["recovery"]
            recovery[field][""]["mode"] = 0o777
            if field == "archive_tree":
                recovery["abort_request"]["evidence_tree_sha256"] = R.digest(R.canonical(recovery[field]))
            with self.assertRaises(A.Refusal): self.project(f)


if __name__ == "__main__": unittest.main()
