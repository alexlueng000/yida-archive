import tempfile
import threading
import unittest
from unittest.mock import patch, Mock

from test_ai_classification import files_for, decision, SECTIONS
from yida_archive import ai_classify as ai, classify, document_text
from yida_archive.config import Config
from tools.reclassify_pending import result_snapshot


class SpeedTests(unittest.TestCase):
    def test_parallel_requests_share_call_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config({'storage': {'work_dir': tmp}, 'ai': {
                'enabled': True, 'workers': 3, 'max_calls': 2}})
            files = [files_for('资料%d.docx' % i)[0] for i in range(3)]
            classify.classify(files, cfg)
            barrier = threading.Barrier(2)
            def post(*args, **kwargs):
                barrier.wait(timeout=5)
                response = Mock(status_code=200)
                import json
                response.json.return_value = {'choices': [{'finish_reason': 'stop',
                    'message': {'content': json.dumps(decision(status='unknown'))}}]}
                return response
            with patch.object(ai, 'credentials', return_value=('key', 'https://example.invalid', 'fake')), \
                 patch.object(document_text, 'extract', return_value=SECTIONS), \
                 patch.object(ai.requests, 'post', side_effect=post) as request:
                ai.apply(files, cfg, lambda f: b'body')
            self.assertEqual(request.call_count, 2)
            self.assertEqual(sum(f.ai_result['status'] == 'failed' for f in files), 1)
            self.assertTrue(any('调用次数' in f.review_note for f in files))

    def test_invalid_evidence_is_cached_but_never_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config({'storage': {'work_dir': tmp}, 'ai': {'enabled': True}})
            result = decision()
            result['evidence'][0]['quote'] = '正文中不存在的证据'
            with patch.object(ai, 'credentials', return_value=('key', 'https://example.invalid', 'fake')), \
                 patch.object(document_text, 'extract', return_value=SECTIONS), \
                 patch.object(ai.Client, 'classify', return_value=(result, {})) as request:
                for _ in range(2):
                    files = files_for()
                    classify.classify(files, cfg)
                    ai.apply(files, cfg, lambda f: b'body')
                    self.assertEqual(files[0].ai_result['status'], 'failed')
                    self.assertFalse(files[0].type_name)
                self.assertEqual(request.call_count, 1)

    def test_snapshot_ignores_cache_hits_but_detects_new_reason(self):
        files = files_for()
        before = result_snapshot(files)
        files[0].cache_path = 'another-cache'
        files[0].ai_result['cached'] = True
        self.assertEqual(before, result_snapshot(files))
        files[0].review_note = '新的失败原因'
        self.assertNotEqual(before, result_snapshot(files))
