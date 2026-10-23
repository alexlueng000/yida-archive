# -*- coding: utf-8 -*-
"""从宜搭拉取流程实例和附件元数据。

接口（与 OpenYida CLI 调用的一致）：
  流程表单  GET /dingtalk/web/{appType}/v1/process/getInstances.json
  普通表单  GET /dingtalk/web/{appType}/v1/form/searchFormDatas.json
认证：Authorization: Bearer <access_token>
公共参数：_api=nattyFetch, _mock=false, _csrf_token, _locale_time_zone_offset, _stamp
"""

import json
import os
import time
from urllib.parse import urlparse, urljoin

import requests

from . import extract
from .models import AttachmentObs, Instance, as_millis

PROCESS_PATH = '/dingtalk/web/{app}/v1/process/getInstances.json'
FORM_PATH = '/dingtalk/web/{app}/v1/form/searchFormDatas.json'
MAX_PAGES = 200


class YidaError(RuntimeError):
    pass


def _is_html_target(path):
    """目标文件本身就是 html/htm 的话，返回网页内容是正常的，不算登录页。"""
    return os.path.splitext(path)[1].lower() in ('.html', '.htm')


# 没有登录态时会被跳到这里。有登录态时也会跳转（附件跳去 OSS 取真文件），
# 那种跳转是正常的。区分这两种是下载能不能成功的关键。
LOGIN_HOSTS = ('login.dingtalk.com', 'oauth.dingtalk.com',
               'account.dingtalk.com', 'login.aliwork.com',
               'yidalogin.aliwork.com')


def _login_redirect(location):
    """这个跳转是不是「没登录」的意思。"""
    if not location:
        return False
    host = urlparse(location).netloc.lower().split(':')[0]
    return host in LOGIN_HOSTS or host.startswith(('login.', 'yidalogin.'))


