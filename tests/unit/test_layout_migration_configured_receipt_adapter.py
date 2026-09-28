import copy
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "experiments/thick-generations/layout-migration-configured-receipt-adapter.py"
SPEC = importlib.util.spec_from_file_location("configured_receipt_adapter", PATH)
M = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(M)


class Tests(unittest.TestCase):
    def fixture(self):
        candidate = {"package": "pve-sharedlvmthin", "version": "1", "architecture": "all", "flavor": "dual",
                     "artifact_sha256": "a" * 64, "deb_sha256": "b" * 64}
        context = {"tx": "c" * 32, "generation": 38, "context_sha256": "d" * 64,
                   "candidate": candidate, "target_storage_cfg_sha256": "e" * 64}
        manifest = {"schema": "slt-package-maintenance/v1", "tx": context["tx"],
                    "phase": "CONFIG_COMMITTED", "generation": 38, "issued_at": 100,
                    "expires_at": 300, "cluster_name": "lab", "corosync_conf_sha256": "f" * 64,
                    "nodes": [{"name": "pve01", "boot_id": "boot"}],
                    "candidate": {key: candidate[key] for key in
                                  ("package", "version", "flavor", "artifact_sha256", "deb_sha256")},
                    "baseline_storage_cfg_sha256": "1" * 64,
                    "target_storage_cfg_sha256": context["target_storage_cfg_sha256"],
                    "allowed_effects": ["package-unpack", "package-configure-deferred"],
                    "plan_sha256": "2" * 64, "node_evidence": []}
        raw = M.canonical(manifest) + b"\n"
        receipt = {"schema": "slt-package-maintenance-receipt/v1", "tx": context["tx"],
                   "generation": 38, "phase": "PACKAGE_CONFIGURED_DEFERRED", "node": "pve01",
                   "boot_id": "boot", "package": candidate["package"], "version": candidate["version"],
                   "flavor": "dual", "artifact_sha256": candidate["artifact_sha256"],
                   "manifest_sha256": M.digest(raw), "storage_cfg_sha256": "e" * 64,
                   "recorded_at": 200}
        return context, raw, M.canonical(receipt) + b"\n"

    def test_exact_v1_normalizes_without_authorization(self):
        context, manifest, receipt = self.fixture()
        out = M.adapt(receipt, manifest, context, "pve01", "boot")
        self.assertEqual(out["schema"], "slt-package-maintenance-receipt/v2")
        self.assertEqual(out["context_sha256"], context["context_sha256"])
        self.assertNotIn("authorization", out)

    def test_every_identity_boundary_fails_closed(self):
        mutations = [
            lambda c, m, r: r.update(manifest_sha256="0" * 64),
            lambda c, m, r: r.update(storage_cfg_sha256="0" * 64),
            lambda c, m, r: r.update(boot_id="other"),
            lambda c, m, r: r.update(phase="PREINST_ACCEPTED"),
            lambda c, m, r: m.update(phase="PREPARE_READY"),
            lambda c, m, r: m["candidate"].update(version="other"),
            lambda c, m, r: r.update(recorded_at=301),
        ]
        for mutate in mutations:
            context, manifest_raw, receipt_raw = self.fixture()
            manifest = json.loads(manifest_raw); receipt = json.loads(receipt_raw)
            mutate(context, manifest, receipt)
            # Rebind only the ordinary manifest digest so each test reaches its intended invariant.
            if receipt["manifest_sha256"] != "0" * 64:
                receipt["manifest_sha256"] = M.digest(M.canonical(manifest) + b"\n")
            with self.subTest(mutate=mutate), self.assertRaises(M.Refusal):
                M.adapt(M.canonical(receipt) + b"\n", M.canonical(manifest) + b"\n",
                        context, "pve01", "boot")

    def test_duplicate_json_key_refused(self):
        context, manifest, receipt = self.fixture()
        duplicate = receipt[:-2] + b',"tx":"' + b"c" * 32 + b'"}\n'
        with self.assertRaises(M.Refusal):
            M.adapt(duplicate, manifest, context, "pve01", "boot")


if __name__ == "__main__":
    unittest.main()
