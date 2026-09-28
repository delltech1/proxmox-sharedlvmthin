import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "scripts/package-privacy-scan.py"
SPEC = importlib.util.spec_from_file_location("package_privacy_scan", PATH)
SCAN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCAN)


class PackagePrivacyScanTests(unittest.TestCase):
    def tree(self, data=b"safe package payload\n", name="payload"):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / name).write_bytes(data)
        return root

    def test_clean_payload_and_project_noreply_pass(self):
        root = self.tree(b"Maintainer: Community <noreply@users.noreply.github.com>\n")
        SCAN.scan_tree(root, "data")

    def test_all_private_ipv4_ranges_refuse(self):
        for value in (b"10." b"1.2.3", b"172." b"16.0.1",
                      b"172." b"31.255.254", b"192." b"168.50.10"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(SCAN.PrivacyError, "private IPv4"):
                    SCAN.scan_tree(self.tree(value), "data")

    def test_documentation_ranges_remain_usable(self):
        SCAN.scan_tree(self.tree(b"192.0.2.1 198.51.100.2 203.0.113.3"), "data")

    def test_credentials_keys_emails_and_operator_paths_refuse(self):
        cases = (
            (b"password=NeverShipThis", "credential-like"),
            (b"-----BEGIN OPENSSH PRIVATE KEY-----", "private marker"),
            (b"ssh-ed25519 AAAATEST operator@host", "private marker"),
            (b"C:\\Users\\operator\\artifact", "operator path"),
            (b"/root/private-build/file", "operator path"),
            (b"person@example.com", "non-project email"),
            (b"DEV-PRX-LAB", "private marker"),
        )
        for data, message in cases:
            with self.subTest(data=data):
                with self.assertRaisesRegex(SCAN.PrivacyError, message):
                    SCAN.scan_tree(self.tree(data), "data")

    def test_filename_is_scanned_too(self):
        with self.assertRaisesRegex(SCAN.PrivacyError, "operator path|private marker"):
            SCAN.scan_tree(self.tree(name="dev-prx-secret"), "data")


if __name__ == "__main__":
    unittest.main()
