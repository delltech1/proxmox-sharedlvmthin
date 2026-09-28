import argparse
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[2]
RELEASE_TEST_PATH = ROOT / "tests/unit/test_layout_migration_release_certificate.py"
RT_SPEC = importlib.util.spec_from_file_location("release_fixtures", RELEASE_TEST_PATH)
RT = importlib.util.module_from_spec(RT_SPEC)
RT_SPEC.loader.exec_module(RT)
PATH = ROOT / "experiments/thick-generations/layout-migration-all-certified-plan.py"
SPEC = importlib.util.spec_from_file_location("all_certified", PATH)
PLAN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLAN)


class AllCertifiedPlanTests(unittest.TestCase):
    now = RT.ReleaseCertificateTests.now

    def fixture(self):
        helper = RT.ReleaseCertificateTests(
            methodName="test_exact_inputs_make_non_releasing_certificate")
        temp, release_args, manifest, _, _, _, _ = helper.release_fixture()
        root = Path(temp.name)
        certificate, certificate_raw = PLAN.CERT.evaluate(release_args)
        certificate_path = root / "certificate.json"
        certificate_path.write_bytes(certificate_raw)
        certificate_sha = PLAN.digest(certificate_raw)
        names = [row["name"] for row in manifest["nodes"]]
        boots = {row["name"]: row["boot_id"] for row in manifest["nodes"]}
        collection = {
            "schema": "slt-layout-certificate-collection/v1",
            "collection_id": "d" * 32, "tx": manifest["tx"],
            "generation": manifest["generation"],
            "commit_id": certificate["commit_id"],
            "manifest_sha256": helper.manifest_sha,
            "certificate_sha256": certificate_sha,
            "candidate": manifest["candidate"], "participant_boots": boots,
            "collector_sha256": "7" * 64,
            "issued_at": self.now, "expires_at": self.now + 120,
            "challenges": {name: str(index + 1) * 32
                           for index, name in enumerate(names)},
            "allowed_effects": [],
        }
        collection_path = root / "collection.json"
        collection_path.write_text(json.dumps(collection, sort_keys=True) + "\n")
        collection_sha = PLAN.digest(collection_path.read_bytes())
        manifest_size = len(Path(release_args.manifest).read_bytes())
        ack_paths = []
        paths = ["/"]
        current = PurePosixPath("/")
        for part in PurePosixPath(PLAN.CERTIFICATE_DIRECTORY).parts[1:]:
            current /= part
            paths.append(str(current))
        for index, node in enumerate(names, 1):
            components = []
            for offset, path in enumerate(paths, 1):
                components.append({
                    "path": path,
                    "identity": {"type": "directory", "dev": 100 + index,
                                 "ino": index * 1000 + offset, "uid": 0,
                                 "mode": 0o755 if offset < 4 else 0o700,
                                 "nlink": 2},
                })
            ack = {
                "schema": "slt-layout-certificate-ack/v1",
                "collection_id": collection["collection_id"],
                "collection_plan_sha256": collection_sha,
                "challenge": collection["challenges"][node],
                "collector_sha256": collection["collector_sha256"],
                "node": node, "boot_id_start": boots[node],
                "boot_id_end": boots[node], "tx": manifest["tx"],
                "generation": manifest["generation"],
                "commit_id": certificate["commit_id"],
                "certificate_sha256": certificate_sha,
                "certificate_size": len(certificate_raw),
                "verification_started_at": self.now,
                "verification_finished_at": self.now,
                "durability": {"result": "CERTIFICATE_DURABLE_LOCAL",
                               "file_fsync": True, "directory_fsync": True,
                               "parent_fsync": True, "exact_reread": True},
                "namespace": {
                    "directory": PLAN.CERTIFICATE_DIRECTORY,
                    "decision_name":
                        f"{manifest['tx']}-{manifest['generation']}.json",
                    "components": components,
                    "certificate": {"type": "regular", "dev": 100 + index,
                                    "ino": index * 1000 + 99, "uid": 0,
                                    "mode": 0o600, "nlink": 1,
                                    "size": len(certificate_raw),
                                    "sha256": certificate_sha},
                },
                "control_plane": {
                    "cluster_name": manifest["cluster_name"],
                    "cluster_nodes": names, "quorate": True,
                    "corosync_conf_sha256": manifest["corosync_conf_sha256"],
                    "storage_cfg_sha256":
                        manifest["target_storage_cfg_sha256"],
                },
                "installed": {
                    "package": manifest["candidate"]["package"],
                    "version": manifest["candidate"]["version"],
                    "flavor": manifest["candidate"]["flavor"],
                    "artifact_sha256":
                        manifest["candidate"]["artifact_sha256"],
                    "dpkg_state": "installed",
                },
                "payload": {"dpkg_verify_complete": True,
                            "dpkg_verify_clean": True,
                            "package_file_list_sha256": "6" * 64},
                "hold": {
                    "active": True, "path": PLAN.ACTIVE_MANIFEST,
                    "manifest_sha256": helper.manifest_sha,
                    "identity": {"type": "regular", "dev": 100 + index,
                                 "ino": index * 1000 + 100, "uid": 0,
                                 "mode": 0o600, "nlink": 1,
                                 "size": manifest_size,
                                 "sha256": helper.manifest_sha},
                },
            }
            path = root / f"{node}-ack.json"
            path.write_text(json.dumps(ack, sort_keys=True) + "\n")
            ack_paths.append(str(path))
        args = argparse.Namespace(
            manifest=release_args.manifest,
            all_configured=release_args.all_configured,
            refresh_authorization=release_args.refresh_authorization,
            all_ready=release_args.all_ready,
            release_authorization=release_args.release_authorization,
            ready_evidence=release_args.node_evidence,
            certificate=str(certificate_path), collection=str(collection_path),
            node_ack=ack_paths, ready_max_age_sec=300,
            ready_max_skew_sec=60, max_age_sec=120, max_skew_sec=60,
            now=self.now)
        return (temp, args, manifest, certificate, certificate_path,
                collection, collection_path, ack_paths)

    def mutate(self, path, callback):
        value = json.loads(Path(path).read_text())
        callback(value)
        Path(path).write_text(json.dumps(value, sort_keys=True) + "\n")

    def test_exact_ack_set_is_non_authorizing(self):
        temp, args, _, certificate, _, _, _, _ = self.fixture()
        self.addCleanup(temp.cleanup)
        result = PLAN.evaluate(args)
        self.assertEqual(result["phase"], "ALL_CERTIFIED")
        self.assertEqual(result["verdict"],
                         "READY_FOR_HOLD_RELEASE_AUTHORIZATION")
        self.assertEqual(result["authorization"], "NONE")
        self.assertFalse(result["hold_released"])
        self.assertFalse(result["mutation_performed"])
        self.assertEqual(result["commit_id"], certificate["commit_id"])

    def test_certificate_must_match_exact_raw_bytes(self):
        temp, args, _, _, path, _, _, _ = self.fixture()
        self.addCleanup(temp.cleanup)
        value = json.loads(path.read_text())
        path.write_text(json.dumps(value, indent=4, sort_keys=True) + "\n")
        with self.assertRaisesRegex(PLAN.Refusal, "bytes differ"):
            PLAN.evaluate(args)

    def test_missing_duplicate_challenge_and_boot_refuse(self):
        temp, args, _, _, _, _, _, paths = self.fixture()
        self.addCleanup(temp.cleanup)
        args.node_ack = paths[:2]
        with self.assertRaisesRegex(PLAN.Refusal, "exactly one"):
            PLAN.evaluate(args)
        temp.cleanup()
        temp, args, _, _, _, _, _, paths = self.fixture()
        self.addCleanup(temp.cleanup)
        args.node_ack[2] = paths[1]
        with self.assertRaisesRegex(PLAN.Refusal, "duplicated"):
            PLAN.evaluate(args)
        temp.cleanup()
        for field, value, message in (
            ("challenge", "f" * 32, "challenge"),
            ("boot_id_end", "0" * 36, "boot"),
        ):
            temp, args, _, _, _, _, _, paths = self.fixture()
            try:
                self.mutate(paths[0], lambda item, f=field, v=value:
                            item.update({f: v}))
                with self.assertRaisesRegex(PLAN.Refusal, message):
                    PLAN.evaluate(args)
            finally:
                temp.cleanup()

    def test_durability_namespace_and_hold_fail_closed(self):
        mutations = [
            (lambda item: item["durability"].update(parent_fsync=False),
             "durability"),
            (lambda item: item["namespace"]["certificate"].update(nlink=2),
             "file inode"),
            (lambda item: item["namespace"]["certificate"].update(mode=0o644),
             "file inode"),
            (lambda item: item["namespace"]["components"][2]["identity"]
             .update(mode=0o777), "group/world"),
            (lambda item: item["hold"].update(active=False), "hold is absent"),
            (lambda item: item["hold"].update(manifest_sha256="0" * 64),
             "hold is absent"),
        ]
        for mutate, message in mutations:
            with self.subTest(message=message):
                temp, args, _, _, _, _, _, paths = self.fixture()
                try:
                    self.mutate(paths[0], mutate)
                    with self.assertRaisesRegex(PLAN.Refusal, message):
                        PLAN.evaluate(args)
                finally:
                    temp.cleanup()

    def test_time_collection_replay_and_control_drift_refuse(self):
        temp, args, _, _, _, collection, collection_path, paths = self.fixture()
        self.addCleanup(temp.cleanup)
        self.mutate(paths[0], lambda item: item.update(
            verification_started_at=self.now - 1,
            verification_finished_at=self.now - 1))
        with self.assertRaisesRegex(PLAN.Refusal, "time is stale"):
            PLAN.evaluate(args)
        temp.cleanup()
        temp, args, _, _, _, collection, collection_path, paths = self.fixture()
        self.addCleanup(temp.cleanup)
        collection["collection_id"] = "e" * 32
        collection["challenges"]["pve01"] = "f" * 32
        collection_path.write_text(json.dumps(collection, sort_keys=True) + "\n")
        with self.assertRaisesRegex(PLAN.Refusal, "identity, challenge"):
            PLAN.evaluate(args)
        temp.cleanup()
        temp, args, _, _, _, _, _, paths = self.fixture()
        self.addCleanup(temp.cleanup)
        self.mutate(paths[1], lambda item: item["control_plane"].update(
            storage_cfg_sha256="0" * 64))
        with self.assertRaisesRegex(PLAN.Refusal, "control-plane"):
            PLAN.evaluate(args)

    def test_collection_raw_hash_and_qualified_payload_are_bound(self):
        temp, args, _, _, _, collection, collection_path, paths = self.fixture()
        self.addCleanup(temp.cleanup)
        collection["expires_at"] -= 1
        collection_path.write_text(json.dumps(collection, sort_keys=True) + "\n")
        with self.assertRaisesRegex(PLAN.Refusal, "identity, challenge"):
            PLAN.evaluate(args)
        temp.cleanup()
        temp, args, _, _, _, _, _, paths = self.fixture()
        self.addCleanup(temp.cleanup)
        for path in paths:
            self.mutate(path, lambda item: item["payload"].update(
                package_file_list_sha256="5" * 64))
        with self.assertRaisesRegex(PLAN.Refusal, "payload proof"):
            PLAN.evaluate(args)

    def test_outer_manifest_swap_and_unbounded_ready_limits_refuse(self):
        temp, args, manifest, _, _, _, _, _ = self.fixture()
        self.addCleanup(temp.cleanup)
        real_evaluate = PLAN.CERT.evaluate
        def swap_then_evaluate(namespace):
            manifest["candidate"]["artifact_sha256"] = "0" * 64
            Path(args.manifest).write_text(json.dumps(manifest, sort_keys=True)
                                           + "\n")
            return real_evaluate(namespace)
        from unittest import mock
        with mock.patch.object(PLAN.CERT, "evaluate",
                               side_effect=swap_then_evaluate):
            with self.assertRaisesRegex(PLAN.Refusal,
                                        "different manifest|identity differs"):
                PLAN.evaluate(args)
        temp.cleanup()
        for field, value, message in (
            ("ready_max_age_sec", 901, "READY evidence age"),
            ("ready_max_skew_sec", 121, "READY evidence skew"),
            ("max_age_sec", 301, "ACK age"),
            ("max_skew_sec", 121, "ACK skew"),
        ):
            temp, args, _, _, _, _, _, _ = self.fixture()
            try:
                setattr(args, field, value)
                with self.assertRaisesRegex(PLAN.Refusal, message):
                    PLAN.evaluate(args)
            finally:
                temp.cleanup()

    def test_numeric_alias_unknown_field_and_duplicate_json_refuse(self):
        temp, args, _, _, _, _, _, paths = self.fixture()
        self.addCleanup(temp.cleanup)
        self.mutate(paths[0], lambda item: item.update(generation=7.0))
        with self.assertRaisesRegex(PLAN.Refusal, "identity"):
            PLAN.evaluate(args)
        temp.cleanup()
        temp, args, _, _, _, _, _, paths = self.fixture()
        self.addCleanup(temp.cleanup)
        self.mutate(paths[0], lambda item: item.update(release_hold=True))
        with self.assertRaisesRegex(PLAN.Refusal, "fields do not match"):
            PLAN.evaluate(args)
        temp.cleanup()
        temp, args, _, _, _, _, _, paths = self.fixture()
        self.addCleanup(temp.cleanup)
        raw = Path(paths[0]).read_text().rstrip()
        Path(paths[0]).write_text(raw[:-1] + ',"node":"pve99"}\n')
        with self.assertRaisesRegex(PLAN.Refusal, "duplicate JSON key"):
            PLAN.evaluate(args)


if __name__ == "__main__":
    unittest.main()
