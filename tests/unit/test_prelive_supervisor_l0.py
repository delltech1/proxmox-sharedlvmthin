import importlib.util
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "experiments/thick-generations/prelive-supervisor-l0.py"
SPEC = importlib.util.spec_from_file_location("prelive_supervisor_l0", SCRIPT)
LAB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAB)


class PreliveSupervisorL0Tests(unittest.TestCase):
    def test_mount_parser_selects_longest_parent_and_decodes(self):
        text = (
            "1 0 8:1 / / rw - ext4 /dev/root rw\n"
            "2 1 8:2 / /var rw - xfs /dev/local rw\n"
            "3 2 8:3 / /var/tmp\\040lab ro - tmpfs tmpfs ro\n"
        )
        value = LAB.mount_identity("/var/tmp lab/x", text)
        self.assertEqual(value["mountpoint"], "/var/tmp lab")
        self.assertEqual(value["fstype"], "tmpfs")
        self.assertTrue(value["readonly"])

    def test_wrong_host_refuses_before_runtime_collection(self):
        with mock.patch.object(LAB.platform, "node", return_value="actual"), \
             mock.patch.object(LAB, "sha256_file") as digest:
            with self.assertRaisesRegex(RuntimeError, "hostname"):
                LAB.collect("expected")
        digest.assert_not_called()

    def test_source_has_no_spawn_or_subreaper_set_surface(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in ("subprocess", "os.fork", "Popen", "posix_spawn",
                          "PR_SET_CHILD_SUBREAPER", "dmsetup", "vgs", "lvs"):
            self.assertNotIn(forbidden, source)
        self.assertIn("PR_GET_CHILD_SUBREAPER", source)
        self.assertIn('"mutation_performed": False', source)
        self.assertIn('"child_spawned": False', source)

    def test_feature_result_never_authorizes_runtime(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('"runtime_authorized": False', source)
        self.assertIn("L0_FEATURES_PRESENT_MODEL_REVIEW_ONLY", source)


if __name__ == "__main__":
    unittest.main()
