import importlib.machinery
import importlib.util
import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-qmp-path-check"


def load_helper():
    loader = importlib.machinery.SourceFileLoader("qmp_path_check", str(HELPER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class QmpPathCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helper = load_helper()

    def test_collects_only_canonical_device_filenames(self):
        result = set()
        self.helper.collect_device_paths({
            "filename": "/dev/mapper/example",
            "backing-image": {"filename": "json:not-a-device"},
            "children": [{"filename": "/tmp/not-a-device"}],
        }, result)
        self.assertEqual(result, {os.path.realpath("/dev/mapper/example")})

    def test_slot_grammar_excludes_shell_and_path_syntax(self):
        self.assertIsNotNone(self.helper.SLOT_RE.fullmatch("scsi12"))
        for value in ("scsi0;id", "../scsi0", "unused0", "scsi"):
            self.assertIsNone(self.helper.SLOT_RE.fullmatch(value))


if __name__ == "__main__":
    unittest.main()
