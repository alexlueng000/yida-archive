# -*- coding: utf-8 -*-
"""诊断登录凭证到底有没有被宜搭接口接受。

在**你自己设好 YIDA_COOKIE / YIDA_ACCESS_TOKEN 的那个窗口里**跑：

    py probe_auth.py --config config.pay.yaml

因为环境变量只对设它的那个窗口有效，换个窗口跑就又是空的。

为什么需要它：这个接口在凭证不被接受时**不报错也不跳转**，直接返回
HTTP 200 + 空响应（实测）。程序只能说「返回的不是 JSON」，
分不清到底是 cookie 抄漏了、域名不对、还是缺 csrf。
这个脚本把三种可能一次问完。

只输出状态码、Content-Type、跳转目标和响应长度，**不打印任何凭证**，
输出可以直接贴给人看。
"""

import argparse
import json
import os
import sys
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    raise SystemExit('缺少依赖 requests。先执行：pip install -r requirements.txt')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from yida_archive.config import load                      # noqa: E402
from yida_archive.fetch_yida import YidaClient            # noqa: E402

# 浏览器全程在 front.aliwork.com 上，配置里的 base_url 是 www.aliwork.com。
# 这两个到底哪个能收这份凭证，是这次要问清楚的问题之一。
HOSTS = ['https://www.aliwork.com', 'https://front.aliwork.com']
PATH = '/dingtalk/web/{app}/v1/process/getInstances.json'


def describe(resp):
    """把一次响应浓缩成几行，够判断就行，不打印任何凭证。"""
    lines = ['  HTTP %s' % resp.status_code]
    loc = resp.headers.get('Location')
    if loc:
        lines.append('  跳转到        %s' % (urlparse(loc).netloc or loc[:80]))
    ctype = resp.headers.get('Content-Type') or '(无)'
    lines.append('  Content-Type  %s' % ctype)
    lines.append('  响应长度      %d 字节' % len(resp.content))
    low = ctype.lower()
    if 'json' in low:
        preview = resp.text[:120].replace('\n', ' ')
        lines.append('  开头          %s' % preview)
        lines.append('  → 通了，这份凭证这个域认')
    elif 'html' in low:
        lines.append('  → 返回的是网页（登录页或错误页），不是接口数据')
    else:
        lines.append('  → 空响应。这个接口不认凭证时就是这个样子')

    if resp.status_code == 200 and b'"success":true' in resp.content:
        lines.append('  → 接口明确回了 success=true，取数没问题')
    return '\n'.join(lines)


def one(cfg, host, params, headers, label):
    print('%s  [%s]' % (host, label))
    try:
        resp = requests.get(host + PATH.format(app=cfg.app_type),
                            params=params, headers=headers,
                            timeout=cfg.timeout, allow_redirects=False)
    except Exception as exc:
        print('  请求失败：%s' % exc)
        return None
    print(describe(resp))
    print('')
    return resp


def main():
    ap = argparse.ArgumentParser(description='诊断宜搭登录凭证')
    ap.add_argument('--config', default='config.yaml')
    args = ap.parse_args()

    cfg = load(args.config)
    print('配置文件      %s' % args.config)
    print('appType       %s' % (cfg.app_type or '(没配)'))

    if cfg.access_token:
        print('凭证          Bearer token（%d 字符）' % len(cfg.access_token))
    elif cfg.cookie:
        print('凭证          Cookie（%d 字符）' % len(cfg.cookie))
    else:
        print('凭证          没有。先设 YIDA_ACCESS_TOKEN 或 YIDA_COOKIE。')
        print('              （注意：环境变量只对设它的那个窗口有效）')
        return 2

    print('CSRF          %s' % (
        '有（%d 字符）' % len(cfg.csrf_token) if cfg.csrf_token
        else '没有 —— cookie 认证时多半是必需的，建议补上 YIDA_CSRF_TOKEN'))

    forms = cfg.forms or []
    if not forms:
        print('\n配置里没有 scope.forms，没法试接口。')
        return 2
    form = forms[0]

    client = YidaClient(cfg, app_type=cfg.app_type)
    try:
        headers = client._headers()
    except Exception as exc:
        print('\n凭证没准备好：%s' % exc)
        return 2

    params = client._common({
        'formUuid': form.get('form_uuid', ''),
        'currentPage': '1',
        'pageSize': '10',
        'searchFieldJson': json.dumps(
            {form.get('project_field', ''): cfg.project_no},
            ensure_ascii=False) if form.get('project_field') else '{}',
    })

    print('\n表单          %s  (%s)' % (form.get('name', ''),
                                        form.get('form_uuid', '')))
    print('项目号        %s' % cfg.project_no)
    print('')

    has_csrf = '_csrf_token' in params
    results = {}
    for host in HOSTS:
        results[host] = one(cfg, host, params, headers,
                            '带 csrf' if has_csrf else '没 csrf')

    if has_csrf:
        bare = dict(params)
        bare.pop('_csrf_token')
        print('--- 去掉 _csrf_token 再试一遍 ---\n')
        for host in HOSTS:
            one(cfg, host, bare, headers, '不带 csrf')

    print('=' * 60)
    ok = [h for h, r in results.items()
          if r is not None and r.status_code == 200 and r.content]
    if ok:
        print('结论：%s 能取到数据。把配置里的 yida.base_url 改成它。' % ok[0])
    elif not has_csrf:
        print('结论：CSRF 是空的，两个域都没取到数据 —— 但这次等于没测到\n'
              '      有用的信息，因为缺的可能正是 csrf。\n'
              '      先把 csrf 设上再跑一遍：\n'
              '          set YIDA_CSRF_TOKEN=<浏览器 cookie 里 tianshu_csrf_token 的值>\n'
              '      它和请求 URL 里的 _csrf_token 是同一个值。')
    else:
        print('结论：带着 csrf 两个域也都没取到数据。凭证大概率没抄全或者\n'
              '      已经过期，重新到浏览器 F12 → 标头 → 请求标头 → cookie\n'
              '      那行右键「复制值」，整串重新设一遍 YIDA_COOKIE。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