class YidaClient:
    def __init__(self, cfg, session=None, app_type=''):
        self.cfg = cfg
        self.app_type = app_type
        self.session = session or requests.Session()
        self.base_url = cfg.base_url
        self.token = cfg.access_token
        self.cookie = cfg.cookie
        self.csrf = cfg.csrf_token
        self.referer = cfg.referer
        self.timeout = cfg.timeout

    # ---------- 底层请求 ----------
    def _common(self, extra):
        params = {
            '_api': 'nattyFetch',
            '_mock': 'false',
            '_stamp': str(int(time.time() * 1000)),
            '_locale_time_zone_offset': '28800000',
        }
        if self.csrf:
            params['_csrf_token'] = self.csrf
        params.update(extra)
        return params

    def _headers(self):
        """凭证优先级：Bearer token > 整条 Cookie。

        实测（2026-09-23）浏览器发往 front.aliwork.com 的请求里**没有
        Authorization 头**，全靠 cookie 认证。附件地址也在同一个域下，
        所以 cookie 这条路同样能下到文件，不能只认 Bearer。
        """
        headers = {
            'Accept': 'application/json, text/plain, */*',
            # 这个头不能少。少了它接口返回 200 + 空响应，什么都不报。
            'Referer': self.referer,
        }
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
            return headers
        if self.cookie:
            headers['Cookie'] = self.cookie
            return headers
        yida = self.cfg.raw.get('yida') or {}
        raise YidaError(
            '没有拿到登录凭证。设置环境变量 %s（Bearer token）或 %s'
            '（浏览器 F12 里整条 cookie 头的值）任意一个即可。'
            % (yida.get('access_token_env', 'YIDA_ACCESS_TOKEN'),
               yida.get('cookie_env', 'YIDA_COOKIE')))

    def get(self, path, params):
        url = self.base_url + path
        resp = self.session.get(url, params=self._common(params),
                                headers=self._headers(),
                                timeout=self.timeout,
                                allow_redirects=False)
        return self._unwrap(resp, url)

    def _unwrap(self, resp, url):
        """失败时把状态码、跳转目标、Content-Type 都说出来。

        之前只报「返回不是 JSON」，看不到到底发生了什么——是 401、
        还是跳去了登录页、还是跳去了别的域，全靠猜。这三样信息
        决定了下一步该改什么，不能省。
        """
        code = resp.status_code
        if code in (301, 302, 303, 307, 308):
            loc = resp.headers.get('Location', '')
            host = urlparse(loc).netloc or (loc[:80] or '未知地址')
            raise YidaError(
                '接口跳转到 %s（HTTP %d），说明登录凭证没被接受：%s\n'
                '常见原因：cookie 抄漏了一段、cookie 已过期、'
                '或者该接口不认这份凭证。' % (host, code, url))
        if code in (401, 403):
            raise YidaError(
                '认证失败(HTTP %d)：凭证无效或过期，或没有该数据的权限：%s'
                % (code, url))
        if code != 200:
            raise YidaError('HTTP %d：%s' % (code, url))
        try:
            body = resp.json()
        except ValueError:
            ctype = resp.headers.get('Content-Type') or '未知'
            raise YidaError(
                '接口返回的不是 JSON（HTTP %d，Content-Type: %s）：%s\n'
                '多半是凭证没被接受，服务器回了登录页或错误页。'
                % (code, ctype, url))
        if body.get('success') is False:
            raise YidaError('接口报错：%s'
                            % (body.get('errorMsg') or body.get('message')))
        return body.get('content', body)

    # ---------- 业务查询 ----------
    def query_page(self, form_cfg, page, page_size, search_json):
        params = {
            'formUuid': form_cfg['form_uuid'],
            'currentPage': str(page),
            'pageSize': str(page_size),
            'searchFieldJson': search_json,
        }
        kind = form_cfg.get('kind', 'process')
        path = (FORM_PATH if kind == 'form' else PROCESS_PATH).format(
            app=self.app_type)
        return self.get(path, params)

    def iter_records(self, form_cfg, project_no=None):
        """把一个表单里的单据翻完。

        project_no 传 '' 表示不按项目过滤（盘点全部项目时用），
        传 None 表示用配置里的 scope.project_no。
        """
        if project_no is None:
            project_no = self.cfg.project_no
        project_field = form_cfg.get('project_field') or ''
        search_field = project_field.split('.')[0]
        search_json = json.dumps(
            {search_field: project_no} if (search_field and project_no) else {},
            ensure_ascii=False)
        page_size = self.cfg.page_size

        seen = set()
        for page in range(1, MAX_PAGES + 1):
            content = self.query_page(form_cfg, page, page_size, search_json)
            rows = content.get('data') or []
            if not rows:
                break
            for row in rows:
                inst_id = (row.get('processInstanceId')
                           or row.get('formInstId') or row.get('instId') or '')
                if inst_id in seen:
                    continue
                seen.add(inst_id)
                form_data = row.get('formData') or row.get('data') or {}
                if project_no and not extract.matches_project(
                        form_data, project_field, project_no):
                    continue
                yield row
            if len(rows) < page_size:
                break

    def iter_instances(self, form_cfg, project_no=None):
        """产出 Instance，附件已经抽好。project_no 语义同 iter_records。"""
        labels = form_cfg.get('field_labels') or {}
        field_map = form_cfg.get('field_map') or labels
        for row in self.iter_records(form_cfg, project_no=project_no):
            form_data = row.get('formData') or row.get('data') or {}
            originator = ''
            org = row.get('originator') or {}
            name = org.get('name')
            if isinstance(name, dict):
                originator = (name.get('zh_CN') or name.get('pureEn_US')
                              or name.get('en_US') or '')
            elif isinstance(name, str):
                originator = name

            inst = Instance(
                form_name=form_cfg.get('name', ''),
                form_uuid=form_cfg['form_uuid'],
                instance_id=(row.get('processInstanceId')
                             or row.get('formInstId') or ''),
                title=row.get('title') or '',
                submitted_at=as_millis(row.get('gmtCreate')),
                originator=originator,
                status=row.get('instanceStatus') or '',
            )
            for path, item in extract.find_attachments(form_data):
                fid = extract.field_id_of(path)
                inst.attachments.append(AttachmentObs(
                    form_name=inst.form_name,
                    form_uuid=inst.form_uuid,
                    instance_id=inst.instance_id,
                    instance_title=inst.title,
                    submitted_at=inst.submitted_at,
                    field_id=fid,
                    field_label=field_map.get(fid, fid),
                    name=item.get('name') or '',
                    size=item.get('size'),
                    file_uuid=item.get('fileUuid') or '',
                    url=item.get('downloadUrl') or item.get('url') or '',
                ))
            yield inst

    # ---------- 下载 ----------
    def download(self, url, dest_path):
        """下载附件到本地，返回字节数。

        两种跳转要分开对待，这是踩了两轮才弄清楚的：

        - **跳到登录页** = 没有有效登录态。跟过去会拿到 200 的登录页
          HTML，然后被当成附件存下来——44 个 PDF 全变成登录页，日志里
          还写着「下载成功」。这种必须当失败。
        - **跳到 OSS** = 正常。附件地址只是个中转，真文件在后面的对象
          存储上（实测：302 之后跟过去才是 application/pdf 的正文）。

        所以既不能一律不跟（那样一个文件都下不到），也不能一律跟
        （那样没登录态时会把登录页存成 PDF）。只认已知的登录域。
        """
        if not url:
            raise YidaError('附件没有下载地址')

        # 部分附件记录的地址是相对路径（/ossFileHandle?...），平台没给域名。
        # 不补的话请求根本没发出去就报 "No scheme supplied"，
        # 看起来像下载失败，其实是地址不完整。实测 2026-10-09：
        # 4 个项目 63 个失败项 100% 都是这一种，都是合同类附件。
        if url.startswith('/'):
            url = self.base_url + url

        headers = self._headers()
        current = url
        resp = None
        try:
            for _ in range(5):
                resp = self.session.get(current, headers=headers,
                                        timeout=self.timeout, stream=True,
                                        allow_redirects=False)
                if resp.status_code not in (301, 302, 303, 307, 308):
                    break
                loc = resp.headers.get('Location', '')
                host = urlparse(loc).netloc or (loc[:60] or '未知地址')
                if _login_redirect(loc):
                    yida = self.cfg.raw.get('yida') or {}
                    raise YidaError(
                        '附件地址跳转到 %s（登录页），说明当前没有可用的登录态。'
                        '确认环境变量 %s（Bearer token）或 %s（cookie）'
                        '已设置且没过期。'
                        % (host,
                           yida.get('access_token_env', 'YIDA_ACCESS_TOKEN'),
                           yida.get('cookie_env', 'YIDA_COOKIE')))
                resp.close()
                current = urljoin(current, loc)
            else:
                raise YidaError('附件地址连续跳转超过 5 次，放弃：%s' % url)

            if resp.status_code in (401, 403):
                raise YidaError('下载被拒(%d)，凭证可能无该文件权限'
                                % resp.status_code)
            if resp.status_code != 200:
                raise YidaError('下载失败 HTTP %d' % resp.status_code)

            # 再防一手：有的登录页不给 302，直接 200 返回 HTML
            ctype = (resp.headers.get('Content-Type') or '').lower()
            if 'text/html' in ctype and not _is_html_target(dest_path):
                raise YidaError(
                    '下到的是网页而不是文件（Content-Type: %s），'
                    '多半是登录页。检查凭证是否有效。' % ctype)

            total = 0
            with open(dest_path, 'wb') as fh:
                for chunk in resp.iter_content(64 * 1024):
                    if chunk:
                        fh.write(chunk)
                        total += len(chunk)
            return total
        finally:
            if resp is not None:
                resp.close()
