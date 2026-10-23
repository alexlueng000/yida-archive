# -*- coding: utf-8 -*-
"""用本地假服务器验证下载落盘机制（不需要真 token、不连宜搭）。

验的是：临时文件改名、同 uuid 只下一次、落盘清单、同名不覆盖。
真实附件地址要认证，这里替换成 127.0.0.1，其余流程完全一样。

跑法： python tests/test_download.py
"""

import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from yida_archive import archive, by_instance, config, fetch_cli
from yida_archive.config import Config
from yida_archive.models import AttachmentObs, Instance

PAYLOAD = ('附件内容 ' * 200).encode('utf-8')
HITS = {}
SEEN = {}          # 路径 -> 收到的请求头，用来验证发的是 Bearer 还是 Cookie


LOGIN_HTML = ('<html><head><title>钉钉登录</title></head>'
              '<body>请登录</body></html>').encode('utf-8')


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        name = self.path.lstrip('/')
        HITS[name] = HITS.get(name, 0) + 1
        SEEN[name] = dict(self.headers)
        if name == 'missing':
            self.send_response(404)
            self.end_headers()
            return
        if name == 'redirect':
            # 真实情况：没有有效登录态时附件地址 302 到钉钉登录页
            self.send_response(302)
            self.send_header('Location', 'https://login.dingtalk.com/oauth2/auth')
            self.end_headers()
            return
        if name == 'oss':
            # 真实情况：附件地址只是个中转，302 到对象存储，跟过去才是正文
            self.send_response(302)
            self.send_header('Location', '/file')
            self.end_headers()
            return
        if name == 'oss-loop':
            self.send_response(302)
            self.send_header('Location', '/oss-loop')
            self.end_headers()
            return
        if name == 'loginpage':
            # 有的登录页不给 302，直接 200 返回 HTML
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(LOGIN_HTML)))
            self.end_headers()
            self.wfile.write(LOGIN_HTML)
            return
        self.send_response(200)
        self.send_header('Content-Length', str(len(PAYLOAD)))
        self.end_headers()
        self.wfile.write(PAYLOAD)

    def log_message(self, *a):
        pass


_ENV_KEYS = ('YIDA_ACCESS_TOKEN', 'YIDA_COOKIE')
_ENV_SAVED = {}


def setUpModule():
    global SERVER, PORT
    # 本模块多个测试要临时设 YIDA_ACCESS_TOKEN / YIDA_COOKIE，之前的写法
    # 只在自己类里 pop，拦不住跨模块泄漏：unittest discover 按字母序跑，
    # test_download 排在 test_pipeline 前面，于是 test_pipeline 里
    # 「没凭证要报什么错」那条会拿到上个模块留下的值，反过来去连真地址
    # （实测报 HTTP 503）。单独跑 test_pipeline 是绿的，一起跑就红。
    # 这里整模块兜一次底，进出各还原一遍。
    for key in _ENV_KEYS:
        _ENV_SAVED[key] = os.environ.get(key)
    SERVER = HTTPServer(('127.0.0.1', 0), Handler)
    PORT = SERVER.server_address[1]
    threading.Thread(target=SERVER.serve_forever, daemon=True).start()


def tearDownModule():
    SERVER.shutdown()
    for key in _ENV_KEYS:
        if _ENV_SAVED.get(key) is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = _ENV_SAVED[key]


def att(name, uuid, url_name, size=len(PAYLOAD), field='附件'):
    return AttachmentObs(
        form_name='合同审批', form_uuid='F1', instance_id='I1',
        instance_title='T', submitted_at=1000, field_id='a1',
        field_label=field, name=name, size=size, file_uuid=uuid,
        url='http://127.0.0.1:%d/%s' % (PORT, url_name))


