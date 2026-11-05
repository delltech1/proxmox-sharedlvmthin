import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('source_privacy', ROOT / 'scripts/public-source-privacy-scan.py')
SCANNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCANNER)


class PublicSourcePrivacyTests(unittest.TestCase):
    def scan(self, relative, content):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = root / relative
            item.parent.mkdir(parents=True, exist_ok=True)
            item.write_bytes(content)
            output = io.StringIO()
            with mock.patch.object(SCANNER, 'ROOT', root), \
                    mock.patch.object(SCANNER, 'tracked_files', return_value=[item]), \
                    contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
                result = SCANNER.main()
            return result, output.getvalue()

    def test_tests_are_scanned_for_private_identifiers(self):
        for content in (b'10' + b'.2.3.4', b'DEV-' + b'PRX-TEST', b'pass' + b'word="example-secret"'):
            with self.subTest(content=content):
                result, output = self.scan('tests/unit/ordinary_test.py', content)
                self.assertEqual(result, 1, output)

    def test_generic_test_nodes_and_scanner_markers_are_allowed(self):
        self.assertEqual(self.scan('tests/unit/ordinary_test.py', b'NODE01')[0], 0)
        self.assertEqual(self.scan('tests/unit/ordinary_test.py', b'PVE' + b'01')[0], 1)
        fixture = b'-----BEGIN OPENSSH ' + b'PRIVATE KEY-----'
        self.assertEqual(self.scan('tests/unit/test_package_privacy_scan.py', fixture)[0], 0)
        self.assertEqual(self.scan('tests/unit/test_package_privacy_scan.py', b'10' + b'.2.3.4')[0], 1)
        self.assertEqual(self.scan('tests/unit/test_package_privacy_scan.py', b'pass' + b'word="actual-secret"')[0], 1)

    def test_specific_lab_host_is_rejected_in_test_and_model_paths(self):
        marker = b'DEV-' + b'PRX-TEST'
        for relative in (
                'tests/unit/ordinary_test.py',
                'experiments/thick-generations/layout-migration-exact-integration-model.py',
                'experiments/thick-generations/layout-migration-exact-transport.py'):
            with self.subTest(path=relative):
                result, output = self.scan(relative, marker)
                self.assertEqual(result, 1, output)
                self.assertIn('lab hostname', output)
        for relative in (
                'experiments/thick-generations/layout-migration-exact-integration-model.py',
                'experiments/thick-generations/layout-migration-exact-transport.py'):
            with self.subTest(lab_host=relative):
                self.assertEqual(self.scan(relative, b'PVE' + b'01')[0], 1)

    def test_binary_or_oversized_files_do_not_silently_pass(self):
        self.assertEqual(self.scan('tests/unit/unknown.bin', b'\x00data')[0], 1)
        with mock.patch.object(SCANNER, 'TEXT_LIMIT', 3):
            self.assertEqual(self.scan('tests/unit/oversized.txt', b'1234')[0], 1)

    def test_scanner_implementations_are_scanned_for_secrets(self):
        markers = (b'10' + b'.2.3.4', b'DEV-' + b'PRX-TEST',
                   b'pass' + b'word="actual-secret"',
                   b'-----BEGIN OPENSSH ' + b'PRIVATE KEY-----')
        for relative in ('scripts/package-privacy-scan.py', 'scripts/public-source-privacy-scan.py'):
            source = (ROOT / relative).read_bytes()
            with self.subTest(path=relative, marker='own source'):
                result, output = self.scan(relative, source)
                self.assertEqual(result, 0, output)
            for marker in markers:
                with self.subTest(path=relative, marker=marker):
                    result, output = self.scan(relative, source + b'\n' + marker + b'\n')
                    self.assertEqual(result, 1, output)
