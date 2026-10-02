import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin_update_policy.py"
SPEC = importlib.util.spec_from_file_location("update_policy", SOURCE)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def action(package, old="1", new="2", operation="/var/cache/apt/pkg.deb"):
    return {"package": package, "old_version": old, "old_arch": "amd64",
            "old_multiarch": "none", "direction": "<", "new_version": new,
            "new_arch": "amd64", "new_multiarch": "none", "action": operation}


class UpdatePolicyTests(unittest.TestCase):
    def manifest(self):
        return {
            "watched_packages": ["libpve-storage-perl", "qemu-server", "lvm2"],
            "watched_package_patterns": [r"(?:proxmox|pve)-kernel-.*"],
            "known_incompatible": [{"storage_min": "3", "qemu_max_exclusive": "3", "reason": "bad pair"}],
            "tuples": [
                {"id": "old", "profiles": ["dual"], "api": 15, "running_kernel": "k1",
                 "plugin_versions": ["p1"], "scopes": ["san-dataplane"],
                 "status": "EXACT_LAB_TESTED",
                 "packages": {"libpve-storage-perl": "1", "qemu-server": "1"}},
                {"id": "new", "profiles": ["dual"], "api": 15, "running_kernel": "k1",
                 "plugin_versions": ["p1"], "scopes": ["san-dataplane"],
                 "status": "EXACT_LAB_TESTED",
                 "packages": {"libpve-storage-perl": "2", "qemu-server": "2"}},
            ],
            "rolling_edges": [{"id": "old-new", "from": "old", "to": "new", "status": "QUALIFIED"}],
        }

    def context(self):
        return {"profile": "dual", "api": 15, "running_kernel": "k1", "plugin_version": "p1",
                "installed": {"libpve-storage-perl": "1", "qemu-server": "1", "lvm2": "1"}}

    @staticmethod
    def compare(left, operator, right):
        values = {"lt": left < right, "ge": left >= right}
        return values[operator]

    def evaluate(self, actions, mode="QUALIFIED_ONLY", manifest=None, context=None):
        return M.evaluate({"protocol": 3, "actions": actions}, manifest or self.manifest(),
                          {"mode": mode, "required_scope": "san-dataplane"},
                          context or self.context(), self.compare)

    def test_protocol_v3_is_strict_and_complete(self):
        raw = (b"VERSION 3\nAPT::Architecture=amd64\n\n"
               b"qemu-server 1 amd64 none < 2 amd64 none /tmp/qemu.deb\n")
        parsed = M.parse_protocol_v3(raw)
        self.assertEqual(parsed["actions"][0]["new_version"], "2")
        for bad in (b"", b"VERSION 2\n\n", b"VERSION 3\nmissing\n\n",
                    b"VERSION 3\n\nqemu-server too few fields\n"):
            with self.subTest(bad=bad):
                with self.assertRaises(M.Refusal):
                    M.parse_protocol_v3(bad)

    def test_unrelated_security_update_is_not_blocked(self):
        result = self.evaluate([action("openssl")])
        self.assertEqual(result["verdict"], "ALLOW_UNRELATED")
        self.assertFalse(result["post_gate_required"])

    def test_qualified_exact_edge_is_allowed_and_requires_post_gate(self):
        result = self.evaluate([action("libpve-storage-perl"), action("qemu-server")])
        self.assertEqual(result["verdict"], "ALLOW_QUALIFIED_EDGE")
        self.assertTrue(result["post_gate_required"])

    def test_advisory_operation_matrix_never_changes_package_admission(self):
        actions = [action("libpve-storage-perl"), action("qemu-server")]
        baseline = self.manifest()
        advisory = self.manifest()
        advisory.update({
            "operation_groups": ["snapshot-vmstate"],
            "operation_qualification_mode": "ADVISORY_ONLY",
            "operation_authorization": "NONE",
            "operation_evidence_ids": ["fake-description"],
        })
        for item in advisory["tuples"]:
            item["operation_qualifications"] = {
                "snapshot-vmstate": {
                    "status": "QUALIFIED",
                    "evidence": ["fake-description"],
                },
            }
        self.assertEqual(
            self.evaluate(actions, manifest=advisory),
            self.evaluate(actions, manifest=baseline),
        )

    def test_partial_update_does_not_invent_a_qualified_tuple(self):
        result = self.evaluate([action("libpve-storage-perl")])
        self.assertEqual(result["verdict"], "REFUSE_UNQUALIFIED")

    def test_unqualified_edge_and_unlisted_plugin_refuse(self):
        manifest = self.manifest()
        manifest["rolling_edges"] = []
        self.assertEqual(self.evaluate([action("libpve-storage-perl"), action("qemu-server")],
                                       manifest=manifest)["verdict"], "REFUSE_UNQUALIFIED_EDGE")
        context = self.context(); context["plugin_version"] = "other"
        self.assertEqual(self.evaluate([action("qemu-server", "1", "1", "**CONFIGURE**")],
                                       context=context)["verdict"], "REFUSE_UNQUALIFIED")

    def test_control_plane_scope_never_authorizes_san(self):
        manifest = self.manifest()
        manifest["tuples"][0]["scopes"] = ["control-plane"]
        result = self.evaluate([action("qemu-server", "1", "1", "**CONFIGURE**")], manifest=manifest)
        self.assertEqual(result["verdict"], "REFUSE_UNQUALIFIED")

    def test_freeze_refuses_payload_remove_reinstall_and_configure(self):
        for candidate in (action("qemu-server"), action("qemu-server", "1", "-", "**REMOVE**"),
                          action("qemu-server", "1", "1", "/tmp/reinstall.deb")):
            self.assertEqual(self.evaluate([candidate], mode="FREEZE")["verdict"], "REFUSE_FREEZE")
        configure = action("qemu-server", "1", "1", "**CONFIGURE**")
        self.assertEqual(self.evaluate([configure], mode="FREEZE")["verdict"], "REFUSE_FREEZE")
        changed_configure = dict(configure, old_version="9.1.9", new_version="9.1.10")
        self.assertEqual(self.evaluate([changed_configure], mode="FREEZE")["verdict"], "REFUSE_FREEZE")

    def test_qualified_refuses_unrepresented_watched_configure_replay(self):
        manifest = self.manifest()
        manifest["watched_packages"].append("libcfg7")
        configure = action("libcfg7", "1", "2", "**CONFIGURE**")
        result = self.evaluate([configure], mode="QUALIFIED_ONLY", manifest=manifest)
        self.assertEqual(result["verdict"], "REFUSE_UNQUALIFIED_PACKAGE_CHANGE")
        self.assertEqual(result["packages"], ["libcfg7"])

    def test_qualified_refuses_architecture_and_multiarch_identity_changes(self):
        architecture = action("qemu-server", "1", "1", "**CONFIGURE**")
        architecture["new_arch"] = "arm64"
        result = self.evaluate([architecture], mode="QUALIFIED_ONLY")
        self.assertEqual(result["verdict"], "REFUSE_UNQUALIFIED_PACKAGE_IDENTITY_CHANGE")
        self.assertEqual(result["identity_changes"][0]["reason"], "architecture-change")

        multiarch = action("qemu-server", "1", "1", "**CONFIGURE**")
        multiarch["new_multiarch"] = "foreign"
        result = self.evaluate([multiarch], mode="QUALIFIED_ONLY")
        self.assertEqual(result["verdict"], "REFUSE_UNQUALIFIED_PACKAGE_IDENTITY_CHANGE")
        self.assertEqual(result["identity_changes"][0]["reason"], "multiarch-change")

    def test_qualified_edge_refuses_legacy_name_only_allowance(self):
        manifest = self.manifest()
        manifest["watched_packages"].append("libcfg7")
        manifest["rolling_edges"][0]["allowed_package_changes"] = ["libcfg7"]
        actions = [action("libpve-storage-perl"), action("qemu-server"),
                   action("libcfg7", "1", "999")]
        result = self.evaluate(actions, mode="QUALIFIED_ONLY", manifest=manifest)
        self.assertEqual(result["verdict"], "REFUSE_UNSAFE_EDGE_ALLOWANCE")
        self.assertEqual(result["packages"], ["libcfg7"])

    def test_retest_required_tuple_is_never_qualified(self):
        manifest = self.manifest()
        manifest["tuples"][0]["status"] = "RETEST_REQUIRED"
        manifest["tuples"][0]["required_tests"] = ["still-missing"]
        configure = action("libpve-storage-perl", "1", "1", "**CONFIGURE**")
        self.assertEqual(self.evaluate([configure], manifest=manifest)["verdict"], "REFUSE_UNQUALIFIED")
        observed = M.observed_tuple(
            manifest, self.context()["installed"], profile="dual", api=15,
            running_kernel="k1", plugin_version="p1",
        )
        self.assertEqual(observed["status"], "RETEST_REQUIRED")

    def test_every_direct_provider_is_watched(self):
        manifest = self.manifest()
        for package in ("libpve-guest-common-perl", "pve-cluster", "dmsetup", "open-iscsi"):
            manifest["watched_packages"].append(package)
            candidate = action(package, "1", "2")
            self.assertNotEqual(self.evaluate([candidate], mode="FREEZE", manifest=manifest)["verdict"],
                                "ALLOW_UNRELATED")

    def test_unselected_and_manual_modes_require_operator_action(self):
        candidate = action("qemu-server")
        self.assertEqual(self.evaluate([candidate], mode="UNSELECTED")["verdict"],
                         "REFUSE_POLICY_UNSELECTED")
        self.assertEqual(self.evaluate([candidate], mode="MANUAL_OVERRIDE")["verdict"],
                         "REQUIRE_EXACT_MANUAL_AUTHORIZATION")
        self.assertEqual(self.evaluate([candidate], mode="WARN")["verdict"],
                         "REQUIRE_EXACT_MANUAL_AUTHORIZATION")

    def test_schema2_qualified_auto_retains_exact_tuple_semantics(self):
        candidate = action("libpve-storage-perl", "1", "1")
        self.assertEqual(
            self.evaluate([candidate], mode="QUALIFIED_AUTO"),
            self.evaluate([candidate], mode="QUALIFIED_ONLY"),
        )

    def test_manual_authorization_is_exactly_bound_to_plan_payloads_and_context(self):
        payloads = [{"path": "/x", "sha256": "b" * 64, "size": 1}]
        context = {"schema": 1, "node": "pve1", "boot_id": "boot-a",
                   "manifest_sha256": "c" * 64, "policy_sha256": "d" * 64,
                   "watched_state": [], "context_sha256": "e" * 64}
        authorization = {"schema": 2, "authorization_id": "f" * 32,
                         "plan_digest": "a" * 64, "payloads": payloads,
                         "context": context}
        self.assertTrue(M.manual_authorization_matches(
            authorization, "a" * 64, payloads, context
        ))
        mutations = [
            dict(authorization, schema=1),
            dict(authorization, authorization_id="bad"),
            dict(authorization, plan_digest="0" * 64),
            dict(authorization, payloads=[]),
            dict(authorization, context=dict(context, boot_id="boot-b")),
            dict(authorization, context=dict(context, manifest_sha256="0" * 64)),
            dict(authorization, context=dict(context, watched_state=[{"package": "qemu-server"}])),
        ]
        for candidate in mutations:
            with self.subTest(candidate=candidate):
                self.assertFalse(M.manual_authorization_matches(
                    candidate, "a" * 64, payloads, context
                ))

    def test_kernel_pattern_is_watched_and_unqualified(self):
        result = self.evaluate([action("proxmox-kernel-7.0.14-20-pve-signed")])
        self.assertEqual(result["verdict"], "REFUSE_UNQUALIFIED_PACKAGE_CHANGE")
        # A watched package absent from tuple requirements cannot silently alter
        # tuple identity; a payload change must be explicitly represented.
        self.assertFalse(result["post_gate_required"])

    def test_repeated_protocol_config_is_retained_and_unsafe_payload_refuses(self):
        parsed = M.parse_protocol_v3(b"VERSION 3\na=b\na=c\n\n")
        self.assertEqual(parsed["protocol"], 3)
        with self.assertRaises(M.Refusal):
            M.parse_protocol_v3(b"VERSION 3\n\nqemu-server 1 amd64 none < 2 amd64 none relative.deb\n")


if __name__ == "__main__":
    unittest.main()
