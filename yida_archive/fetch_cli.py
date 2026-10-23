# -*- coding: utf-8 -*-
"""通过 openyida CLI 取数，复用 CLI 已有的登录态。

为什么要有这条路径：服务器上装了 OpenYida 并登录过，就不必再单独配一个 token
给这个程序用。配置里 yida.auth_mode 设成 cli 即可。

注意：**下载仍然需要自己的登录凭证**（token 或浏览器 cookie）。CLI 没有附件
下载命令，附件地址不带认证会直接跳钉钉登录页。所以 cli 模式适合「列清单」，
真要落盘还是得配 token 或 cookie。
"""

import json
import os
import re
import subprocess
import sys
import time

from . import extract
from .models import AttachmentObs, Instance, as_millis

ANSI = re.compile(r'\x1b\[[0-9;]*m')
MAX_PAGES = 200


class CliError(RuntimeError):
    pass


def _recover_json(text):
    """CLI 的 stdout 前面可能有日志，把 JSON 抠出来。"""
    start = text.find('{')
    if start < 0:
        return {}
    for cut in range(0, len(text) - start, 3000):
        chunk = text[start:len(text) - cut] if cut else text[start:]
        try:
            return json.loads(chunk)
        except ValueError:
            continue
    return {}


class CliClient:
    def __init__(self, cfg, app_type='', cli='openyida'):
        self.cfg = cfg
        self.app_type = app_type
        self.cli = cli
        self.work_dir = os.path.abspath(cfg.work_dir)
        self.cond_file = os.path.join(self.work_dir, '_cond.json')

    # ---------- 调 CLI ----------
    def _run(self, args):
        os.makedirs(self.work_dir, exist_ok=True)
        cmd = ' '.join(args)
        try:
            proc = subprocess.run(cmd, shell=True, capture_output=True,
                                  text=True, encoding='utf-8',
                                  errors='replace', cwd=self.work_dir,
                                  timeout=self.cfg.timeout * 6)
        except subprocess.TimeoutExpired:
            raise CliError('openyida 命令超时：%s' % cmd)
        except FileNotFoundError:
            raise CliError('找不到 openyida 命令，确认它在 PATH 里。')
        out = ANSI.sub('', proc.stdout or '')
        if not out.strip():
            err = ANSI.sub('', proc.stderr or '').strip()
            raise CliError('openyida 没有输出。%s' % (err[:300] or cmd))
        return out

    def _query_page(self, form_cfg, page, size, search_file):
        kind = 'form' if form_cfg.get('kind') == 'form' else 'process'
        args = [self.cli, 'data', 'query', kind, self.app_type,
                form_cfg['form_uuid'], '--size', str(size),
                '--page', str(page)]
        if search_file:
            args += ['--search-file', '"%s"' % search_file]
        return _recover_json(self._run(args))

    # ---------- 取数 ----------
    def iter_records(self, form_cfg, project_no=None):
        """project_no 传 '' 表示不按项目过滤，语义同 fetch_yida。"""
        if project_no is None:
            project_no = self.cfg.project_no
        project_field = form_cfg.get('project_field') or ''
        search_field = project_field.split('.')[0]

        search_file = None
        if search_field and project_no:
            os.makedirs(self.work_dir, exist_ok=True)
            cond = [{'key': search_field, 'value': project_no, 'type': 'TEXT',
                     'operator': 'eq', 'componentName': 'TextField'}]
            with open(self.cond_file, 'w', encoding='utf-8') as fh:
                json.dump(cond, fh, ensure_ascii=False)
            search_file = self.cond_file

        size = self.cfg.page_size
        seen = set()
        for page in range(1, MAX_PAGES + 1):
            body = self._query_page(form_cfg, page, size, search_file)
            content = body.get('content', body)
            rows = content.get('data') or []
            if not rows:
                break
            for row in rows:
                inst_id = (row.get('processInstanceId')
                           or row.get('formInstId') or '')
                if inst_id in seen:
                    continue
                seen.add(inst_id)
                fd = row.get('formData') or row.get('data') or {}
                if project_no and not extract.matches_project(
                        fd, project_field, project_no):
                    continue
                yield row
            if len(rows) < size:
                break

    def iter_instances(self, form_cfg, project_no=None):
        field_map = form_cfg.get('field_labels') or {}
        for row in self.iter_records(form_cfg, project_no=project_no):
            fd = row.get('formData') or row.get('data') or {}
            inst = Instance(
                form_name=form_cfg.get('name', ''),
                form_uuid=form_cfg['form_uuid'],
                instance_id=(row.get('processInstanceId')
                             or row.get('formInstId') or ''),
                title=row.get('title') or '',
                submitted_at=as_millis(row.get('gmtCreate')),
                originator=_person(row.get('originator')),
                status=row.get('instanceStatus') or '',
            )
            for path, item in extract.find_attachments(fd):
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
        """CLI 没有下载命令，这里退回带凭证的 requests。"""
        if not (self.cfg.access_token or self.cfg.cookie):
            yida = self.cfg.raw.get('yida') or {}
            raise CliError(
                '下载附件需要登录凭证：OpenYida 没有下载命令，附件地址不带认证会跳登录页。'
                '请设置环境变量 %s（token）或 %s（浏览器 cookie）后再跑 --write；'
                '只列清单的话不加 --write 即可。'
                % (yida.get('access_token_env', 'YIDA_ACCESS_TOKEN'),
                   yida.get('cookie_env', 'YIDA_COOKIE')))
        from .fetch_yida import YidaClient
        client = YidaClient(self.cfg, app_type=self.app_type)
        return client.download(url, dest_path)


def _person(org):
    if not org:
        return ''
    name = org.get('name')
    if isinstance(name, dict):
        return (name.get('zh_CN') or name.get('pureEn_US')
                or name.get('en_US') or '')
    return name if isinstance(name, str) else ''
