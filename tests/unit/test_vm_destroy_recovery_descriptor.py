import importlib.machinery
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / 'usr/libexec/pve-sharedlvmthin/sharedlvmthin-vm-destroy-recovery'
loader = importlib.machinery.SourceFileLoader('destroy_recovery_descriptor', str(HELPER))
spec = importlib.util.spec_from_loader(loader.name, loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
TXID = 'a' * 32


def descriptor():
    context = {'schema': mod.CORE_SCHEMA, 'receipt': {
        'schema': 1, 'txid': TXID, 'vmid': 123, 'config': {'digest': 'b' * 40},
        'runtime': {'status': 'QUALIFIED', 'boot_id': 'exact-boot'},
        'plan': {'authority': 'NONE', 'objects': ['exact-identity']}}}
    return {'schema': mod.SCHEMA, 'authority': 'NONE', 'txid': TXID,
            'context': context, 'context_sha256': mod.digest(context)}


def raw(value=None):
    return mod.canonical(value if value is not None else descriptor()) + b'\n'


class DescriptorModelTests(unittest.TestCase):
    def test_exact_canonical_context_roundtrip(self):
        self.assertEqual(mod.decode(raw(), TXID), descriptor())

    def test_strict_bytes_reject_noncanonical_duplicate_and_oversized(self):
        for data in (b'', raw()[:-1], b' ' + raw(), raw() + b'\n', b'x' * (mod.LIMIT + 1),
                     b'{"txid":"a","txid":"a"}\n', b'{"x":NaN}\n'):
            with self.subTest(data=data[:64]), self.assertRaises(mod.Refusal):
                mod.decode(data, TXID)

    def test_exact_descriptor_schema_hash_and_identity(self):
        for change in ({'authority': 'MUTATE'}, {'schema': 'other'}, {'txid': 'b' * 32},
                       {'extra': True}, {'context_sha256': '0' * 64}, {'context': {}}):
            with self.subTest(change=change), self.assertRaises(mod.Refusal):
                mod.decode(raw({**descriptor(), **change}), TXID)

    def test_full_core_required_no_evidence_or_partial_context(self):
        for key in ('schema', 'txid', 'vmid', 'config', 'runtime', 'plan'):
            value = descriptor()
            del value['context']['receipt'][key]
            value['context_sha256'] = mod.digest(value['context'])
            with self.subTest(key=key), self.assertRaises(mod.Refusal):
                mod.decode(raw(value), TXID)
        value = descriptor()
        value['context']['receipt']['evidence'] = {}
        value['context_sha256'] = mod.digest(value['context'])
        with self.assertRaises(mod.Refusal):
            mod.decode(raw(value), TXID)

    def test_typed_core_identity(self):
        for key, replacement in (('schema', True), ('schema', '1'), ('schema', 2),
                                 ('vmid', True), ('vmid', 123.0), ('vmid', '123'),
                                 ('vmid', 0), ('vmid', 1000000000), ('txid', 'b' * 32),
                                 ('config', {}), ('runtime', []), ('plan', None)):
            value = descriptor()
            value['context']['receipt'][key] = replacement
            value['context_sha256'] = mod.digest(value['context'])
            with self.subTest(key=key, replacement=replacement), self.assertRaises(mod.Refusal):
                mod.decode(raw(value), TXID)

    def test_nonroot_cli_refused_before_any_operation(self):
        with mock.patch.object(mod.os, 'geteuid', return_value=1000, create=True):
            with self.assertRaisesRegex(mod.Refusal, 'root required'):
                mod.main(['_read', '--txid', TXID])

    def test_no_public_exposure_or_storage_executor(self):
        source = HELPER.read_text(encoding='utf-8')
        wrapper = (ROOT / 'usr/sbin/sharedlvmthin').read_text(encoding='utf-8')
        self.assertNotIn('sharedlvmthin-vm-destroy-recovery', wrapper)
        for token in ('subprocess', 'os.system(', 'os.exec', 'os.unlink(', 'os.rename(', 'os.replace('):
            self.assertNotIn(token, source)
        self.assertIn("choices=['_create', '_read']", source)
        self.assertNotIn("add_argument('--base'", source)


@unittest.skipUnless(os.name == 'posix', 'requires real POSIX dirfd/flock/fsync semantics')
class DescriptorFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / 'recovery'
        self.base.mkdir(mode=0o700)
        self.path = self.base / (TXID + '.json')
        self.store = mod.Store(self.base, expected_uid=os.geteuid())

    def test_create_and_durable_read_preserve_exact_descriptor(self):
        result = self.store.create(TXID, raw())
        self.assertIs(result['durable'], True)
        self.assertEqual(result['authority'], 'NONE')
        self.assertEqual(result['descriptor_sha256'], mod.digest(descriptor()))
        self.assertEqual(self.path.read_bytes(), raw())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        before = self.path.stat()
        self.assertEqual(self.store.read(TXID), result)
        self.assertEqual(mod.signature(before), mod.signature(self.path.stat()))

    def test_existing_exact_or_empty_file_never_adopted(self):
        self.store.create(TXID, raw())
        with self.assertRaises(FileExistsError):
            self.store.create(TXID, raw())
        other_id = 'c' * 32
        (self.base / (other_id + '.json')).touch(mode=0o600)
        other = descriptor()
        other['txid'] = other['context']['receipt']['txid'] = other_id
        other['context_sha256'] = mod.digest(other['context'])
        with self.assertRaises(FileExistsError):
            self.store.create(other_id, raw(other))

    def test_invalid_payload_never_creates_descriptor(self):
        with self.assertRaises(mod.Refusal):
            self.store.create(TXID, raw({**descriptor(), 'authority': 'MUTATE'}))
        self.assertEqual(list(self.base.iterdir()), [])

    def test_missing_root_is_never_created(self):
        missing = self.base / 'missing'
        with self.assertRaises(FileNotFoundError):
            mod.Store(missing, expected_uid=os.geteuid()).create(TXID, raw())
        self.assertFalse(missing.exists())

    def test_fsync_file_directory_parent_order(self):
        original = os.fsync
        seen = []
        base_identity = (self.base.stat().st_dev, self.base.stat().st_ino)
        parent_identity = (self.base.parent.stat().st_dev, self.base.parent.stat().st_ino)
        def track(fd):
            info = os.fstat(fd)
            identity = (info.st_dev, info.st_ino)
            seen.append('base' if identity == base_identity else 'parent' if identity == parent_identity else 'file')
            return original(fd)
        with mock.patch.object(mod.os, 'fsync', side_effect=track):
            self.store.create(TXID, raw())
        self.assertEqual(seen[:4], ['parent', 'file', 'base', 'parent'])
        self.assertEqual(seen[4:7], ['file', 'base', 'parent'])
        seen.clear()
        with mock.patch.object(mod.os, 'fsync', side_effect=track):
            self.store.read(TXID)
        self.assertEqual(seen, ['file', 'base', 'parent'])

    def test_each_create_crash_prefix_blocks_recreation(self):
        for point in ('after_create', 'after_file_fsync', 'after_directory_fsync'):
            with self.subTest(point=point):
                base = self.base / point
                base.mkdir(mode=0o700)
                def fault(at):
                    if at == point:
                        raise RuntimeError('crash')
                store = mod.Store(base, expected_uid=os.geteuid(), fault=fault)
                with self.assertRaises(RuntimeError):
                    store.create(TXID, raw())
                store.fault = lambda _: None
                self.assertTrue((base / (TXID + '.json')).exists())
                with self.assertRaises(FileExistsError):
                    store.create(TXID, raw())
                if point == 'after_create':
                    with self.assertRaises(mod.Refusal):
                        store.read(TXID)
                else:
                    self.assertEqual(store.read(TXID)['descriptor'], descriptor())

    def test_partial_write_remains_poisoned(self):
        original = os.write
        calls = []
        def partial(fd, data):
            if calls:
                raise OSError('injected short-write failure')
            calls.append(True)
            return original(fd, data[:13])
        with mock.patch.object(mod.os, 'write', side_effect=partial):
            with self.assertRaises(OSError):
                self.store.create(TXID, raw())
        self.assertEqual(self.path.stat().st_size, 13)
        with self.assertRaises(mod.Refusal):
            self.store.read(TXID)
        with self.assertRaises(FileExistsError):
            self.store.create(TXID, raw())

    def test_symlink_and_hardlink_descriptor_refused(self):
        target = self.base.parent / 'outside'
        target.write_bytes(raw())
        target.chmod(0o600)
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            self.store.read(TXID)
        with self.assertRaises(FileExistsError):
            self.store.create(TXID, raw())
        self.path.unlink()  # disposable test fixture only
        os.link(target, self.path)
        with self.assertRaisesRegex(mod.Refusal, 'unsafe descriptor inode'):
            self.store.read(TXID)
        self.assertEqual(target.read_bytes(), raw())

    def test_special_file_refused_without_blocking(self):
        os.mkfifo(self.path, 0o600)
        with self.assertRaisesRegex(mod.Refusal, 'unsafe descriptor inode'):
            self.store.read(TXID)

    def test_unsafe_base_and_ancestor_permissions(self):
        self.base.chmod(0o755)
        with self.assertRaises(mod.Refusal):
            self.store.create(TXID, raw())
        self.base.chmod(0o700)
        self.base.parent.chmod(0o777)
        with self.assertRaises(mod.Refusal):
            self.store.create(TXID, raw())
        self.base.parent.chmod(0o700)
        self.assertFalse(self.path.exists())

    def test_symlink_base_refused(self):
        link = self.base.parent / 'alias'
        link.symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(OSError):
            mod.Store(link, expected_uid=os.geteuid()).create(TXID, raw())

    def test_owner_and_record_mode_refused(self):
        with self.assertRaises(mod.Refusal):
            mod.Store(self.base, expected_uid=os.geteuid() + 1).create(TXID, raw())
        self.store.create(TXID, raw())
        self.path.chmod(0o644)
        with self.assertRaises(mod.Refusal):
            self.store.read(TXID)

    def test_tampered_digest_canonical_bytes_and_txid_refused(self):
        self.store.create(TXID, raw())
        for data in (raw({**descriptor(), 'context_sha256': '0' * 64}),
                     raw({**descriptor(), 'txid': 'b' * 32}), b' ' + raw()):
            self.path.write_bytes(data)
            with self.assertRaises(mod.Refusal):
                self.store.read(TXID)

    def test_created_inode_replacement_refused(self):
        saved = self.base / 'preserved'
        def fault(at):
            if at == 'after_file_fsync':
                self.path.rename(saved)
                self.path.write_bytes(raw())
                self.path.chmod(0o600)
        self.store.fault = fault
        with self.assertRaisesRegex(mod.Refusal, 'created descriptor replaced'):
            self.store.create(TXID, raw())
        self.assertEqual(saved.read_bytes(), raw())

    def test_namespace_replacement_during_create_refused(self):
        saved = self.base.with_name('preserved')
        def fault(at):
            if at == 'after_create':
                self.base.rename(saved)
                self.base.mkdir(mode=0o700)
        self.store.fault = fault
        with self.assertRaisesRegex(mod.Refusal, 'namespace changed'):
            self.store.create(TXID, raw())
        self.assertEqual(list(self.base.iterdir()), [])
        self.assertTrue((saved / (TXID + '.json')).exists())

    def test_read_changed_before_acknowledgement_refused(self):
        self.store.create(TXID, raw())
        def fault(at):
            if at == 'after_read':
                self.path.chmod(0o644)
        self.store.fault = fault
        with self.assertRaisesRegex(mod.Refusal, 'changed before acknowledgement'):
            self.store.read(TXID)

    def test_concurrent_creator_has_one_winner_and_no_adoption(self):
        start_r, start_w = os.pipe()
        result_r, result_w = os.pipe()
        children = []
        for _ in range(2):
            pid = os.fork()
            if pid == 0:
                os.close(start_w)
                os.close(result_r)
                os.read(start_r, 1)
                try:
                    self.store.create(TXID, raw())
                    result = b'Y'
                except (mod.Refusal, OSError):
                    result = b'N'
                os.write(result_w, result)
                os._exit(0)
            children.append(pid)
        os.close(start_r)
        os.close(result_w)
        os.write(start_w, b'xx')
        os.close(start_w)
        results = b''
        while len(results) < 2:
            chunk = os.read(result_r, 2 - len(results))
            if not chunk:
                break
            results += chunk
        os.close(result_r)
        for pid in children:
            self.assertEqual(os.waitpid(pid, 0)[1], 0)
        self.assertEqual(sorted(results), sorted(b'YN'))
        self.assertEqual(self.store.read(TXID)['descriptor'], descriptor())


if __name__ == '__main__':
    unittest.main()
