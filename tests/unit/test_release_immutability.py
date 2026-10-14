import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('release_absent', ROOT / 'scripts/assert-release-absent.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class ReleaseImmutabilityTests(unittest.TestCase):
    def test_absent_on_successful_empty_page(self):
        guard.assert_absent('[[]]', 'v0.9.0-rc5.96-tg53')

    def test_existing_release_or_draft_on_later_page_refused(self):
        for draft in (False, True):
            with self.subTest(draft=draft), self.assertRaises(ValueError):
                guard.assert_absent(json.dumps([[], [{'tag_name': 'v0.9.0-rc5.96-tg53', 'draft': draft}]]),
                                    'v0.9.0-rc5.96-tg53')

    def test_older_release_does_not_block_new_tag(self):
        guard.assert_absent('[[{"tag_name":"v0.9.0-rc5.89-tg53"}]]', 'v0.9.0-rc5.96-tg53')

    def test_failed_or_malformed_response_refused(self):
        for response in ('', 'not-json', '[]', '{}', '[{}]', '[[null]]', '[[{}]]',
                         '[[{"tag_name":null}]]', '{"message":"Bad credentials"}',
                         '[[{"tag_name":"older","draft":NaN}]]',
                         '[[{"tag_name":"one","tag_name":"two"}]]',
                         '[[{"tag_name":"one"}],[{"tag_name":"one"}]]'):
            with self.subTest(response=response), self.assertRaises(ValueError):
                guard.assert_absent(response, 'v0.9.0-rc5.96-tg53')

    def test_invalid_tag_refused(self):
        for tag in ('', 'main', 'v1\n', 'v1/other'):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                guard.assert_absent('[[]]', tag)

    def test_workflow_has_no_overwrite_fallback(self):
        workflow = (ROOT / '.github/workflows/release.yml').read_text(encoding='utf-8')
        for forbidden in ('--clobber', 'gh release upload', 'gh release edit', 'gh release delete'):
            self.assertNotIn(forbidden, workflow)
        self.assertIn('gh api --paginate --slurp', workflow)
        self.assertLess(workflow.index('scripts/assert-release-absent.py'), workflow.index('gh release create'))
        self.assertIn('--verify-tag', workflow)
        self.assertEqual(workflow.count('gh release create'), 1)
        self.assertIn('tag_name="$GITHUB_REF_NAME"', workflow)
        self.assertNotIn('${GITHUB_REF_NAME##*/}', workflow)


if __name__ == '__main__':
    unittest.main()
