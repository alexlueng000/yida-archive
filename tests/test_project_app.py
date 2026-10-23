"""Exercise project isolation and automatic storage through real HTTP requests."""
import base64
import copy
import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from web import server as web


class ProjectAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for name, value in [('OUTPUT_ROOT', str(root / 'archive')),
                            ('SESSION_DIR', root / 'cache' / 'web-session'),
                            ('SERVER_CONFIG', {}), ('STATE', copy.deepcopy(web.STATE))]:
            p = patch.object(web, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.server = web.Server(('127.0.0.1', 0), web.Handler)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = 'http://127.0.0.1:%d' % self.server.server_address[1]

    def request(self, path, body=None, binary=False):
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=20) as response:
            data = response.read()
        return data if binary else json.loads(data)

    def create(self, no='P1', purchaser='采购单位'):
        return self.request('/api/projects', dict(project_no=no, purchaser=purchaser))['project']['id']

    def upload(self, ident, files):
        return self.request('/api/project/upload?id=' + ident, dict(files=[
            dict(name=name, data=base64.b64encode(content.encode()).decode()) for name, content in files]))

    def test_auto_save_dedupe_unknown_plain_and_download(self):
        ident = self.create()
        first = self.upload(ident, [('采购合同.pdf', 'contract'), ('陌生资料.txt', 'unknown')])
        self.assertEqual(len(first['files']), 2)
        self.assertEqual(first['pending'], 1)
        unknown = next(f for f in first['files'] if f['folder'] == '未归类')
        self.assertTrue((Path(first['path']) / unknown['path']).is_file())
        for f in first['files']:
            self.assertNotIn('加密', f['path'])
            self.assertNotIn('公共', f['path'])
        second = self.upload(ident, [('改名合同.pdf', 'contract')])
        self.assertEqual(len(second['files']), 2)
        content = self.request('/api/project/file?id=' + ident + '&path=' + urllib.parse.quote(unknown['path']), binary=True)
        self.assertEqual(content, b'unknown')
        packed = self.request('/api/project/export?id=' + ident, binary=True)
        with zipfile.ZipFile(io.BytesIO(packed)) as archive:
            self.assertEqual(len(archive.namelist()), 2)
        listing = self.request('/api/projects')['projects']
        self.assertEqual(listing[0]['count'], 2)
        self.assertEqual(listing[0]['pending'], 1)

    def test_project_isolation_and_restart(self):
        a, b = self.create('A'), self.create('B')
        self.upload(a, [('采购合同.pdf', 'AAA')])
        self.upload(b, [('采购合同.pdf', 'BBB')])
        self.upload(a, [('发票.pdf', 'invoice')])
        self.assertEqual(len(self.request('/api/project/detail?id=' + a)['files']), 2)
        self.assertEqual(len(self.request('/api/project/detail?id=' + b)['files']), 1)
        with patch.dict(web.STATE, dict(batches=[], blobs={}, last_result=None)):
            self.assertEqual(len(self.upload(a, [('采购合同.pdf', 'AAA')])['files']), 2)
        self.assertEqual(len(self.request('/api/projects')['projects']), 2)

    def test_failed_archive_retains_original_and_retry_works(self):
        ident = self.create()
        with patch.object(web, 'save_result', return_value={'ok': False, 'failed': ['disk']}):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.upload(ident, [('采购合同.pdf', 'retained')])
            self.assertEqual(error.exception.code, 500)
        result = self.request('/api/project/save?id=' + ident, {})
        self.assertEqual(len(result['files']), 1)

    def test_rejects_duplicate_project_and_path_traversal(self):
        ident = self.create('Project')
        with self.assertRaises(urllib.error.HTTPError):
            self.create('project')
        with self.assertRaises(urllib.error.HTTPError):
            self.create('../escape')
        with self.assertRaises(urllib.error.HTTPError):
            self.request('/api/project/file?id=' + ident + '&path=../../config.yaml')

    def test_invalid_uploads_do_not_create_archived_files(self):
        ident = self.create()
        for data in ('', '%%%invalid%%%'):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request('/api/project/upload?id=' + ident,
                             {'files': [{'name': '采购合同.pdf', 'data': data}]})
            self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.request('/api/project/detail?id=' + ident)['files'], [])

    def test_history_visible_without_session_and_upload_preserves_it(self):
        ident = self.create()
        result = self.upload(ident, [('采购合同.pdf', 'history')])
        import shutil
        from web import projects
        folder = projects.catalogue_path().parent / ident
        shutil.rmtree(folder)
        self.assertEqual(len(self.request('/api/project/detail?id=' + ident)['files']), 1)
        result = self.upload(ident, [('陌生.txt', 'new')])
        self.assertEqual(len(result['files']), 2)

    def test_legacy_encrypted_directory_migrates_to_plain_project_folder(self):
        import os
        import shutil
        from web import projects
        ident = self.create()
        result = self.upload(ident, [('采购合同.pdf', 'legacy')])
        base = Path(result['path'])
        source = base / result['files'][0]['path']
        old = base / '加密' / '合同' / source.name
        old.parent.mkdir(parents=True)
        os.replace(source, old)
        ledger = base / '编号记录.json'
        data = json.loads(ledger.read_text(encoding='utf-8'))
        for unit in data['units'].values():
            unit['path'] = old.relative_to(web.OUTPUT_ROOT).as_posix()
            unit['file']['secret'] = True
        ledger.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        shutil.rmtree(projects.catalogue_path().parent / ident)
        result = self.request('/api/project/save?id=' + ident, {})
        self.assertEqual(len(result['files']), 1)
        self.assertEqual(result['files'][0]['path'], '合同/' + source.name)
        self.assertEqual(source.read_bytes(), b'legacy')
        self.assertFalse((base / '加密').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
