"""Isolated inventory regressions: no live files, databases or settings writes."""
import json
import tempfile
import unittest
from pathlib import Path

from projects import PROJECT_REGISTRY, inventory


class ProjectInventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def write(self, project_id, **updates):
        relative = dict(PROJECT_REGISTRY)[project_id]
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        value = {'id': project_id, 'name': project_id, 'milestones': []}
        value.update(updates)
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def test_missing_scopes_are_explicit(self):
        result = inventory(self.root)
        self.assertEqual(result['status'], 'missing')
        self.assertEqual(result['expectedProjects'], 4)
        self.assertEqual(len(result['sources']), 4)

    def test_all_four_are_independently_collected(self):
        for project_id, _ in PROJECT_REGISTRY:
            self.write(project_id)
        result = inventory(self.root)
        self.assertEqual(result['status'], 'collected')
        self.assertEqual([p['id'] for p in result['projects']], [p[0] for p in PROJECT_REGISTRY])

    def test_corrupt_new_app_does_not_hide_bible(self):
        self.write('bible_app')
        path = self.write('shopping')
        path.write_text('{broken', encoding='utf-8')
        result = inventory(self.root)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual([p['id'] for p in result['projects']], ['bible_app'])
        self.assertEqual({s['id']: s['status'] for s in result['sources']}['shopping'], 'invalid')

    def test_wrong_identity_and_oversized_files_are_isolated(self):
        self.write('bible_app')
        self.write('magnifier', id='bible_app')
        self.write('greeting').write_text('x' * 65537)
        result = inventory(self.root)
        self.assertEqual([p['id'] for p in result['projects']], ['bible_app'])
        self.assertEqual(sum(s['status'] == 'invalid' for s in result['sources']), 2)

    def test_unknown_metrics_and_unproved_milestones_stay_unknown(self):
        self.write('bible_app', measurement={'admob': {'status': 'not_connected', 'revenue': 0},
                   'analytics': {'status': 'collected', 'activeUsers': 12, 'checkedAt': {}, 'period': 'today'}},
                   milestones=[{'id': 'release', 'status': 'verified', 'verifiedAt': {'fake': True}, 'evidence': 'file'}])
        app = inventory(self.root)['projects'][0]
        self.assertIsNone(app['measurement']['admob']['revenue'])
        self.assertIsNone(app['measurement']['analytics']['activeUsers'])
        self.assertEqual(app['milestones'][0]['status'], 'in_progress')

    def test_real_zero_needs_source_metadata(self):
        self.write('bible_app', measurement={'admob': {'status': 'collected', 'revenue': 0,
                   'checkedAt': '2026-10-14T10:00:00+09:00', 'period': '2026-10-14'}})
        self.assertEqual(inventory(self.root)['projects'][0]['measurement']['admob']['revenue'], 0)

    def test_secrets_and_unsafe_urls_never_enter_public_response(self):
        for url in ['https://127.0.0.1/', 'https://10.0.0.1/', 'https://user:pass@example.com/', 'http://example.com/']:
            self.write('bible_app', publicUrl=url, api_key='DO_NOT_ECHO')
            result = inventory(self.root)
            self.assertIsNone(result['projects'][0]['publicUrl'])
            self.assertNotIn('DO_NOT_ECHO', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
