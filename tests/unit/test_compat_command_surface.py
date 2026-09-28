import importlib.machinery
import importlib.util
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-upstream-inventory"
COMPAT = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-compat-check"


def load_inventory():
    loader = importlib.machinery.SourceFileLoader("slt_inventory_commands", str(INVENTORY))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class CompatibilityCommandSurfaceTests(unittest.TestCase):
    def test_live_probe_and_fingerprint_cover_the_same_commands(self):
        inventory = load_inventory()
        source = COMPAT.read_text(encoding="utf-8")
        match = re.search(
            r"SLT_REQUIRED_RUNTIME_COMMANDS='([^']+)'",
            source,
            flags=re.MULTILINE,
        )
        self.assertIsNotNone(match)
        probed = match.group(1).split()
        self.assertEqual(len(probed), len(set(probed)))
        self.assertEqual(set(probed), set(inventory.COMMANDS))

    def test_each_runtime_command_emits_an_individual_result(self):
        source = COMPAT.read_text(encoding="utf-8")
        self.assertIn('echo "PASS=command:$command"', source)
        self.assertIn('echo "FAIL=command:$command"', source)
        self.assertIn('for command in $SLT_REQUIRED_RUNTIME_COMMANDS; do', source)


if __name__ == "__main__":
    unittest.main()
