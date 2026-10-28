"""Classify original directories through the actual background-task API."""
import json
import sys
import time
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_source_browser as browser_test
from web import server as web, source_classify


class SourceClassifyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = browser_test.SourceBrowserTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.request = self.fixture.request
        self.scope = self.fixture.base / '03-测试'
        self.scope.mkdir()
        (self.scope / '采购合同.pdf').write_bytes(b'contract')
        (self.scope / '采购合同-副本.pdf').write_bytes(b'contract')
        (self.scope / '未知附件.dat').write_bytes(b'unknown')
        for name, value in [('JOBS_DIR', self.fixture.root / 'jobs'),
                            ('CONFIG_ROOT', self.fixture.root), ('_active', None)]:
            p = patch.object(source_classify, name, value)
            p.start()
            self.addCleanup(p.stop)

    def start(self, path='03-测试'):
        return self.request('/api/source/classify', dict(id=self.fixture.ident, path=path))['job']

    def wait(self, ident):
        until = time.monotonic() + 15
        while time.monotonic() < until:
            job = self.request('/api/source/classify-status?job=' + ident)['job']
            if job['status'] not in ('queued', 'running'):
                while source_classify._active and time.monotonic() < until:
                    time.sleep(.01)
                return job
            time.sleep(.02)
        self.fail('Background classification did not complete')

    def test_scope_mapping_dedupe_unknown_and_retry_are_persistent(self):
        cfg = self.fixture.root / 'config.P1.全流程.yaml'
        cfg.write_text('scope:\n  project_no: P1\n  purchaser: 采购单位A\n', encoding='utf-8')
        before = {p.name: p.read_bytes() for p in self.scope.iterdir()}
        first = self.wait(self.start()['id'])
        self.assertEqual(first['status'], 'succeeded', first)
        result = first['result']
        self.assertEqual(result['project']['project_no'], 'P1')
        self.assertEqual(result['project']['purchaser'], '采购单位A')
        self.assertEqual(result['summary']['total'], 3)
        self.assertEqual(result['summary']['duplicates'], 1)
        self.assertEqual(len(result['files']), 2)
        self.assertEqual(result['pending'], 1)
        self.assertEqual(Path(result['path']).parts[-2:], ('采购单位A', 'P1'))
        self.assertEqual({p.name: p.read_bytes() for p in self.scope.iterdir()}, before)
        second = self.wait(self.start()['id'])
        self.assertEqual(second['status'], 'succeeded', second)
        self.assertEqual(len(second['result']['files']), 2)
        self.assertEqual(second['result']['summary']['unchanged'], 3)
        latest = self.request('/api/source/classify-status?id=' + self.fixture.ident + '&path=03-%E6%B5%8B%E8%AF%95')['job']
        self.assertEqual(latest['id'], second['id'])

    def test_directory_history_survives_restart_and_failed_retry(self):
        job = self.wait(self.start()['id'])
        self.assertEqual(job['status'], 'succeeded')
        tree = self.request('/api/source/tree?id=' + self.fixture.ident)
        entry = next(e for e in tree['entries'] if e['path'] == '03-测试')
        self.assertEqual(entry['classification']['completed_at'], job['updated'])
        self.assertIsNone(next(e for e in tree['entries'] if e['path'] == '02-货款支付')['classification'])
        with patch.object(source_classify, '_active', None):
            restored = self.request('/api/source/tree?id=' + self.fixture.ident + '&path=03-%E6%B5%8B%E8%AF%95')
            self.assertEqual(restored['classification']['completed_at'], job['updated'])
        with patch.object(web, 'save_result', return_value={'ok': False, 'failed': ['disk']}):
            failed = self.wait(self.start()['id'])
        self.assertEqual(failed['status'], 'failed')
        tree = self.request('/api/source/tree?id=' + self.fixture.ident)
        history = next(e for e in tree['entries'] if e['path'] == '03-测试')['classification']
        self.assertEqual(history['completed_at'], job['updated'])
        self.assertEqual(history['latest_status'], 'failed')

    def test_payment_water_receipt_from_original_directory(self):
        folder = self.fixture.base / '06-20230921-1038-货款支付-2件-项目'
        folder.mkdir()
        (folder / '三方水单1.pdf').write_bytes(b'payment evidence one')
        (folder / '两方水单.png').write_bytes(b'payment evidence two')
        result = self.wait(self.start(folder.name)['id'])
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(len(result['result']['files']), 2)
        self.assertTrue(all(f['type'] == '付款凭证' for f in result['result']['files']))

    def test_running_job_status_and_source_browsing_do_not_block(self):
        entered, release = threading.Event(), threading.Event()
        save = web.save_result
        def delayed(*args, **kwargs):
            entered.set()
            release.wait(5)
            return save(*args, **kwargs)
        with patch.object(web, 'save_result', delayed):
            job = self.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.start()['id'], job['id'])
                status = self.request('/api/source/classify-status?job=' + job['id'])['job']
                self.assertEqual(status['status'], 'running')
                self.assertTrue(self.request('/api/source/tree?id=' + self.fixture.ident)['entries'])
            finally:
                release.set()
            self.assertEqual(self.wait(job['id'])['status'], 'succeeded')

    def test_failed_save_preserves_originals_and_retry_succeeds(self):
        with patch.object(web, 'save_result', return_value={'ok': False, 'failed': ['disk']}):
            failed = self.wait(self.start()['id'])
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual((self.scope / '采购合同.pdf').read_bytes(), b'contract')
        result = self.wait(self.start()['id'])
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(len(result['result']['files']), 2)

    def test_older_stage_does_not_overwrite_different_content_with_same_name(self):
        for name, content in [('04-早期', b'older'), ('05-后期', b'newer')]:
            folder = self.fixture.base / name
            folder.mkdir()
            (folder / '采购合同.pdf').write_bytes(content)
        self.assertEqual(self.wait(self.start('05-后期')['id'])['status'], 'succeeded')
        result = self.wait(self.start('04-早期')['id'])
        self.assertEqual(result['status'], 'succeeded', result)
        base = Path(result['result']['path'])
        files = result['result']['files']
        self.assertEqual({(base / f['path']).read_bytes() for f in files}, {b'older', b'newer'})

    def test_empty_and_invalid_directories_do_not_archive_other_files(self):
        empty = self.fixture.base / 'empty'
        empty.mkdir()
        result = self.wait(self.start('empty')['id'])
        self.assertEqual(result['status'], 'failed')
        self.assertIn('没有可归类', result['message'])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.start('../../config.yaml')
        self.assertEqual(error.exception.code, 400)

    def test_interrupted_job_is_reported_after_restart(self):
        job = dict(id='a'*32, source_id=self.fixture.ident, relative='', created=1,
                   status='running', progress=20, message='working')
        source_classify.checkpoint(job)
        result = self.request('/api/source/classify-status?job=' + job['id'])['job']
        self.assertEqual(result['status'], 'failed')
        self.assertIn('服务已重启', result['message'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