class TestNoTokenDoesNotSaveLoginPage(unittest.TestCase):
    """没有登录态时，绝对不能把登录页当附件存下来。

    踩过的坑：requests 默认跟着 302 跳转，附件地址会跳到钉钉登录页，
    拿到 200 的 HTML 后原样写进 xxx.pdf，日志还报「下载成功」。
    44 个文件全变成登录页，而且看起来一切正常。
    """

    def setUp(self):
        os.environ['YIDA_ACCESS_TOKEN'] = 'dummy-for-local-server'
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.cfg = Config({
            'yida': {'budget': 1, 'timeout': 10},
            'scope': {'project_no': 'P1', 'purchaser': '测试单位', 'project_secret': False},
            'storage': {'out_dir': os.path.join(self.tmp, 'out'),
                        'work_dir': os.path.join(self.tmp, 'work')},
        })
        self.client = fetch_cli.CliClient(self.cfg, app_type='APP_X')

    def test_redirect_is_error_not_saved_file(self):
        dest = os.path.join(self.tmp, '合同.pdf')
        with self.assertRaises(Exception) as cm:
            self.client.download('http://127.0.0.1:%d/redirect' % PORT, dest)
        self.assertIn('跳转', str(cm.exception))
        self.assertIn('login.dingtalk.com', str(cm.exception))
        self.assertFalse(os.path.exists(dest), '不该留下文件')

    def test_html_login_page_is_error_not_saved_file(self):
        dest = os.path.join(self.tmp, '合同.pdf')
        with self.assertRaises(Exception) as cm:
            self.client.download('http://127.0.0.1:%d/loginpage' % PORT, dest)
        self.assertIn('网页', str(cm.exception))
        self.assertFalse(os.path.exists(dest), '不该留下文件')

    def test_real_html_attachment_still_works(self):
        """附件本身就是 .html 的话，网页内容是正常的，不能误判。"""
        dest = os.path.join(self.tmp, '说明.html')
        n = self.client.download('http://127.0.0.1:%d/loginpage' % PORT, dest)
        self.assertEqual(n, len(LOGIN_HTML))

    def test_write_reports_it_as_failed(self):
        """整批跑的时候，这类失败要进 failed 而不是 done，且不留 .part。"""
        inst = Instance(form_name='货款支付', form_uuid='F2',
                        instance_id='I2', title='一次上传', submitted_at=1000)
        inst.attachments = [att('好.pdf', 'u1', 'u1'),
                            att('坏.pdf', 'u2', 'redirect')]
        planned = by_instance.plan([inst], self.cfg)
        done, failed = by_instance.write(planned, self.client)
        self.assertEqual(len(done), 1)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0][0].name, '坏.pdf')
        for root, _, files in os.walk(self.cfg.out_dir):
            for f in files:
                self.assertFalse(f.endswith('.part'), f)
                self.assertFalse(f.startswith('坏'), f)

    def test_oss_redirect_is_followed_not_treated_as_login(self):
        """附件地址 302 到 OSS 是正常的，跟过去才是真文件。

        踩过：为了不让登录页被存成 PDF，早期版本把**所有** 3xx 都当
        认证失败，结果有登录态时一个文件也下不到——附件地址本身就
        是个中转。所以只把已知登录域当失败。
        """
        dest = os.path.join(self.tmp, '合同.pdf')
        n = self.client.download('http://127.0.0.1:%d/oss' % PORT, dest)
        self.assertEqual(n, len(PAYLOAD))
        with open(dest, 'rb') as fh:
            self.assertEqual(fh.read(), PAYLOAD)

    def test_redirect_loop_gives_up(self):
        dest = os.path.join(self.tmp, 'x.pdf')
        with self.assertRaises(Exception) as cm:
            self.client.download('http://127.0.0.1:%d/oss-loop' % PORT, dest)
        self.assertIn('跳转', str(cm.exception))
        self.assertFalse(os.path.exists(dest))

    def test_referer_header_is_sent(self):
        """少了 Referer 接口只回 200 + 空响应，什么都不报，必须带上。"""
        SEEN.clear()
        self.client.download('http://127.0.0.1:%d/u1' % PORT,
                             os.path.join(self.tmp, 'a.bin'))
        self.assertTrue(SEEN['u1'].get('Referer'), 'Referer 没带上')

    def test_no_token_gives_actionable_message(self):
        """没设凭证时，报错要说清楚要设哪个环境变量。"""
        os.environ.pop('YIDA_ACCESS_TOKEN', None)
        os.environ.pop('YIDA_COOKIE', None)
        with self.assertRaises(Exception) as cm:
            self.client.download('http://127.0.0.1:%d/u1' % PORT,
                                 os.path.join(self.tmp, 'x.pdf'))
        self.assertIn('YIDA_ACCESS_TOKEN', str(cm.exception))


