# -*- coding: utf-8 -*-
"""本地测试 web 服务的端到端测试。

起一个真的 HTTP 服务（随机端口），走真的接口：
新建上传 -> 传文件 -> 复制一次上传（模拟重传）-> 分析 -> 导出 zip。
不需要浏览器，也不需要连宜搭。

跑法： py tests\\test_web.py
"""

import base64
import http.client
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web import server as web

SERVER = None
PORT = 0
SESSION_TEMP = None
SESSION_PATCH = None


def setUpModule():
    global SERVER, PORT, SESSION_TEMP, SESSION_PATCH
    SESSION_TEMP = tempfile.TemporaryDirectory()
    SESSION_PATCH = patch.object(web, "SESSION_DIR", Path(SESSION_TEMP.name))
    SESSION_PATCH.start()
    # 测试里不需要访问日志，否则把断言输出淹了
    web.Handler.log_message = lambda *a, **k: None
    SERVER = web.Server(('127.0.0.1', 0), web.Handler)
    PORT = SERVER.server_address[1]
    threading.Thread(target=SERVER.serve_forever, daemon=True).start()


def tearDownModule():
    SERVER.shutdown()
    SERVER.server_close()
    SESSION_PATCH.stop()
    SESSION_TEMP.cleanup()


def post(path, obj=None):
    data = json.dumps(obj or {}).encode('utf-8')
    req = urllib.request.Request(
        'http://127.0.0.1:%d%s' % (PORT, path), data=data,
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode('utf-8'))


def get(path):
    with urllib.request.urlopen(
            'http://127.0.0.1:%d%s' % (PORT, path), timeout=20) as r:
        return json.loads(r.read().decode('utf-8'))


def fetch_page(path='/'):
    with urllib.request.urlopen(
            'http://127.0.0.1:%d%s' % (PORT, path), timeout=20) as r:
        return r.read().decode('utf-8')


def b64(text):
    return base64.b64encode(text.encode('utf-8')).decode('ascii')


class WebTestBase(unittest.TestCase):
    FILES = [('采购合同-B22026LD001.pdf', 'A' * 500),
             ('增值税发票.pdf', 'B' * 300),
             ('装箱单.xlsx', 'C' * 200)]

    def setUp(self):
        # 编号持久化后，测试必须隔离真实项目账本与归档目录。
        output = tempfile.TemporaryDirectory()
        self.addCleanup(output.cleanup)
        output_patch = patch.object(web, 'OUTPUT_ROOT', output.name)
        output_patch.start()
        self.addCleanup(output_patch.stop)
        post('/api/reset')
        with web._lock:
            web.STATE.update(project_no='B22026LD001', purchaser='测试单位', unmatched_policy='skip', project_secret='否')
        self.batch = post('/api/batch', {'name': '第一次上传'})['batch']['id']

    def upload(self, batch_id, items):
        """items: [(文件名, 内容)]"""
        body = {'batch_id': batch_id,
                'files': [{'name': n, 'data': b64(c)} for n, c in items]}
        return post('/api/upload', body)


