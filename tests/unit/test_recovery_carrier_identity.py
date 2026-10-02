"""Real carrier identity/settlement logic with in-memory package and file probes.

No package installation, host mutation, or runtime qualification is performed.
These fixtures do not claim native filesystem/fsync qualification.
"""
import copy
import hashlib
import stat
import types
import unittest
from unittest import mock

import test_freeze_unqualified_settlement as existing


class RecoveryCarrierIdentityTests(unittest.TestCase):
    row = staticmethod(existing.ClosedSettlementTests.row)
    write = existing.ClosedSettlementTests.write
    create = existing.ClosedSettlementTests.create
    mark = existing.ClosedSettlementTests.mark
    prepare = existing.ClosedSettlementTests.prepare
    install = existing.ClosedSettlementTests.install
    replacement = existing.ClosedSettlementTests.replacement
    run_command = existing.ClosedSettlementTests.run_command
    unlisted = existing.ClosedSettlementTests.unlisted
    command = existing.ClosedSettlementTests.command
    assert_closed = existing.ClosedSettlementTests.assert_closed

    def setUp(self):
        existing.ClosedSettlementTests.setUp(self)
        self.args.recovery_code_deb = '/fixture/recovery.deb'
        self.args.recovery_code_sha256 = 'f' * 64
        self.members = {
            'control': b'Package: pve-sharedlvmthin-thick\nVersion: 99\nArchitecture: all\n',
            'sharedlvmthin-candidate-update-policy': b'recovery helper fixture',
            'sharedlvmthin_update_policy.py': b'recovery core fixture',
        }
        self.digests = {
            self.args.recovery_code_deb: 'f' * 64,
            str(self.m.__file__): hashlib.sha256(self.members['sharedlvmthin-candidate-update-policy']).hexdigest(),
            str(self.m.LIB): hashlib.sha256(self.members['sharedlvmthin_update_policy.py']).hexdigest(),
        }
        self.file_probe = self.stack.enter_context(mock.patch.object(self.m, 'recovery_file_digest',
            side_effect=lambda path, **_: self.digests[str(path)]))
        self.archive_probe = self.stack.enter_context(mock.patch.object(self.m, 'recovery_carrier_members',
            side_effect=lambda _: copy.deepcopy(self.members)))
        self.stack.enter_context(mock.patch.object(self.m, 'command_finalize_runtime',
            side_effect=AssertionError('carrier must not finalize runtime')))
        self.stack.enter_context(mock.patch.object(self.m, 'command_qualify_runtime',
            side_effect=AssertionError('carrier must not qualify runtime')))

    def test_separate_carrier_target_and_installed_manifest_identities(self):
        # A carrier manifest, even when claiming qualification, is not consumed.
        self.members['pve-qualified-tuples.json'] = b'{"qualified":true}'
        identity = self.m.recovery_executor_identity(self.args)
        self.assertEqual(identity['carrier_version'], '99')
        self.assertEqual(self.args.version, '1')
        self.command()
        latch = self.store[self.m.RUNTIME_QUALIFICATION]
        self.assertEqual(latch['recovery_executor'], identity)
        self.assertEqual(latch['artifact_sha256'], 'b' * 64)
        self.assertEqual(latch['runtime_manifest_sha256'], self.core.digest(self.manifest))
        self.assert_closed()

    def test_missing_optional_carrier_is_probe_free(self):
        self.args.recovery_code_deb = None
        self.args.recovery_code_sha256 = None
        self.assertIsNone(self.m.recovery_executor_identity(self.args))
        self.file_probe.assert_not_called()
        self.archive_probe.assert_not_called()

    def test_incomplete_or_malformed_approval_is_probe_free(self):
        for path, digest in ((None, 'f' * 64), ('/fixture/a', None),
                             ('/fixture/a', 'F' * 64), ('/fixture/a', 'short')):
            with self.subTest(path=path, digest=digest):
                self.args.recovery_code_deb, self.args.recovery_code_sha256 = path, digest
                with self.assertRaises(RuntimeError):
                    self.m.recovery_executor_identity(self.args)
        self.file_probe.assert_not_called()

    def test_swapping_target_and_carrier_hashes_cannot_settle(self):
        original = copy.deepcopy(self.store)
        self.args.recovery_code_sha256 = self.args.sha256
        with self.assertRaisesRegex(RuntimeError, 'SHA-256 mismatch'):
            self.command()
        self.assertEqual(self.store, original)
        self.args.recovery_code_sha256 = 'f' * 64
        self.args.sha256 = 'f' * 64
        with self.assertRaises(RuntimeError):
            self.command()
        self.assertEqual(self.store, original)

    def test_executing_helper_and_core_must_match_carrier_members(self):
        for member in ('sharedlvmthin-candidate-update-policy', 'sharedlvmthin_update_policy.py'):
            with self.subTest(member=member):
                original = self.members[member]
                self.members[member] = b'foreign code'
                with self.assertRaisesRegex(RuntimeError, 'differs from its approved carrier'):
                    self.m.recovery_executor_identity(self.args)
                self.members[member] = original

    def test_wrong_profile_architecture_or_duplicate_control_refused(self):
        for control in (
            b'Package: pve-sharedlvmthin\nVersion: 99\nArchitecture: all\n',
            b'Package: pve-sharedlvmthin-thick\nVersion: 99\nArchitecture: amd64\n',
            self.members['control'] + b'Version: 100\n',
        ):
            with self.subTest(control=control), self.assertRaises(RuntimeError):
                self.members['control'] = control
                self.m.recovery_executor_identity(self.args)

    def test_second_digest_read_detects_each_identity_drift(self):
        for changed in self.digests:
            counts = {}
            def read(path, **_):
                key = str(path)
                counts[key] = counts.get(key, 0) + 1
                return 'e' * 64 if key == changed and counts[key] > 1 else self.digests[key]
            with self.subTest(changed=changed), mock.patch.object(self.m, 'recovery_file_digest', side_effect=read):
                with self.assertRaisesRegex(RuntimeError, 'changed during identity inspection'):
                    self.m.recovery_executor_identity(self.args)

    def test_closed_replay_preserves_successor_and_refuses_new_carrier(self):
        self.command()
        successor = copy.deepcopy(self.store)
        self.effects.clear()
        self.command()
        self.assertEqual(self.store, successor)
        self.assertEqual(self.effects, [])
        self.args.recovery_code_sha256 = 'e' * 64
        self.digests[self.args.recovery_code_deb] = 'e' * 64
        with self.assertRaises(RuntimeError):
            self.command()
        self.assertEqual(self.store, successor)
        self.assert_closed()

    def test_closed_latch_lost_ack_replays_exact_executor(self):
        def lost_ack(path, value):
            self.create(path, value)
            raise RuntimeError('lost ACK')
        with mock.patch.object(self.m, 'create_json', side_effect=lost_ack):
            with self.assertRaisesRegex(RuntimeError, 'lost ACK'):
                self.command()
        identity = copy.deepcopy(self.store[self.m.RUNTIME_QUALIFICATION]['recovery_executor'])
        self.command()
        self.assertEqual(self.store[self.m.RUNTIME_QUALIFICATION]['recovery_executor'], identity)
        self.assert_closed()

    def test_unsafe_file_identity_refused_before_open(self):
        # Exercise the actual filesystem validator with synthetic lstat evidence.
        # Load a separate module so only syscall evidence, not the validator, is mocked.
        from test_freeze_package_settlement import load, ROOT
        import sys
        imports = {} if sys.platform != 'win32' else {'fcntl': types.ModuleType('fcntl')}
        with mock.patch.dict(sys.modules, imports):
            module = load('carrier_file_safety_test', ROOT / 'usr/libexec/pve-sharedlvmthin/sharedlvmthin-update-policy')
        safe = dict(st_mode=stat.S_IFREG | 0o400, st_uid=0, st_nlink=1, st_size=10)
        for changed in ({'st_mode': stat.S_IFLNK | 0o777}, {'st_nlink': 2},
                        {'st_uid': 1000}, {'st_mode': stat.S_IFREG | 0o620}):
            entry = types.SimpleNamespace(**(safe | changed))
            path = mock.Mock(parts=('/', 'fixture'), parents=())
            path.is_absolute.return_value = True
            path.lstat.return_value = entry
            with self.subTest(changed=changed), mock.patch.object(module.pathlib, 'Path', return_value=path), \
                    mock.patch.object(module.os, 'name', 'posix'), mock.patch.object(module.os, 'open') as opened:
                with self.assertRaisesRegex(RuntimeError, 'unsafe recovery code file'):
                    module.recovery_file_digest('/fixture', limit=100)
                opened.assert_not_called()


if __name__ == '__main__':
    unittest.main()
