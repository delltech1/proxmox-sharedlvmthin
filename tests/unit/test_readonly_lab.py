import importlib.machinery
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def load(name, relative):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / relative))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class ReadOnlyLabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.observe = load(
            "readonly_observe",
            "usr/libexec/pve-sharedlvmthin/sharedlvmthin-readonly-lab-observe",
        )
        cls.oracle = load(
            "readonly_oracle",
            "usr/libexec/pve-sharedlvmthin/sharedlvmthin-readonly-lab-oracle",
        )

    def common(self, scenario):
        return {
            "kind": "readonly-lab-observation", "schema": 1,
            "observation": {
                "scenario_id": scenario, "package_flavor": "dual",
                "plugin_sha256": "a" * 64, "storage_cfg_sha256": "b" * 64,
            },
        }

    def test_storage_node_parser_is_exact_and_does_not_inherit_scope(self):
        parsed = self.observe.parse_storage_nodes(
            "sharedlvmthin: first\n\tnodes n2,n1,n1\n\tshared 1\n\n"
            "dir: local\n\tpath /var/lib/vz\n"
            "sharedlvmthin: second\n\tdisable\n"
        )
        self.assertEqual(parsed, {
            "first": {"nodes": ["n1", "n2"], "disabled": False},
            "second": {"nodes": None, "disabled": True},
        })

    def test_web_oracle_requires_api_hooks_assets_and_loaded_unit(self):
        document = self.common("web-ui-registration")
        row = document["observation"]
        row.update({
            "api_probe": {"rc": 0, "stdout": (
                '{"runtime":15,"plugin":15,"type":"sharedlvmthin",'
                '"hooks":{"alloc_image":1,"free_image":1}}'), "stderr": ""},
            "web_assets": {"index.html": {"size": 10, "sha256": "c" * 64}},
            "web_service": {"rc": 0, "stdout": "loaded\nenabled\nactive\n", "stderr": ""},
        })
        self.assertTrue(all(self.oracle.evaluate(document, "web-ui-registration").values()))
        row["api_probe"]["stdout"] = row["api_probe"]["stdout"].replace(
            '"free_image":1', '"free_image":0')
        self.assertFalse(all(self.oracle.evaluate(document, "web-ui-registration").values()))

    def test_node_scope_refuses_unknown_member(self):
        document = self.common("node-scope")
        row = document["observation"]
        row.update({
            "cluster_nodes": {"rc": 0, "stdout": '["n1","n2"]', "stderr": ""},
            "sharedlvmthin_storages": {
                "safe": {"nodes": ["n1", "n2"], "disabled": False}},
        })
        self.assertTrue(all(self.oracle.evaluate(document, "node-scope").values()))
        row["sharedlvmthin_storages"]["safe"]["nodes"].append("foreign")
        self.assertFalse(all(self.oracle.evaluate(document, "node-scope").values()))

    def test_quorum_oracle_requires_native_and_perl_proofs(self):
        document = self.common("quorum-read-only")
        row = document["observation"]
        row.update({
            "pvecm_status": {"rc": 0, "stdout": "Quorate: Yes\n", "stderr": ""},
            "quorum_probe": {"rc": 0, "stdout": "QUORATE\n", "stderr": ""},
            "corosync_sha256": "d" * 64,
        })
        self.assertTrue(all(self.oracle.evaluate(document, "quorum-read-only").values()))
        row["quorum_probe"]["rc"] = 1
        self.assertFalse(all(self.oracle.evaluate(document, "quorum-read-only").values()))


if __name__ == "__main__":
    unittest.main()