class TestCookieAuth(unittest.TestCase):
    """浏览器走的是 cookie 认证，不是 Bearer。

    实测（2026-09-23）：F12 里发往 front.aliwork.com 的请求头按字母排序，
    Accept-Language 下面直接就是 cookie，**中间没有 Authorization**。
    程序原来只发 Bearer，在这台机器上等于一个文件都下不下来。
    """

    def setUp(self):
        os.environ.pop('YIDA_ACCESS_TOKEN', None)
        os.environ.pop('YIDA_COOKIE', None)
        self.addCleanup(os.environ.pop, 'YIDA_ACCESS_TOKEN', None)
        self.addCleanup(os.environ.pop, 'YIDA_COOKIE', None)
        SEEN.clear()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.cfg = Config({
            'yida': {'budget': 1, 'timeout': 10},
            'scope': {'project_no': 'P1', 'purchaser': '测试单位', 'project_secret': False},
            'storage': {'out_dir': os.path.join(self.tmp, 'out'),
                        'work_dir': os.path.join(self.tmp, 'work')},
        })
        self.client = fetch_cli.CliClient(self.cfg, app_type='APP_X')

    def _download(self, url_name='u1'):
        return self.client.download('http://127.0.0.1:%d/%s' % (PORT, url_name),
                                    os.path.join(self.tmp, 'a.bin'))

    def test_cookie_is_sent_when_no_token(self):
        os.environ['YIDA_COOKIE'] = 'corp_id=ding92dcc27e50e36d1a; account=abc'
        n = self._download()
        self.assertEqual(n, len(PAYLOAD))
        sent = SEEN['u1']
        self.assertEqual(sent.get('Cookie'), os.environ['YIDA_COOKIE'])
        self.assertIsNone(sent.get('Authorization'), '不该凭空发 Bearer')

    def test_token_wins_when_both_set(self):
        os.environ['YIDA_ACCESS_TOKEN'] = 'tok-123'
        os.environ['YIDA_COOKIE'] = 'corp_id=x'
        self._download()
        sent = SEEN['u1']
        self.assertEqual(sent.get('Authorization'), 'Bearer tok-123')
        self.assertIsNone(sent.get('Cookie'))

    def test_no_credential_names_both_env_vars(self):
        with self.assertRaises(Exception) as cm:
            self._download()
        msg = str(cm.exception)
        self.assertIn('YIDA_ACCESS_TOKEN', msg)
        self.assertIn('YIDA_COOKIE', msg)

    def test_cli_mode_download_accepts_cookie(self):
        """cli 模式的下载原先只认 token，会把配了 cookie 的人直接拦掉。"""
        os.environ['YIDA_COOKIE'] = 'corp_id=x'
        self._download()

    def test_dirty_paste_is_cleaned(self):
        """手工粘贴常见的三种脏输入：带 `cookie:` 头名、带换行、首尾空白。"""
        self.assertEqual(config.clean_cookie('cookie: a=b; c=d'), 'a=b; c=d')
        self.assertEqual(config.clean_cookie('Cookie: a=b'), 'a=b')
        self.assertEqual(config.clean_cookie('  a=b; c=d\n'), 'a=b; c=d')
        self.assertEqual(config.clean_cookie('a=b\n  c=d'), 'a=b c=d')
        self.assertEqual(config.clean_cookie(''), '')
        self.assertEqual(config.clean_cookie(None), '')

    def test_dirty_paste_reaches_the_wire_clean(self):
        os.environ['YIDA_COOKIE'] = 'cookie: corp_id=x;\n  account=y\n'
        self._download()
        self.assertEqual(SEEN['u1'].get('Cookie'), 'corp_id=x; account=y')

    def test_referer_defaults_to_app_workbench_and_can_be_overridden(self):
        self.cfg.raw['yida']['app_type'] = 'APP_X'
        self.assertEqual(self.cfg.referer,
                         'https://www.aliwork.com/APP_X/workbench')
        self.cfg.raw['yida']['referer'] = 'https://example.com/'
        self.assertEqual(self.cfg.referer, 'https://example.com/')

    def test_query_also_uses_cookie(self):
        """取数接口同样要能用 cookie，不然只能下文件不能列单据。"""
        from yida_archive.fetch_yida import YidaClient
        os.environ['YIDA_COOKIE'] = 'corp_id=x'
        client = YidaClient(self.cfg, app_type='APP_X')
        self.assertEqual(client._headers().get('Cookie'), 'corp_id=x')


