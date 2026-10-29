"""Verify original-directory navigation, file access, and project boundaries."""
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_project_app as project_test
from web import source_browser


class SourceBrowserTests(unittest.TestCase):
    def setUp(self):
        self.app = project_test.ProjectAppTests()
        self.app.setUp()
        self.addCleanup(self.app.doCleanups)
        self.root = Path(self.app.tmp.name) / 'sources'
        self.root.mkdir()
        p = patch.object(source_browser, 'SOURCE_ROOT', self.root)
        p.start()
        self.addCleanup(p.stop)
        self.base = self.root / 'archive-P1-全流程' / '每次上传'
        (self.base / '01-合同审批' / '附件').mkdir(parents=True)
        (self.base / '01-合同审批' / '附件' / '合同.pdf').write_bytes(b'%PDF-original')
        (self.base / '02-货款支付').mkdir()
        (self.base / '02-货款支付' / '发票.txt').write_text('original invoice', encoding='utf-8')
        (self.root / 'archive-P2' / '每次上传').mkdir(parents=True)
        (self.root / 'archive-P3').mkdir()
        self.request = self.app.request
        self.sources = self.request('/api/sources')['sources']
        self.ident = next(s['id'] for s in self.sources if s['name'] == 'P1-全流程')

    def test_lists_projects_and_nested_old_and_flat_new_directories(self):
        self.assertEqual(len(self.sources), 2)
        data = self.request('/api/source/tree?id=' + self.ident)
        self.assertEqual(data['path'], '')
        self.assertEqual([e['name'] for e in data['entries']], ['01-合同审批', '02-货款支付'])
        self.assertEqual(data['entries'][0]['folders'], 1)
        self.assertEqual(data['entries'][1]['files'], 1)
        from urllib.parse import quote
        data = self.request('/api/source/tree?id=' + self.ident + '&path=' + quote('01-合同审批/附件'))
        self.assertEqual(data['entries'][0]['name'], '合同.pdf')
        self.assertTrue(data['entries'][0]['preview'])
        self.assertEqual(data['entries'][0]['size'], len(b'%PDF-original'))

    def test_projects_group_batches_and_nightly_archive_by_project_number(self):
        # 同一个项目下会有多个下载批次，夜间归档又是另一套目录结构，都要并到一个项目里。
        key = '24JZBTPFB157-401'
        for folder in ('archive-%s' % key, 'archive-%s-全流程' % key):
            path = self.root / folder / '每次上传' / '01-合同审批'
            path.mkdir(parents=True)
            (path / '合同.pdf').write_bytes(b'%PDF-batch')
        nightly = Path(self.app.tmp.name) / 'nightly'
        (nightly / '南方科技大学' / key / '公共' / '合同').mkdir(parents=True)
        (nightly / '南方科技大学' / key / '公共' / '合同' / '1-采购合同.pdf').write_bytes(b'%PDF-archived')
        (nightly / '南方科技大学' / '不是项目号的目录').mkdir(parents=True)
        with patch.object(source_browser, '_archive_root', lambda: nightly):
            data = self.request('/api/sources')
        group = next(g for g in data['projects'] if g['project'] == key)
        self.assertEqual(len(group['sources']), 2)
        self.assertEqual(group['files'], 2)
        self.assertFalse(group['archived'])
        self.assertEqual(group['batches'], 2)
        self.assertEqual(group['sources'][0]['kind'], 'batch')
        # 认不出项目号的目录不硬塞进别的项目
        unknown = next(g for g in data['projects'] if g['project'] == source_browser.UNKNOWN_PROJECT)
        self.assertEqual(sorted(s['name'] for s in unknown['sources']), ['P1-全流程', 'P2'])

    def test_old_style_project_number_and_unattributed_directories_are_listed(self):
        key = 'B22026LD001'
        nightly = Path(self.app.tmp.name) / 'nightly2'
        (nightly / '深圳理工大学' / key / '公共' / '合同').mkdir(parents=True)
        (nightly / '深圳理工大学' / key / '公共' / '合同' / '1-采购合同.pdf').write_bytes(b'%PDF')
        (nightly / source_browser.UNATTRIBUTED_DIR / '2026-10-27' / '总代理协议' / '单据A').mkdir(parents=True)
        (nightly / source_browser.UNATTRIBUTED_DIR / '2026-10-27' / '总代理协议' / '单据A' /
         '结算单.xlsx').write_bytes(b'x')
        with patch.object(source_browser, '_archive_root', lambda: nightly):
            data = self.request('/api/sources')
        by_key = {g['project']: g for g in data['projects']}
        # 分类结果不混入原始附件浏览。
        self.assertNotIn(key, by_key)
        # 认领不了的单据单独成组，日期作为目录列出来
        self.assertIn(source_browser.UNATTRIBUTED_PROJECT, by_key)
        self.assertEqual(by_key[source_browser.UNATTRIBUTED_PROJECT]['files'], 1)
        self.assertEqual([s['name'] for s in by_key[source_browser.UNATTRIBUTED_PROJECT]['sources']],
                         ['2026-10-27'])

    def test_download_and_view_original_file_without_changes(self):
        from urllib.parse import quote
        path = '01-合同审批/附件/合同.pdf'
        original = self.base / path
        stamp = original.stat().st_mtime_ns
        url = '/api/source/file?id=' + self.ident + '&path=' + quote(path)
        self.assertEqual(self.request(url, binary=True), b'%PDF-original')
        with urllib.request.urlopen(self.app.base + url + '&view=1', timeout=10) as response:
            self.assertEqual(response.headers['Content-Type'], 'application/pdf')
            self.assertTrue(response.headers['Content-Disposition'].startswith('inline;'))
            self.assertEqual(response.read(), b'%PDF-original')
        self.assertEqual(original.stat().st_mtime_ns, stamp)
        self.assertEqual(len(list(self.base.rglob('*.pdf'))), 1)

    def test_rejects_outside_paths_and_other_projects(self):
        from urllib.parse import quote
        for path in ('../../config.yaml', '/config.yaml', r'C:\config.yaml',
                     r'..\..\config.yaml', '../../archive-P2/每次上传'):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request('/api/source/tree?id=' + self.ident + '&path=' + quote(path))
            self.assertEqual(error.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/source/tree?id=unknown')
        self.assertEqual(error.exception.code, 404)

    def test_html_download_cannot_be_opened_inline(self):
        (self.base / 'unsafe.html').write_text('<script>alert(1)</script>')
        with urllib.request.urlopen(self.app.base + '/api/source/file?id=' + self.ident +
                                    '&path=unsafe.html&view=1', timeout=10) as response:
            self.assertTrue(response.headers['Content-Disposition'].startswith('attachment;'))
            self.assertEqual(response.headers['Content-Type'], 'application/octet-stream')


if __name__ == '__main__':
    unittest.main(verbosity=2)