class TestSessionPersistence(WebTestBase):
    def test_restart_restores_uploads_result_settings_and_reviews(self):
        self.upload(self.batch, self.FILES)
        post('/api/settings', {'project_no': 'RESTART', 'purchaser': '续测单位'})
        result = post('/api/analyze', {'project_secret': '否'})
        post('/api/review', {'id': result['files'][0]['id'], 'type': '箱单'})
        expected = get('/api/state')
        content = dict(web.STATE['blobs'])
        reviews = dict(web.STATE['file_reviews'])
        with web._lock:
            web.STATE.update(batches=[], blobs={}, last_result=None, file_reviews={})
            self.assertTrue(web.load_session())
        self.assertEqual(get('/api/state'), expected)
        self.assertEqual(web.STATE['blobs'], content)
        self.assertEqual(web.STATE['file_reviews'], reviews)
        self.assertGreater(expected['batches'][0]['uploaded_at'], 0)

    def test_reset_is_persistent_and_removes_cached_attachments(self):
        self.upload(self.batch, self.FILES)
        post('/api/reset')
        with web._lock:
            web.load_session()
        self.assertEqual(get('/api/state')['batches'], [])
        self.assertEqual(web.STATE['blobs'], {})
        self.assertEqual(list((web.SESSION_DIR / 'blobs').iterdir()), [])

    def test_interrupted_checkpoint_preserves_previous_session(self):
        self.upload(self.batch, self.FILES)
        before = (web.SESSION_DIR / 'session.json').read_bytes()
        with web._lock, patch.object(web.os, 'replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                web.save_session()
        self.assertEqual((web.SESSION_DIR / 'session.json').read_bytes(), before)
        self.assertTrue(web.load_session())


class TestWebFlow(WebTestBase):
    def test_server_save_without_review_uses_automatic_rules(self):
        self.upload(self.batch, [('支付两方合同SRS部分尾款凭证.png', 'PAY'),
                                ('发展确认结算表.png', 'CHAT'),
                                ('采购合同草稿.pdf', 'DRAFT'),
                                ('FedEx-AWB-5Sep2024.pdf', 'AWB')])
        with tempfile.TemporaryDirectory() as tmp, patch.object(web, 'OUTPUT_ROOT', tmp):
            saved = post('/api/save', {'project_no': 'P1'})
            self.assertTrue(saved['ok'])
            self.assertEqual((saved['saved'], saved['skipped']), (4, 0))
            base = Path(saved['path'])
            self.assertEqual(len(list((base / '收付款').glob('*.png'))), 1)
            self.assertEqual(len(list((base / '清关送货').glob('*.pdf'))), 1)
            self.assertEqual(len(list((base / '未归类').iterdir())), 2)
            self.assertEqual(web.STATE['file_reviews'], {})

    def test_repeated_save_replaces_project_and_removes_old_classification(self):
        self.upload(self.batch, [('采购合同.pdf', 'AAAA')])
        result = post('/api/analyze', {'project_no': 'P1'})
        with tempfile.TemporaryDirectory() as tmp, patch.object(web, 'OUTPUT_ROOT', tmp):
            first = post('/api/save')
            other = Path(tmp) / 'P2_文件分类'
            other.mkdir()
            (other / 'keep.txt').write_text('keep')
            old = next(Path(first['path']).rglob('*.pdf'))
            post('/api/review', {'id': result['files'][0]['id'], 'type': '箱单'})
            second = post('/api/save')
            self.assertEqual(first['path'], second['path'])
            self.assertEqual(Path(second['path']), Path(tmp) / '测试单位' / 'P1')
            self.assertFalse(old.exists())
            new = list(Path(second['path']).rglob('*.pdf'))
            self.assertEqual(len(new), 1)
            self.assertEqual(new[0].parent.name, '清关送货')
            self.assertEqual(new[0].read_bytes(), b'AAAA')
            self.assertEqual((other / 'keep.txt').read_text(), 'keep')
            self.assertEqual({p.name for p in Path(tmp).iterdir()}, {'测试单位', 'P2_文件分类'})

    def test_failed_save_preserves_previous_project(self):
        self.upload(self.batch, [('采购合同.pdf', 'AAAA')])
        post('/api/analyze', {'project_no': 'P1'})
        with tempfile.TemporaryDirectory() as tmp, patch.object(web, 'OUTPUT_ROOT', tmp):
            first = post('/api/save')
            before = {p.relative_to(tmp): p.read_bytes() for p in Path(tmp).rglob('*') if p.is_file()}
            newer = post('/api/batch')['batch']['id']
            self.upload(newer, [('采购合同.pdf', 'NEW CONTENT')])
            with patch.dict(web.STATE, {'blobs': {}}):
                failed = post('/api/save')
            self.assertFalse(failed['ok'])
            self.assertEqual(failed['saved'], 0)
            self.assertEqual(failed['path'], first['path'])
            after = {p.relative_to(tmp): p.read_bytes() for p in Path(tmp).rglob('*') if p.is_file()}
            self.assertEqual(before, after)

    def test_repeated_save_overwrites_contents_without_numbered_copy(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(web, 'OUTPUT_ROOT', tmp):
            self.upload(self.batch, [('采购合同.pdf', 'AAAA')])
            first = post('/api/save', {'project_no': 'P1'})
            post('/api/reset')
            batch = post('/api/batch')['batch']['id']
            self.upload(batch, [('采购合同v2.pdf', 'BBBB')])
            second = post('/api/save', {'project_no': 'P1'})
            self.assertEqual(first['path'], second['path'])
            files = list(Path(second['path']).rglob('*.pdf'))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].name, '3-采购合同v2-P1.pdf')
            self.assertEqual(files[0].read_bytes(), b'BBBB')

    def test_save_matches_zip_and_retains_equal_size_different_contents(self):
        self.upload(self.batch, [('采购合同.pdf', 'AAAA'), ('采购合同.pdf', 'BBBB')])
        post('/api/analyze', {'project_no': 'P1'})
        with tempfile.TemporaryDirectory() as tmp, patch.object(web, 'OUTPUT_ROOT', tmp):
            result = post('/api/save', {})
            self.assertTrue(result['ok'], result)
            self.assertEqual(result['saved'], 2)
            paths = list(Path(result['path']).rglob('*.pdf'))
            self.assertEqual({p.read_bytes() for p in paths}, {b'AAAA', b'BBBB'})
            self.assertEqual({p.parent.name for p in paths}, {'合同'})
            self.assertFalse((Path(result['path']) / '加密').exists())
            self.assertTrue((Path(result['path']) / '未归类').is_dir())
            with urllib.request.urlopen('http://127.0.0.1:%d/api/export' % PORT) as resp:
                zf = zipfile.ZipFile(io.BytesIO(resp.read()))
            zipped = {n: zf.read(n) for n in zf.namelist() if n.endswith('.pdf')}
            saved = {'测试单位/P1/' + p.relative_to(result['path']).as_posix(): p.read_bytes() for p in paths}
            self.assertEqual(zipped, saved)

    def test_confirm_latest_then_export_only_v2(self):
        self.upload(self.batch, [('采购合同.pdf', 'AAAA'), ('采购合同.pdf', 'BBBB')])
        result = post('/api/analyze', {'project_no': 'P1'})
        rows = result['files']
        for index, row in enumerate(rows, 1):
            result = post('/api/review', {'id': row['id'], 'version_group': '采购一', 'version_order': index})
        self.assertEqual(result['summary']['superseded'], 1)
        self.assertEqual([f['new_name'] for f in result['files'] if not f['superseded']], ['3-采购合同v2-P1.pdf'])
        with tempfile.TemporaryDirectory() as tmp, patch.object(web, 'OUTPUT_ROOT', tmp):
            saved = post('/api/save')
            self.assertEqual(saved['saved'], 1)
            self.assertEqual(len(list(Path(saved['path']).rglob('*.pdf'))), 1)

    def test_unknown_saved_even_when_old_client_requests_skip(self):
        self.upload(self.batch, [('可行性测算表.xlsx', 'SECRET')])
        post('/api/analyze', {'unmatched_policy': 'skip'})
        with tempfile.TemporaryDirectory() as tmp, patch.object(web, 'OUTPUT_ROOT', tmp):
            saved = post('/api/save')
            self.assertEqual(saved['saved'], 1)
            self.assertTrue((Path(saved['path']) / '未归类' / '可行性测算表.xlsx').is_file())

    def test_manual_classification_is_used_when_saving(self):
        self.upload(self.batch, [('扫描文件.pdf', 'ABCD')])
        result = post('/api/analyze', {'project_no': 'P1'})
        post('/api/review', {'id': result['files'][0]['id'], 'type': '采购合同'})
        with tempfile.TemporaryDirectory() as tmp, patch.object(web, 'OUTPUT_ROOT', tmp):
            saved = post('/api/save')
            self.assertTrue((Path(saved['path']) / '合同' / '3-采购合同-P1.pdf').is_file())

    def test_metadata_only_save_reports_missing_content(self):
        post('/api/seed')
        with tempfile.TemporaryDirectory() as tmp, patch.object(web, 'OUTPUT_ROOT', tmp):
            saved = post('/api/save')
            self.assertFalse(saved['ok'])
            self.assertEqual(saved['saved'], 0)
            self.assertTrue(saved['failed'])

    def test_serves_page_without_external_resources(self):
        """页面本身要能打开，而且不能引用外部 CDN——服务器上多半没外网。"""
        with urllib.request.urlopen(
                'http://127.0.0.1:%d/' % PORT, timeout=20) as r:
            self.assertEqual(r.status, 200)
            self.assertIn('text/html', r.headers['Content-Type'])
            html = r.read().decode('utf-8')
        self.assertIn('项目档案', html)
        self.assertIn('/api/projects', html)
        for bad in ('http://cdn', 'https://cdn', '//unpkg', '//cdn.'):
            self.assertNotIn(bad, html, '页面引了外部资源：%s' % bad)

    def test_static_path_traversal_blocked(self):
        """静态目录不能让 ../ 穿出去读到仓库里的文件。

        用 http.client 而不是 urllib：urllib 会把 ../ 规范化掉，
        那样测的就不是服务端的拦截了。
        """
        for path in ('/../yida_archive/config.py',
                     '/..%2f..%2frun.py',
                     '/../server.py'):
            conn = http.client.HTTPConnection('127.0.0.1', PORT, timeout=20)
            conn.request('GET', path)
            resp = conn.getresponse()
            body = resp.read().decode('utf-8', 'replace')
            conn.close()
            self.assertIn(resp.status, (400, 404),
                          '%s 返回了 %d' % (path, resp.status))
            self.assertNotIn('DEFAULT_RULES', body)
            self.assertNotIn('def main()', body)

    def test_empty_state_shows_date_extraction_without_upload(self):
        """空状态通过审批日期拉取附件，不再提供新建和上传入口。"""
        post('/api/reset')
        s = get('/api/state')
        self.assertEqual(s['batches'], [], 'reset 后应该是空的')
        html = fetch_page()
        for needle in ('id="extractForm"', 'id="extractDate"', '开始拉取'):
            self.assertIn(needle, html)
        for needle in ('id="newBtn"', 'id="fileInput"', 'id="dropzone"'):
            self.assertNotIn(needle, html)

    def test_empty_state_upload_flow(self):
        """空状态下「建一条 + 传文件」这条链路要通，这正是上传框做的事。"""
        post('/api/reset')
        b = post('/api/batch', {})['batch']['id']
        self.upload(b, self.FILES)
        s = get('/api/state')
        self.assertEqual(len(s['batches']), 1)
        self.assertEqual(len(s['batches'][0]['files']), 3)
        r = post('/api/analyze', {})
        self.assertEqual(r['summary']['total_uploads'], 3)

    def test_empty_state(self):
        s = get('/api/state')
        self.assertEqual(len(s['batches']), 1)
        self.assertEqual(s['batches'][0]['files'], [])
        self.assertIsNone(s['result'])
        self.assertEqual(s['batches'][0]['field'], '附件')

    def test_upload_shows_files(self):
        r = self.upload(self.batch, self.FILES)
        self.assertEqual(len(r['added']), 3)
        self.assertEqual(r['skipped'], [])
        s = get('/api/state')
        self.assertEqual(len(s['batches']), 1)
        self.assertEqual(len(s['batches'][0]['files']), 3)
        # 哈希要真的算出来，不能是空的
        self.assertEqual(len(s['batches'][0]['files'][0]['sha256']), 12)

    def test_analyze_without_files(self):
        r = post('/api/analyze', {})
        self.assertFalse(r['ok'])
        self.assertIn('还没有上传', r['message'])

    def test_analyze_classifies(self):
        self.upload(self.batch, self.FILES)
        r = post('/api/analyze', {
            'project_no': 'B22026LD001', 'purchaser': '测试单位'})
        self.assertTrue(r['ok'])
        s = r['summary']
        self.assertEqual(s['batches'], 1)
        self.assertEqual(s['total_uploads'], 3)
        self.assertEqual(s['logical_files'], 3)
        self.assertEqual(s['classified'], 3)
        self.assertEqual(s['manual'], 0)
        # 增值税发票 属于默认加密类型
        self.assertEqual(s['secret'], 0)

        names = {}
        for g in r['folders']:
            for f in g['files']:
                names[f['origin']] = (g['name'], f['type'], f['secret'])
        self.assertEqual(names['采购合同-B22026LD001.pdf'][0], '合同')
        self.assertEqual(names['增值税发票.pdf'][0], '结算移交')
        self.assertFalse(names['增值税发票.pdf'][2])
        self.assertEqual(names['装箱单.xlsx'][0], '清关送货')

    def test_clone_then_dedupe(self):
        """复制一次上传 = 同一批文件又传了一遍，应该全被去重掉。"""
        self.upload(self.batch, self.FILES)
        cloned = post('/api/clone', {'batch_id': self.batch})
        self.assertEqual(cloned['count'], 3)

        r = post('/api/analyze', {'project_no': 'B22026LD001'})
        s = r['summary']
        self.assertEqual(s['batches'], 2, '应该变成两次上传')
        self.assertEqual(s['total_uploads'], 6)
        self.assertEqual(s['logical_files'], 3, '6 次上传应该去重成 3 个文件')
        self.assertEqual(len(r['merged']), 3)
        for m in r['merged']:
            self.assertEqual(m['uploads'], 2)
            self.assertFalse(m['renamed'], '同名同内容，不该判成改名')
            self.assertEqual(len(m['batches']), 2)

    def test_same_content_different_name_merged(self):
        """内容一样但文件名不同，哈希应该把它们并掉。"""
        a = post('/api/batch', {'name': 'A'})['batch']['id']
        b = post('/api/batch', {'name': 'B'})['batch']['id']
        self.upload(a, [('合同-初稿.pdf', 'SAME-CONTENT-1234')])
        self.upload(b, [('合同-终稿.pdf', 'SAME-CONTENT-1234')])
        r = post('/api/analyze', {})
        self.assertEqual(r['summary']['logical_files'], 1)
        self.assertEqual(len(r['merged']), 1)
        self.assertTrue(r['merged'][0]['renamed'],
                        '改名副本应该被标出来')

    def test_same_name_different_content_kept_as_versions(self):
        """同名不同内容不能合并，要按版本报出来。"""
        a = post('/api/batch', {'name': 'A'})['batch']['id']
        b = post('/api/batch', {'name': 'B'})['batch']['id']
        self.upload(a, [('结算表.xlsx', 'X' * 100)])
        self.upload(b, [('结算表.xlsx', 'X' * 400)])
        r = post('/api/analyze', {})
        self.assertEqual(r['summary']['logical_files'], 2, '同名不同内容不该合并')
        vc = r['findings']['version_conflicts']
        self.assertEqual(len(vc), 1)
        self.assertEqual(vc[0]['name'], '结算表.xlsx')
        news = [f['new_name'] for g in r['folders'] for f in g['files']]
        self.assertEqual(news, ['26-结算表v2-B22026LD001.xlsx'])
        self.assertEqual(r['summary']['superseded'], 1)

    def test_unknown_file_goes_manual(self):
        self.upload(self.batch, [('扫描件_0001.dat', 'Z' * 50)])
        r = post('/api/analyze', {'unmatched_policy': 'skip'})
        self.assertEqual(r['summary']['classified'], 0)
        self.assertEqual(r['summary']['manual'], 1)
        self.assertEqual(len(r['manual']), 1)
        self.assertEqual(r['folders'], [])

    def test_field_label_participates(self):
        """字段名会参与关键词匹配。

        文件名本身不带任何关键词，但字段叫「装箱单」，靠字段名就能归类。
        注意字段名只在有规则关键词命中时才有用——规则表里没有裸的「发票」
        关键词，所以字段叫「上传发票」并不会自动归到发票类（这是当前行为）。
        """
        b = post('/api/batch', {'name': '付款'})['batch']['id']
        post('/api/patch', {'batch_id': b, 'field': '装箱单'})
        self.upload(b, [('scan_2026.pdf', 'Q' * 77)])
        r = post('/api/analyze', {})
        self.assertEqual(r['summary']['classified'], 1)
        types = [f['type'] for g in r['folders'] for f in g['files']]
        self.assertEqual(types, ['箱单'])
        # 关键词没直接出现在文件名里，只能算「建议核对」，不能算确认
        confs = [f['confidence'] for g in r['folders'] for f in g['files']]
        self.assertEqual(confs, ['suggested'])

    def test_field_label_alone_does_not_classify(self):
        """字段名不带规则关键词时，仍然进待人工，不硬塞。"""
        b = post('/api/batch', {'name': '付款'})['batch']['id']
        post('/api/patch', {'batch_id': b, 'field': '上传发票'})
        self.upload(b, [('scan_2026.pdf', 'Q' * 77)])
        r = post('/api/analyze', {})
        self.assertEqual(r['summary']['classified'], 0)
        self.assertEqual(r['summary']['manual'], 1)

    def test_rename_batch(self):
        post('/api/patch', {'batch_id': self.batch, 'name': '货款支付 第一次'})
        s = get('/api/state')
        self.assertEqual(s['batches'][0]['name'], '货款支付 第一次')
        # 改名不能顺手多建一个批次
        self.assertEqual(len(s['batches']), 1)

    def test_delete_file_and_batch(self):
        r = self.upload(self.batch, self.FILES)
        fid = r['added'][0]['id']
        s = get('/api/state')
        self.assertEqual(len(s['batches'][0]['files']), 3)
        post('/api/remove', {'kind': 'file', 'batch_id': self.batch,
                             'file_id': fid})
        s = get('/api/state')
        self.assertEqual(len(s['batches'][0]['files']), 2)
        post('/api/remove', {'kind': 'batch', 'batch_id': self.batch})
        s = get('/api/state')
        self.assertEqual(s['batches'], [])

    def test_analyze_clears_when_files_change(self):
        self.upload(self.batch, self.FILES)
        post('/api/analyze', {})
        self.assertIsNotNone(get('/api/state')['result'])
        self.upload(self.batch, [('另一个装箱单.xlsx', 'N' * 90)])
        self.assertIsNone(get('/api/state')['result'],
                          '文件变了就要清掉旧结果，别让人看陈旧结论')

    def test_export_zip(self):
        self.upload(self.batch, self.FILES)
        post('/api/clone', {'batch_id': self.batch})
        post('/api/analyze', {'project_no': 'B22026LD001',
                              'purchaser': '测试单位'})
        req = urllib.request.Request('http://127.0.0.1:%d/api/export' % PORT,
                                     data=b'{}',
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=20) as resp:
            self.assertEqual(resp.headers['Content-Type'], 'application/zip')
            raw = resp.read()

        zf = zipfile.ZipFile(io.BytesIO(raw))
        names = zf.namelist()
        # 6 次上传去重成 3 个文件，zip 里就该只有 3 个文件 + 2 个清单
        payload = [n for n in names if not n.endswith(('.csv', '.md'))]
        self.assertEqual(len(payload), 3, names)
        self.assertEqual(len(set(payload)), 3, 'zip 里不能有重名条目')
        self.assertTrue(any(n == '清单.csv' for n in names), names)
        self.assertTrue(any(n == '结果.md' for n in names), names)
        for n in payload:
            # 落在 公共/ 或 加密/ 下，再按 采购单位/项目/分类 分层，没有例外
            self.assertTrue(n.startswith('测试单位/B22026LD001/'), n)
            self.assertNotIn('/加密/', n)
            self.assertNotIn('/公共/', n)
            self.assertTrue(zf.read(n), n)
        joined = '\n'.join(payload)
        # 归档后的文件名用类型名，不是原文件名
        self.assertIn('3-采购合同-B22026LD001.pdf', joined)
        self.assertIn('15-箱单-B22026LD001.xlsx', joined)
        # 增值税发票属于默认加密类型，必须在 加密/ 下，不能混进 公共/
        self.assertIn('测试单位/B22026LD001/', joined)

        # 原文件名不能丢：清单.csv 里要能查到，否则没法对账
        csv_text = zf.read('清单.csv').decode('utf-8-sig')
        for origin in ('采购合同-B22026LD001.pdf', '增值税发票.pdf',
                       '装箱单.xlsx'):
            self.assertIn(origin, csv_text)
        self.assertIn('上传次数', csv_text)

    def test_export_ignores_legacy_secret_flag(self):
        web.STATE['project_secret'] = '是'
        self.upload(self.batch, [('增值税发票.pdf', 'B' * 300)])
        post('/api/analyze', {'project_no': 'B22026LD001',
                              'purchaser': '测试单位'})
        req = urllib.request.Request('http://127.0.0.1:%d/api/export' % PORT,
                                     data=b'{}',
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=20) as resp:
            zf = zipfile.ZipFile(io.BytesIO(resp.read()))
        payload = [n for n in zf.namelist()
                   if not n.endswith(('.csv', '.md'))]
        self.assertEqual(len(payload), 1, zf.namelist())
        self.assertTrue(payload[0].startswith('测试单位/B22026LD001/'), payload[0])
        self.assertNotIn('/加密/', payload[0])

    def test_manual_files_exported_under_manual(self):
        self.upload(self.batch, [('扫描件_0001.dat', 'Z' * 50)])
        post('/api/analyze', {'unmatched_policy': 'manual', 'project_no': 'P1'})
        req = urllib.request.Request('http://127.0.0.1:%d/api/export' % PORT,
                                     data=b'{}',
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=20) as resp:
            zf = zipfile.ZipFile(io.BytesIO(resp.read()))
        self.assertIn('测试单位/P1/未归类/扫描件_0001.dat', zf.namelist())

    def test_duplicate_names_in_two_batches_not_merged(self):
        """同名同大小但内容不同——如果按名字+大小合并是错的，靠哈希兜底才对。

        回归旧版按 (name,size) 误合并的问题，必须保留两个独立哈希。
        """
        a = post('/api/batch', {'name': 'A'})['batch']['id']
        b = post('/api/batch', {'name': 'B'})['batch']['id']
        self.upload(a, [('装箱单.xlsx', 'P' * 100)])
        self.upload(b, [('装箱单.xlsx', 'P' * 99 + 'Q')])
        r = post('/api/analyze', {})
        # 哈希不合并，修订管理只将最新版放入最终归档。
        self.assertEqual(r['summary']['logical_files'], 2)
        files = [f for g in r['folders'] for f in g['files']]
        self.assertEqual(len(files), 1)
        self.assertEqual(len({f['hash'] for f in r['files']}), 2)
        self.assertIn('v2', files[0]['new_name'])

    def test_incremental_unnumbered_revisions_and_history_survive_reset(self):
        self.upload(self.batch, [('采购合同A.pdf', 'VERSION ONE'), ('装箱单.xlsx', 'PACK')])
        first = post('/api/save', {'project_no': 'P1'})
        root = Path(first['path'])
        old = root / '合同/3-采购合同-P1.pdf'
        unchanged = next(root.rglob('*.xlsx'))
        before_stat = unchanged.stat()
        self.assertTrue(old.exists())
        # 清空上传内存模拟后续流程/服务重启；不重新上传箱单或旧合同。
        post('/api/reset')
        batch = post('/api/batch')['batch']['id']
        self.upload(batch, [('采购合同A.pdf', 'VERSION TWO')])
        second = post('/api/save', {'project_no': 'P1'})
        self.assertTrue(second['ok'], second)
        v2 = root / '合同/3-采购合同v2-P1.pdf'
        self.assertFalse(old.exists())
        self.assertEqual(v2.read_bytes(), b'VERSION TWO')
        self.assertEqual(unchanged.read_bytes(), b'PACK')
        self.assertEqual(unchanged.stat().st_mtime_ns, before_stat.st_mtime_ns)
        # 相同内容重传不升版。
        post('/api/save', {'project_no': 'P1'})
        self.assertEqual(len(list(root.rglob('*v2*.pdf'))), 1)
        self.assertEqual(len(list(root.rglob('*v3*.pdf'))), 0)
        post('/api/reset')
        batch = post('/api/batch')['batch']['id']
        self.upload(batch, [('采购合同A.pdf', 'VERSION THREE')])
        third = post('/api/save', {'project_no': 'P1'})
        self.assertTrue(third['ok'], third)
        self.assertFalse(v2.exists())
        self.assertEqual((root / '合同/3-采购合同v3-P1.pdf').read_bytes(), b'VERSION THREE')
        self.assertEqual(unchanged.read_bytes(), b'PACK')
        with urllib.request.urlopen('http://127.0.0.1:%d/api/export' % PORT) as response:
            zf = zipfile.ZipFile(io.BytesIO(response.read()))
        self.assertTrue(any(n.endswith('.xlsx') for n in zf.namelist()))
        self.assertFalse(any('v2' in n for n in zf.namelist()))

    def test_incremental_save_ignores_modified_unuploaded_history(self):
        self.upload(self.batch, [('采购合同A.pdf', 'CONTRACT'), ('装箱单.xlsx', 'PACK')])
        first = post('/api/save', {'project_no': 'P1'})
        self.assertTrue(first['ok'], first)
        root = Path(first['path'])
        historical = next(root.rglob('*.xlsx'))
        historical.write_bytes(b'EXTERNALLY SAVED PACK')
        before = historical.stat()
        ledger_before = json.loads((root / '编号记录.json').read_text(encoding='utf-8'))
        post('/api/reset')
        batch = post('/api/batch')['batch']['id']
        self.upload(batch, [('采购合同A.pdf', 'CONTRACT V2')])
        second = post('/api/save', {'project_no': 'P1'})
        self.assertTrue(second['ok'], second)
        self.assertEqual(second['saved'], 1)
        self.assertEqual(historical.read_bytes(), b'EXTERNALLY SAVED PACK')
        self.assertEqual(historical.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual((root / '合同/3-采购合同v2-P1.pdf').read_bytes(), b'CONTRACT V2')
        ledger_after = json.loads((root / '编号记录.json').read_text(encoding='utf-8'))
        history_key = next(k for k, u in ledger_before['units'].items() if u['type'] == '箱单')
        self.assertEqual(ledger_after['units'][history_key], ledger_before['units'][history_key])

    def test_reuploaded_file_preserves_modified_archive_and_saves_upload(self):
        self.upload(self.batch, [('装箱单.xlsx', 'PACK')])
        first = post('/api/save', {'project_no': 'P1'})
        self.assertTrue(first['ok'], first)
        root = Path(first['path'])
        historical = next(root.rglob('*.xlsx'))
        historical.write_bytes(b'EXTERNAL CHANGE')
        post('/api/reset')
        batch = post('/api/batch')['batch']['id']
        self.upload(batch, [('装箱单.xlsx', 'PACK')])
        second = post('/api/save', {'project_no': 'P1'})
        self.assertTrue(second['ok'], second)
        self.assertEqual(historical.read_bytes(), b'EXTERNAL CHANGE')
        copies = list(root.rglob('*.xlsx'))
        self.assertEqual(len(copies), 2)
        self.assertTrue(any(p.read_bytes() == b'PACK' for p in copies))
        ledger = json.loads((root / '编号记录.json').read_text(encoding='utf-8'))
        unit = next(u for u in ledger['units'].values() if u['type'] == '箱单')
        self.assertEqual((root.parent.parent / unit['path']).read_bytes(), b'PACK')
        third = post('/api/save', {'project_no': 'P1'})
        self.assertTrue(third['ok'], third)
        self.assertEqual(len(list(root.rglob('*.xlsx'))), 2)
        self.assertEqual(historical.read_bytes(), b'EXTERNAL CHANGE')


class TestSeed(WebTestBase):
    """没有实际内容时只按 UUID 分组：95 次上传 -> 72 个分组。"""

    def seed(self):
        return post('/api/seed', {})

    def test_seed_loads_fixture(self):
        r = self.seed()
        self.assertTrue(r.get('ok'), r)
        self.assertEqual(r['batches'], 8)
        self.assertEqual(r['files'], 95)
        s = get('/api/state')
        self.assertEqual(len(s['batches']), 8)
        # 每条单据的附件数应该是 9/6/8/7/7/7/25/26 这组，不是一条 95
        counts = sorted(len(b['files']) for b in s['batches'])
        self.assertEqual(counts, [6, 7, 7, 7, 8, 9, 25, 26], counts)

    def test_seed_matches_offline_baseline(self):
        self.seed()
        r = post('/api/analyze', {'project_no': 'B22026LD001',
                                  'purchaser': '深圳某单位'})
        s = r['summary']
        self.assertEqual(s['total_uploads'], 95)
        self.assertEqual(s['logical_files'], 72)
        # 三份合同模板不再作为正式合同归档。
        self.assertEqual(s['classified'], 47)
        self.assertEqual(s['manual'], 25)
        self.assertEqual(s['secret'], 0)
        self.assertEqual(s['unverified'], 72)

    def test_seed_replaces_not_appends(self):
        self.upload(self.batch, self.FILES)
        self.seed()
        s = get('/api/state')
        self.assertEqual(len(s['batches']), 8, '示例数据应该覆盖而不是追加')

    def test_seed_missing_file(self):
        r = post('/api/seed', {'path': 'fixtures/不存在的.json'})
        self.assertIn('error', r)

    def test_seed_export_gives_reports_only(self):
        """快照没有文件内容，zip 里只该有两份清单，不能假装有附件。"""
        self.seed()
        post('/api/analyze', {})
        req = urllib.request.Request('http://127.0.0.1:%d/api/export' % PORT,
                                     data=b'{}',
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            zf = zipfile.ZipFile(io.BytesIO(resp.read()))
        names = sorted(zf.namelist())
        self.assertEqual(names, ['清单.csv', '结果.md'], names)
        text = zf.read('清单.csv').decode('utf-8-sig')
        self.assertEqual(len(text.strip().splitlines()) - 1, 72,
                         '没有内容时仅按UUID分组，清单应保留72条')


class TestPortGuard(unittest.TestCase):
    """同一个端口不能被两个实例同时绑上。

    踩过的坑：http.server 默认开 SO_REUSEADDR，Windows 上第二个实例
    也能绑成功，于是两个进程各有一份内存状态、请求轮流落到两边，
    表现成「刚传的文件有时在有时不在」。所以要主动拒绝重复启动。
    """

    def test_second_instance_refuses_same_port(self):
        first = web.Server(('127.0.0.1', 0), web.Handler)
        port = first.server_address[1]
        try:
            with self.assertRaises(OSError):
                web.Server(('127.0.0.1', port), web.Handler)
        finally:
            first.server_close()

    def test_reuse_address_is_off(self):
        self.assertFalse(web.Server.allow_reuse_address)


class TestPageScript(unittest.TestCase):
    """页面的 JS 是内联的、没有构建步骤，写错一个字符整个页面就白屏，
    而接口测试照样全绿。所以这里单独用 node 做一次语法检查。"""

    def script(self):
        html = fetch_page('/')
        m = re.search(r'<script>(.*?)</script>', html, re.S)
        self.assertIsNotNone(m, '页面里找不到 <script> 块')
        return m.group(1)

    def test_js_parses(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('没装 node，跳过 JS 语法检查')
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, 'page.js')
            with io.open(path, 'w', encoding='utf-8') as fh:
                fh.write(self.script())
            p = subprocess.run([node, '--check', path],
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 0,
                             '页面 JS 语法错误：\n%s' % (p.stderr or p.stdout))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_emoji_in_page(self):
        """产物里不能有 emoji（平台约定）。"""
        html = fetch_page('/')
        bad = [c for c in html if ord(c) > 0xFFFF]
        self.assertEqual(bad, [], '页面里有 emoji：%s' % bad)


if __name__ == '__main__':
    unittest.main(verbosity=2)