class TestDownload(unittest.TestCase):
    def setUp(self):
        os.environ['YIDA_ACCESS_TOKEN'] = 'dummy-for-local-server'
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.cfg = Config({
            'yida': {'budget': 1, 'timeout': 10},
            'scope': {'project_no': 'P1', 'purchaser': '测试单位', 'project_secret': False},
            'storage': {'out_dir': os.path.join(self.tmp, 'out'),
                        'work_dir': os.path.join(self.tmp, 'work')},
        })
        self.client = fetch_cli.CliClient(self.cfg, app_type='APP_X')

    def test_cli_client_downloads(self):
        """cli 模式下配了 token 就能下载。"""
        dest = os.path.join(self.tmp, 'a.bin')
        n = self.client.download('http://127.0.0.1:%d/a' % PORT, dest)
        self.assertEqual(n, len(PAYLOAD))
        with open(dest, 'rb') as fh:
            self.assertEqual(fh.read(), PAYLOAD)

    def test_write_lands_files_and_no_part_left(self):
        inst = Instance(form_name='合同审批', form_uuid='F1',
                        instance_id='I1', title='第一次上传', submitted_at=1000)
        inst.attachments = [att('合同.pdf', 'u1', 'u1'),
                            att('发票.pdf', 'u2', 'u2')]
        planned = by_instance.plan([inst], self.cfg)
        done, failed = by_instance.write(planned, self.client)
        self.assertEqual(len(done), 2)
        self.assertEqual(failed, [])
        for _, dest in done:
            self.assertTrue(os.path.exists(dest))
        # 不能留下 .part 临时文件
        for root, _, files in os.walk(self.cfg.out_dir):
            for f in files:
                self.assertFalse(f.endswith('.part'), f)

    def test_same_uuid_downloaded_once(self):
        """同一物理文件在两条单据里，只下载一次，第二次复制。"""
        HITS.clear()
        a = Instance(form_name='合同审批', form_uuid='F1', instance_id='I1',
                     title='第一次', submitted_at=1000)
        a.attachments = [att('合同.pdf', 'u1', 'shared')]
        b = Instance(form_name='货款支付', form_uuid='F2', instance_id='I2',
                     title='第二次', submitted_at=2000)
        b.attachments = [att('合同.pdf', 'u1', 'shared')]
        planned = by_instance.plan([a, b], self.cfg)
        done, _ = by_instance.write(planned, self.client)
        self.assertEqual(len(done), 2)
        self.assertEqual(HITS.get('shared'), 1, '同一 uuid 下了 %d 次'
                         % HITS.get('shared', 0))

    def test_failure_recorded_not_fatal(self):
        """一个文件 404 不能中断整批。"""
        inst = Instance(form_name='合同审批', form_uuid='F1',
                        instance_id='I1', title='T', submitted_at=1000)
        inst.attachments = [att('好.pdf', 'u1', 'u1'),
                            att('坏.pdf', 'u2', 'missing')]
        planned = by_instance.plan([inst], self.cfg)
        done, failed = by_instance.write(planned, self.client)
        self.assertEqual(len(done), 1)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0][0].name, '坏.pdf')

    def test_relative_attachment_url_gets_the_domain_added(self):
        """宜搭有的附件地址是 /ossFileHandle?... 这种相对路径，没带域名。

        不补域名的话请求根本发不出去（No scheme supplied），日志里看着
        像下载失败，其实地址是完整的、文件也在。实测 2026-10-09：
        4 个项目 63 个失败项 100% 都是这一种，都是合同类附件。
        """
        dest = os.path.join(self.tmp, 'rel.bin')
        # base_url 指向本地假服务器，补域名才补得对地方
        cfg = Config({
            'yida': {'budget': 1, 'timeout': 10,
                     'base_url': 'http://127.0.0.1:%d' % PORT},
            'scope': {'project_no': 'P1', 'purchaser': '测试单位', 'project_secret': False},
            'storage': {'out_dir': os.path.join(self.tmp, 'out'),
                        'work_dir': os.path.join(self.tmp, 'work')},
        })
        client = fetch_cli.CliClient(cfg, app_type='APP_X')
        n = client.download('/rel', dest)
        self.assertEqual(n, len(PAYLOAD))
        with open(dest, 'rb') as fh:
            self.assertEqual(fh.read(), PAYLOAD)

    def test_existing_file_is_not_downloaded_again(self):
        """断点续传：目标已存在且非空，第二次跑就不再拉一遍。

        重跑 6.4 GB 的抓取时这条决定流量：已经下好的不再碰网络。
        """
        HITS.clear()
        inst = Instance(form_name='合同审批', form_uuid='F1',
                        instance_id='I1', title='T', submitted_at=1000)
        inst.attachments = [att('合同.pdf', 'u1', 'resume')]
        planned = by_instance.plan([inst], self.cfg)
        done, failed = by_instance.write(planned, self.client)
        self.assertEqual((len(done), failed), (1, []))
        self.assertEqual(HITS.get('resume'), 1)
        done2, failed2 = by_instance.write(planned, self.client)
        self.assertEqual((len(done2), failed2), (1, []))
        self.assertEqual(HITS.get('resume'), 1, '已存在的文件又被下了一遍')

    def test_zero_byte_leftover_is_retried(self):
        """空文件不算「已下好」：中断留下的 0 字节必须重下。

        判据是「非空」不是「存在」，否则坏文件会被永远跳过。
        """
        HITS.clear()
        inst = Instance(form_name='合同审批', form_uuid='F1',
                        instance_id='I1', title='T', submitted_at=1000)
        inst.attachments = [att('合同.pdf', 'u1', 'zero')]
        planned = by_instance.plan([inst], self.cfg)
        dest = planned[0][1][0][1]
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        open(dest, 'wb').close()
        done, failed = by_instance.write(planned, self.client)
        self.assertEqual((len(done), failed), (1, []))
        self.assertEqual(HITS.get('zero'), 1, '空文件被当成下好了')
        self.assertEqual(os.path.getsize(dest), len(PAYLOAD))

    def test_archive_hash_and_manifest(self):
        """归类模式的下载 + 哈希 + 落盘清单。"""
        from yida_archive import classify, dedupe, naming

        inst = Instance(form_name='合同审批', form_uuid='F1',
                        instance_id='I1', title='T', submitted_at=1000)
        inst.attachments = [att('四方外贸合同.docx', 'u1', 'u1')]
        files, _ = dedupe.group([inst])
        archive.hash_all(files, self.client, self.cfg.work_dir)
        self.assertEqual(len(files[0].content_hash), 64)
        classify.classify(files, self.cfg)
        classify.mark_secret(files, self.cfg)
        naming.assign(files, self.cfg, 'P1')
        done, failed = archive.write(files, self.cfg, self.client,
                                     self.cfg.work_dir)
        self.assertEqual((len(done), len(failed)), (1, 0))
        self.assertTrue(done[0][1].endswith('四方外贸合同.docx')
                        or '外贸报关合同' in done[0][1])
        path = archive.write_manifest(done, failed, self.cfg,
                                      os.path.join(self.cfg.out_dir, 'm.tsv'))
        with io.open(path, encoding='utf-8') as fh:
            text = fh.read()
        self.assertIn('四方外贸合同.docx', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
