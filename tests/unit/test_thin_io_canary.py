import importlib.util
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "experiments/thin-guard/thin-io-canary.py"
SPEC = importlib.util.spec_from_file_location("thin_io_canary", SCRIPT)
canary = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(canary)


class ThinIoCanaryTests(unittest.TestCase):
    def test_record_round_trip_and_corruption(self):
        data = canary.record(42)
        self.assertEqual(len(data), 4096)
        self.assertEqual(canary.decode(data), 42)
        damaged = bytearray(data)
        damaged[100] ^= 1
        self.assertIsNone(canary.decode(bytes(damaged)))

    def test_bounded_write_and_verify(self):
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "device"
            journal = pathlib.Path(directory) / "journal"
            target.write_bytes(b"\0" * 8192)
            subprocess.run(
                [
                    "python3", str(SCRIPT), "write", str(target), str(journal),
                    "--count", "5", "--interval", "0", "--allow-regular-test",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            result = subprocess.run(
                [
                    "python3", str(SCRIPT), "verify", str(target), str(journal),
                    "--allow-regular-test",
                ],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            )
            self.assertIn("CANARY_VERIFY=PASS storage=5 journal=5", result.stdout)

    def test_regular_file_refused_without_test_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "device"
            journal = pathlib.Path(directory) / "journal"
            target.write_bytes(b"\0" * 8192)
            result = subprocess.run(
                ["python3", str(SCRIPT), "verify", str(target), str(journal)],
                text=True,
                stderr=subprocess.PIPE,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not a block device", result.stderr)


if __name__ == "__main__":
    unittest.main()
