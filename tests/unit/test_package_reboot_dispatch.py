"""Execute the complete reboot gate in an explicitly mocked installed layout.

This proves consumer ordering/FD transport, not a real host reboot or SAN health.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / 'usr/libexec/pve-sharedlvmthin'


@unittest.skipUnless(os.name == 'posix' and getattr(os, 'geteuid', lambda: -1)() == 0,
                     'Linux root executor transport')
class CompleteRebootDispatchTests(unittest.TestCase):
    def test_health_settlement_replay_and_other_failure_preservation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / 'usr/libexec/pve-sharedlvmthin'
            runtime.mkdir(parents=True)
            locks, commands = root / 'locks', root / 'commands'
            locks.mkdir(); commands.mkdir()
            def script(path, text):
                path.write_text(text); path.chmod(0o755)
            helper = runtime / 'sharedlvmthin-package-preservation'
            helper.write_bytes((RUNTIME / helper.name).read_bytes())
            (runtime / 'sharedlvmthin_package_executor_fence.py').write_text(
                (RUNTIME / 'sharedlvmthin_package_executor_fence.py').read_text().replace(
                    'directory="/run/lock"', 'directory=' + repr(str(locks))))
            (runtime / 'sharedlvmthin-recovery-check').write_text('# fixture\n')
            (runtime / 'sharedlvmthin_package_preservation.py').write_text(
                "def collect_snapshot(*args): return {'scopes':(('scope','vm-901-disk-0'),)}\n"
                "def require_unchanged(a,b): assert a==b\n")
            (runtime / 'sharedlvmthin-package-profile-gate').write_text('# fixture\n')
            flavor = root / 'usr/share/pve-sharedlvmthin/package-flavor'
            flavor.parent.mkdir(parents=True); flavor.write_text('dual\n')
            for relative in ('usr/sbin/sharedlvmthin-migrate-bridge',
                             'usr/libexec/pve-sharedlvmthin/sharedlvmthin-thin-guardd',
                             'lib/systemd/system/pve-sharedlvmthin-thin-guard.service'):
                path = root / relative; path.parent.mkdir(parents=True, exist_ok=True); path.touch()
            boot = root / 'boot'; boot.write_text('22222222-2222-2222-2222-222222222222\n')
            gate = (ROOT / 'experiments/thick-generations/package-post-reboot-gate.sh').read_text()
            gate = gate.replace('/usr/', str(root / 'usr') + '/').replace('/lib/systemd/',
                str(root / 'lib/systemd') + '/').replace('/proc/sys/kernel/random/boot_id', str(boot))
            gate = gate.replace('set -euo pipefail', 'set -euo pipefail\nPATH=' + str(commands) + ':/usr/bin:/bin')
            script(runtime / 'sharedlvmthin-package-post-reboot-gate', gate)
            script(commands / 'hostname', '#!/bin/sh\necho fixture-node\n')
            script(commands / 'dpkg', '#!/bin/sh\nexit 0\n')
            script(commands / 'dpkg-query', '#!/bin/sh\ncase "$*" in\n'
                '*pve-sharedlvmthin-thick*) exit 1;;\n*Status-Abbrev*) printf ii;;\n'
                '*Version*) printf "0.9.0~rc5.93~tg53";;\nesac\n')
            script(commands / 'systemctl', '#!/bin/sh\nexit 0\n')
            script(commands / 'sharedlvmthin', '#!/bin/bash\ncase "$1" in\n'
                'help) echo "thick-recover-prepare <storage-id> <volume>"; '
                'echo "thin-adopt-owner-model <storage-id> <volume> ALL-NODES-INACTIVE"; exit 64;;\n'
                'doctor) echo "FAIL: 0"; echo "RESULT: PASS WITH WARNINGS";;\n'
                'update-policy) shift; exec /usr/bin/python3 ' + str(helper) + ' --update-policy "$@";;\n'
                'compat-check|upgrade-check) test -n "$SLT_PACKAGE_FENCE_FD";;\nesac\n')
            settlement = root / 'settlement'
            script(runtime / 'sharedlvmthin-update-policy', '#!/usr/bin/python3\n'
                'import os,pathlib\nfrom sharedlvmthin_package_executor_fence import read_sealed_context\n'
                "read_sealed_context(int(os.environ['SLT_PACKAGE_FENCE_FD']),pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip())\n"
                'pathlib.Path(' + repr(str(settlement)) + ').write_text("settled")\n')
            health_count, fail = root / 'health-count', root / 'fail'
            script(runtime / 'sharedlvmthin-health-json', '#!/usr/bin/python3\n'
                'import ast,json,os,pathlib,re\n'
                'from sharedlvmthin_package_executor_fence import read_sealed_context\n'
                "ctx=read_sealed_context(int(os.environ['SLT_PACKAGE_FENCE_FD']),pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip())\n"
                "assert ['scope','vm-901-disk-0'] in ctx['scopes']\n"
                'tree=ast.parse(pathlib.Path(' + repr(str(RUNTIME / 'sharedlvmthin-health-json')) + ').read_text())\n'
                "node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='evaluate_thick_anchor_references')\n"
                "ns={'re':re,'pve_volume_reference_files':lambda _:['config']}\n"
                "exec(compile(ast.Module(body=[node],type_ignores=[]),'fixture-health','exec'),ns)\n"
                "item=ns['evaluate_thick_anchor_references']('scope',[{'volume':'vm-901-disk-0','phase':'LAZY_ACTIVE'}],active_package_proof=lambda *_:True)[0]\n"
                "assert item['status']=='WARN' and item['safe_for_mutation'] is False\n"
                'p=pathlib.Path(' + repr(str(health_count)) + '); p.write_text(str(int(p.read_text())+1) if p.exists() else "1")\n'
                'bad=pathlib.Path(' + repr(str(fail)) + ').exists()\n'
                "print(json.dumps({'result':'FAIL' if bad else 'WARN','checks':[{'status':'FAIL' if bad else 'WARN'}],"
                "'platform':{'package_flavor':'dual','plugin_package':'pve-sharedlvmthin','plugin_version':'0.9.0~rc5.93~tg53'},'storages':[]}))\n")
            args = [sys.executable, str(helper), '--post-reboot', '--expect-host', 'fixture-node',
                    '--expect-profile', 'dual', '--expect-version', '0.9.0~rc5.93~tg53',
                    '--previous-boot-id', '11111111-1111-1111-1111-111111111111', '--settle-runtime']
            result = subprocess.run(args, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('POST_SETTLEMENT_RECHECK=PASS', result.stdout)
            self.assertEqual(health_count.read_text(), '2')
            self.assertTrue(settlement.exists())
            settlement.unlink(); fail.touch()
            result = subprocess.run(args, capture_output=True, text=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(settlement.exists())
            self.assertNotIn('RESULT=POST_REBOOT_PASS', result.stdout)
