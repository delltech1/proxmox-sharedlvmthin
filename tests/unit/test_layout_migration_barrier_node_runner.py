import importlib.util
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


R = load("node_runner_test", ROOT / "experiments/thick-generations/layout-migration-barrier-node-runner.py")


class Tests(unittest.TestCase):
    def test_executor_accepts_only_closed_exact_argv(self):
        with mock.patch.object(R.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = b""
            run.return_value.stderr = b""
            for argv in R.COMMANDS:
                self.assertTrue(R.execute(argv))
            self.assertEqual(run.call_count, len(R.COMMANDS))

    def test_executor_rejects_shell_arbitrary_and_reordered_argv(self):
        bad = [
            ("/bin/sh", "-c", "systemctl stop pvedaemon"),
            ("/usr/bin/systemctl", "start", "--", "pvedaemon.service"),
            ("/usr/bin/systemctl", "unmask", "--", "pvedaemon.service"),
            ("/usr/bin/systemctl", "stop", "--", *reversed(sorted(R.B.MODEL.REQUIRED_STOPS))),
        ]
        with mock.patch.object(R.subprocess, "run") as run:
            for argv in bad:
                with self.assertRaises(R.Refusal):
                    R.execute(argv)
            run.assert_not_called()

    def test_nonzero_and_unbounded_output_refuse(self):
        argv = next(iter(R.COMMANDS))
        cases = ((1, b"", b""), (0, b"x" * (1024 * 1024 + 1), b""),
                 (0, b"", b"x" * (1024 * 1024 + 1)))
        for code, out, err in cases:
            with mock.patch.object(R.subprocess, "run") as run:
                run.return_value.returncode = code
                run.return_value.stdout = out
                run.return_value.stderr = err
                with self.assertRaises(R.Refusal):
                    R.execute(argv)


if __name__ == "__main__":
    unittest.main()
