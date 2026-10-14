"""Execute the actual publish shell block with an isolated fake gh.

No network or GitHub writes. Native Linux/bash required; Windows skips rather
than substituting a different shell's error semantics.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]


def publish_block():
    text = (ROOT / '.github/workflows/release.yml').read_text(encoding='utf-8')
    marker = '      - name: Publish prerelease\n'
    if text.count(marker) != 1:
        raise AssertionError('expected one actual publish step')
    step = text.split(marker, 1)[1]
    if step.count('        run: |\n') != 1:
        raise AssertionError('publish run block missing/ambiguous')
    lines = step.split('        run: |\n', 1)[1].splitlines()
    body = []
    for line in lines:
        if line.strip() and not line.startswith('          '):
            break
        body.append(line[10:] if line.startswith('          ') else '')
    if not body:
        raise AssertionError('empty publish script')
    return '\n'.join(body) + '\n'


@unittest.skipUnless(sys.platform.startswith('linux') and shutil.which('bash'),
                     'requires native Linux bash; no Windows shell substitution')
class ReleaseImmutabilityShellTests(unittest.TestCase):
    def run_publish(self, response='[[]]', api_code=0, create_code=0,
                    tag='v0.9.0-rc5.96-tg53'):
        with tempfile.TemporaryDirectory(prefix='release-immutability-') as directory:
            work = Path(directory)
            (work / 'bin').mkdir()
            (work / 'scripts').mkdir()
            (work / 'docs').mkdir()
            (work / 'dist').mkdir()
            shutil.copyfile(ROOT / 'scripts/assert-release-absent.py',
                            work / 'scripts/assert-release-absent.py')
            # Supply even the slash-derived notes path so a slash rejection
            # cannot pass merely because notes were missing before validation.
            name = tag.removeprefix('v0.9.0-') if tag.startswith('v0.9.0-') else tag.removeprefix('v')
            notes = work / ('docs/RELEASE-NOTES-' + name.upper() + '.md')
            notes.parent.mkdir(parents=True, exist_ok=True)
            notes.write_text('isolated fixture notes\n', encoding='utf-8')
            (work / 'docs/RELEASE-NOTES-RC5.96-TG53.md').write_text('fixture\n', encoding='utf-8')
            for name in ('dual.deb', 'thick.deb', 'SHA256SUMS'):
                (work / 'dist' / name).write_text('fixture only\n', encoding='utf-8')
            fake = work / 'bin/gh'
            fake.write_text('#!' + sys.executable + '\n' + textwrap.dedent('''\
                import json, os, sys
                args = sys.argv[1:]
                with open(os.environ['FAKE_GH_LOG'], 'a', encoding='utf-8') as log:
                    log.write(json.dumps(args) + '\\n')
                if args[:1] == ['api']:
                    sys.stdout.write(os.environ['FAKE_API_RESPONSE'])
                    raise SystemExit(int(os.environ['FAKE_API_CODE']))
                if args[:2] == ['release', 'create']:
                    raise SystemExit(int(os.environ['FAKE_CREATE_CODE']))
                raise SystemExit(97)
                '''), encoding='utf-8')
            fake.chmod(0o700)
            # Use the test interpreter for the helper, not an unrelated system
            # Python; gh is always our fake at the first PATH entry.
            (work / 'bin/python3').symlink_to(sys.executable)
            env = dict(os.environ, PATH=str(work / 'bin') + os.pathsep + os.environ.get('PATH', ''),
                       GITHUB_REF_NAME=tag, GITHUB_REF_TYPE='tag', GITHUB_REPOSITORY='fixture/repository',
                       GITHUB_SHA='a' * 40, GH_TOKEN='fixture-no-authority',
                       FAKE_GH_LOG=str(work / 'calls.jsonl'), FAKE_API_RESPONSE=response,
                       FAKE_API_CODE=str(api_code), FAKE_CREATE_CODE=str(create_code))
            result = subprocess.run([shutil.which('bash'), '--noprofile', '--norc', '-e', '-o', 'pipefail', '-c', publish_block()],
                                    cwd=work, env=env, capture_output=True, text=True, timeout=20)
            log = work / 'calls.jsonl'
            calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            return result, calls

    def assert_no_create(self, result, calls):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([c for c in calls if c[:1] == ['release']], [])

    def test_api_error_with_valid_absent_stdout_cannot_create(self):
        self.assert_no_create(*self.run_publish(api_code=1))

    def test_existing_draft_on_later_page_cannot_create(self):
        self.assert_no_create(*self.run_publish(json.dumps([[], [{'tag_name': 'v0.9.0-rc5.96-tg53', 'draft': True}]])))

    def test_malformed_success_response_cannot_create(self):
        self.assert_no_create(*self.run_publish('{"message":"Bad credentials"}'))

    def test_absent_creates_exactly_once(self):
        result, calls = self.run_publish()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], ['api', '--paginate', '--slurp', 'repos/fixture/repository/releases'])
        self.assertEqual(calls[1][:3], ['release', 'create', 'v0.9.0-rc5.96-tg53'])
        self.assertIn('--verify-tag', calls[1])
        self.assertNotIn('--clobber', calls[1])

    def test_concurrent_create_failure_has_no_retry_or_fallback(self):
        result, calls = self.run_publish(create_code=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1][:2], ['release', 'create'])

    def test_actual_slash_tag_is_not_rewritten_to_another_tag(self):
        result, calls = self.run_publish(tag='vprefix/v0.9.0-rc5.96-tg53')
        self.assert_no_create(result, calls)
        self.assertIn('invalid release tag', result.stderr)


if __name__ == '__main__':
    unittest.main()
