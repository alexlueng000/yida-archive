# -*- coding: utf-8 -*-
"""探针：确认企业内部应用凭证能走通 拉实例 + 拿附件 + 下载 这条链路。

只做四件事，每一步独立报结论：
  1. 用 AppKey/AppSecret 换 access_token
  2. 按审批完成时间拉「合同审批」的流程实例
  3. 取其中一个附件，换临时免登地址
  4. 下载下来核对字节数

不打印任何凭证和签名地址：凭证只显示头尾掩码，地址只显示域名。
跑完输出可以直接贴给人看。

    python tools/probe_openapi.py
    python tools/probe_openapi.py --days 30 --form FORM-xxx
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from yida_archive import fetch_openapi as api          # noqa: E402

CONTRACT_FORM = 'FORM-HT866U91JVN4V3LWAWXX5AYC8UG63K2N7EM9LQZ5'
OUT_DIR = os.path.join(api.PROJECT_ROOT, '.cache', 'probe-openapi')


def step(name):
    print()
    print('=' * 60)
    print(name)
    print('=' * 60)


def mask_url(url):
    """签名地址带临时凭证，只报域名，确认换到了地址就行。"""
    from urllib.parse import urlparse
    parsed = urlparse(url or '')
    return '%s://%s（长度 %d）' % (parsed.scheme or '?', parsed.netloc or '?', len(url or ''))


def find_urls(payload, depth=0):
    """临时地址接口返回结构不固定，递归捞所有 http 字符串。"""
    out = []
    if depth > 6:
        return out
    if isinstance(payload, str):
        if payload.startswith('http://') or payload.startswith('https://'):
            out.append(payload)
    elif isinstance(payload, list):
        for item in payload:
            out.extend(find_urls(item, depth + 1))
    elif isinstance(payload, dict):
        for value in payload.values():
            out.extend(find_urls(value, depth + 1))
    return out


def probe_credentials():
    step('1/5 读凭证')
    files = api.load_env()
    print('读到的 .env：%s' % ('；'.join(files) if files else '（没有 .env，只认环境变量）'))
    values, missing = api.credentials()
    for name in api.REQUIRED:
        value = values.get(name, '')
        print('  %-13s %s  len=%d' % (name, api.mask(value), len(value)))
    print('  %-13s %s' % ('app_type', values.get('app_type') or '(空)'))
    if missing:
        print()
        print('缺凭证：%s' % '、'.join(missing))
        return False
    return True


def probe_token():
    step('2/5 换 access_token')
    try:
        token = api.access_token(force=True)
    except api.OpenApiError as exc:
        print('失败：%s' % exc)
        print('常见原因：AppKey/AppSecret 抄错，或开放平台的应用还没发布版本。')
        return False
    print('成功，token %s  len=%d' % (api.mask(token), len(token)))
    return True


def probe_param_names(client, form_uuid):
    """逐个试参数名。平台对不认识的参数静默忽略，只能看结果变不变来判断。"""
    print()
    print('参数名对照（不认识的会被静默忽略）')
    today = dt.date.today()
    tomorrow = today + dt.timedelta(days=1)
    fmt = lambda d: d.strftime('%Y-%m-%d')          # noqa: E731
    base = {'instanceStatus': 'COMPLETED', 'approvedResult': 'agree'}

    def show(label, **extra):
        # 直接拼 body，避免和 process_instances 自己的 page/size 参数撞名
        body = {
            'appType': client.app_type, 'systemToken': client.system_token,
            'userId': client.user_id, 'formUuid': form_uuid, 'language': 'zh_CN',
            'pageNumber': 1, 'pageSize': 10,
        }
        body.update({k: v for k, v in dict(base, **extra).items() if v not in (None, '')})
        try:
            payload = client.call(api.INSTANCES_PATH, body)
        except api.OpenApiError as exc:
            print('  %-28s 失败：%s' % (label, exc))
            return
        records = api.find_records(payload)
        print('  %-28s totalCount=%-6s 返回=%-3d 首条=%s' % (
            label, payload.get('totalCount'), len(records),
            api.instance_id_of(records[0])[:8] if records else '-'))

    show('基线 数字 1/10')
    show('pageSize=45 数字', pageSize=45)
    show('pageSize="45" 字符串', pageSize='45')
    show('pageNumber=2 数字 + pageSize=45', pageNumber=2, pageSize=45)
    show('pageNumber="2" 字符串 + pageSize="45"', pageNumber='2', pageSize='45')
    show('当前页 2、每页 10 都写字符串', pageNumber='2', pageSize='10')
    show('pageIndex=2', pageIndex=2)
    show('offset=10', offset=10)
    for key in ('createFromTimeGMT', 'createFromGMT', 'createFrom'):
        show('%s=明天' % key, **{key: fmt(tomorrow), 'createToTimeGMT': fmt(tomorrow)})
    show('createFromTimeGMT+createToTimeGMT=明天',
         createFromTimeGMT=fmt(tomorrow), createToTimeGMT=fmt(tomorrow))
    show('createFromTimeGMT=今天/createToTimeGMT=今天',
         createFromTimeGMT=fmt(today), createToTimeGMT=fmt(today))
    show('modifiedFromTimeGMT=今天/modifiedToTimeGMT=明天',
         modifiedFromTimeGMT=fmt(today), modifiedToTimeGMT=fmt(tomorrow))
    show('modified 今天00:00-23:59 时分秒',
         modifiedFromTimeGMT=fmt(today) + ' 00:00:00',
         modifiedToTimeGMT=fmt(today) + ' 23:59:59')
    show('小窗口 第1页', modifiedFromTimeGMT=fmt(today), modifiedToTimeGMT=fmt(tomorrow))
    show('小窗口 第2页', modifiedFromTimeGMT=fmt(today), modifiedToTimeGMT=fmt(tomorrow),
         pageNumber=2)
    show('小窗口 第2页 字符串', modifiedFromTimeGMT=fmt(today), modifiedToTimeGMT=fmt(tomorrow),
         pageNumber='2')


def probe_timeframe(client, form_uuid, days):
    """确认两件事：时间参数按 GMT 还是北京时间解释；切片求和能否覆盖全量。"""
    print()
    print('时间参数口径与切片覆盖')

    def fmt_dt(value):
        return value.strftime('%Y-%m-%d %H:%M:%S') if isinstance(value, dt.datetime) else value

    def count(frm, to):
        frm, to = fmt_dt(frm), fmt_dt(to)
        body = {
            'appType': client.app_type, 'systemToken': client.system_token,
            'userId': client.user_id, 'formUuid': form_uuid, 'language': 'zh_CN',
            'instanceStatus': 'COMPLETED', 'approvedResult': 'agree',
            'modifiedFromTimeGMT': frm, 'modifiedToTimeGMT': to,
            'pageNumber': 1, 'pageSize': 10,
        }
        payload = client.call(api.INSTANCES_PATH, body)
        return payload.get('totalCount'), api.find_records(payload)

    # 今天 12:50Z 有一条审批完成的记录：按 GMT 解释则该落在 12:45-12:55，按北京时间则落在 20:45-20:55
    for label, frm, to in (
            ('GMT 口径 12:45-12:55', '2026-10-28 12:45:00', '2026-10-28 12:55:00'),
            ('北京口径 20:45-20:55', '2026-10-28 20:45:00', '2026-10-28 20:55:00')):
        total, records = count(frm, to)
        print('  %-22s totalCount=%s' % (label, total))

    today = dt.date.today()
    start = today - dt.timedelta(days=days)
    whole, _ = count(start.strftime('%Y-%m-%d 00:00:00'), today.strftime('%Y-%m-%d 23:59:59'))
    print('  近 %d 天整段 totalCount=%s' % (days, whole))

    calls = [0]

    def walk(frm, to):
        """只按 totalCount 切，切到 <=10 就能一次拿全。"""
        total, _ = count(frm, to)
        calls[0] += 1
        if total <= 10:
            return total
        mid = frm + (to - frm) / 2
        got = walk(frm, mid) + walk(mid, to)
        return got

    span_from = dt.datetime.combine(start, dt.time(0, 0, 0))
    span_to = dt.datetime.combine(today, dt.time(23, 59, 59))
    got = walk(span_from, span_to)
    fmt = lambda d: d.strftime('%Y-%m-%d %H:%M:%S')   # noqa: E731
    print('  切片求和=%s  调用=%d 次  与整段一致=%s' % (got, calls[0], got == whole))


def probe_inspect(client, form_uuid, days):
    """查清三件事：筛选条件是否真的生效、分页怎么翻、时间字段叫什么。

    夜间任务靠这三点决定取值方式，错一个就会漏单或多拉几千条。
    """
    step('检查：筛选、分页、时间字段')
    today = dt.date.today()
    start = today - dt.timedelta(days=days)
    tomorrow = today + dt.timedelta(days=1)
    fmt = lambda d: d.strftime('%Y-%m-%d')          # noqa: E731

    base = {'instanceStatus': 'COMPLETED', 'approvedResult': 'agree'}
    cases = [
        ('无筛选', {}),
        ('仅状态+结果', dict(base)),
        ('近 %d 天 create' % days, dict(base, createFrom=fmt(start), createTo=fmt(today))),
        ('今天 create', dict(base, createFrom=fmt(today), createTo=fmt(today))),
        ('明天起 create', dict(base, createFrom=fmt(tomorrow), createTo=fmt(tomorrow))),
        ('近 %d 天 modified' % days, dict(base, modifiedFrom=fmt(start), modifiedTo=fmt(today))),
        ('今天 modified', dict(base, modifiedFrom=fmt(today), modifiedTo=fmt(today))),
    ]
    for label, filters in cases:
        try:
            payload = client.process_instances(form_uuid, page=1, size=10, **filters)
        except api.OpenApiError as exc:
            print('%-20s 失败：%s' % (label, exc))
            continue
        records = api.find_records(payload)
        print('%-20s totalCount=%-6s 返回=%-3d 首条=%s' % (
            label, payload.get('totalCount'), len(records),
            api.instance_id_of(records[0])[:8] if records else '-'))

    print()
    print('分页：pageSize 是否听指挥')
    for size in (5, 10, 100):
        payload = client.process_instances(form_uuid, page=1, size=size, **base)
        print('  pageSize=%-4d 返回 %d 条' % (size, len(api.find_records(payload))))
    first = api.find_records(client.process_instances(form_uuid, page=1, size=10, **base))
    second = api.find_records(client.process_instances(form_uuid, page=2, size=10, **base))
    print('  page1 首条=%s  page2 首条=%s  是否不同=%s' % (
        api.instance_id_of(first[0])[:8] if first else '-',
        api.instance_id_of(second[0])[:8] if second else '-',
        bool(first) and bool(second) and api.instance_id_of(first[0]) != api.instance_id_of(second[0])))

    probe_param_names(client, form_uuid)
    probe_timeframe(client, form_uuid, days)
    print()
    print('时间字段长什么样')
    if first:
        record = first[0]
        print('  记录键：%s' % ', '.join(sorted(record)[:14]))
        for key in ('createTimeGMT', 'modifiedTimeGMT', 'gmtCreate', 'gmtModified'):
            if key in record:
                print('  %-16s 原值=%r  解析=%s' % (key, record[key], dt_of(record[key])))


def dt_of(value):
    """把毫秒时间戳或日期字符串转成看得懂的本地时间。"""
    if value in (None, ''):
        return '-'
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return str(value)
    if stamp > 1e11:
        stamp /= 1000.0
    return dt.datetime.fromtimestamp(stamp).strftime('%Y-%m-%d %H:%M:%S')


def probe_instances(client, form_uuid, days):
    step('3/5 拉流程实例（近 %d 天，审批通过）' % days)
    today = dt.date.today()
    start = today - dt.timedelta(days=days)
    filters = {
        'instanceStatus': 'COMPLETED',
        'approvedResult': 'agree',
        'createFrom': start.strftime('%Y-%m-%d'),
        'createTo': today.strftime('%Y-%m-%d'),
    }
    print('条件：%s' % filters)
    try:
        payload = client.process_instances(form_uuid, page=1, size=5, **filters)
    except api.OpenApiError as exc:
        print('失败：%s' % exc)
        if exc.payload:
            print('响应原文：%s' % json.dumps(exc.payload, ensure_ascii=False)[:600])
        denied = exc.status == 403 or 'PermissionDenied' in (exc.code or '')
        if denied:
            print()
            print('处理办法：')
            print('  1. 钉钉开放平台（open-dev.dingtalk.com）→ AppKey 为 dingey24... 的那个应用 → 权限管理')
            print('     搜 Yida.Process.Read，状态要变成「已开通」。宜搭应用设置里的「权限管理」是另一回事，点那里没用')
            print('  2. 同一应用 → 版本管理与发布，发布一次；不发布权限不生效')
            print('  3. 发布后等一两分钟，再跑本探针（探针每次都强制换新 token）')
        else:
            print('响应顶层键：%s' % ', '.join(sorted(exc.payload)[:12]) if exc.payload else '（空响应）')
        return None
    records = api.find_records(payload)
    print('返回记录 %d 条' % len(records))
    for record in records:
        print('  %s  %s  状态=%s  结果=%s' % (
            api.instance_id_of(record),
            (record.get('title') or '(无标题)')[:40],
            record.get('instanceStatus') or '?',
            record.get('approvedResult') or '?'))
    return records


def pick_attachment(records):
    """挑第一个带附件的实例。时间窗内可能一张附件都没有，所以放宽再翻几页。"""
    for record in records:
        items = api.attachments_of(record)
        if items:
            return record, items[0]
    return None, None


def probe_temp_url(client, field_id, item):
    step('4/5 附件换临时免登地址')
    name = item.get('name') or ''
    source = item.get('downloadUrl') or item.get('url') or ''
    print('附件：%s  size=%s  字段=%s' % (name[:50], item.get('size'), field_id))
    print('原地址：%s' % mask_url(source))
    try:
        url = client.temporary_url(source)
    except api.OpenApiError as exc:
        print('失败：%s' % exc)
        if exc.payload:
            print('响应原文：%s' % json.dumps(exc.payload, ensure_ascii=False)[:600])
        return ''
    print('换到地址：%s' % mask_url(url))
    return url


def probe_download(url, name):
    step('5/5 下载附件')
    safe = os.path.basename(name) or 'attachment.bin'
    folder = os.path.join(OUT_DIR, dt.datetime.now().strftime('%Y%m%d-%H%M%S'))
    dest = os.path.join(folder, safe)
    try:
        size = api.download(url, dest)
    except api.OpenApiError as exc:
        print('失败：%s' % exc)
        return False
    digest = hashlib.sha256()
    with open(dest, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b''):
            digest.update(chunk)
    head = open(dest, 'rb').read(8)
    print('落地：%s' % dest)
    print('字节数：%d  sha256 前 12 位：%s' % (size, digest.hexdigest()[:12]))
    print('文件头：%r' % head)
    return size > 0


def main():
    parser = argparse.ArgumentParser(description='宜搭开放平台链路探针')
    parser.add_argument('--form', default=CONTRACT_FORM, help='试哪张表的流程实例')
    parser.add_argument('--days', type=int, default=7, help='往回看几天')
    parser.add_argument('--inspect', action='store_true',
                        help='只查分页信封和时间字段，不换附件地址')
    args = parser.parse_args()

    if not probe_credentials():
        return 2
    if args.inspect:
        probe_inspect(api.OpenApiClient(), args.form, args.days)
        return 0
    if not probe_token():
        return 3

    client = api.OpenApiClient()
    records = probe_instances(client, args.form, args.days)
    if records is None:
        print()
        print('结论：换 token 通了，拉实例被权限拒绝。按上面的处理办法补权限并发版，再跑一次。')
        return 4
    if not records:
        print()
        print('结论：token 和权限都通了，但时间窗内没有审批通过的实例。')
        print('下一步：把 --days 调大再跑；仍为空则查 userId 在该应用里有没有数据权限。')
        return 4

    record, picked = pick_attachment(records)
    if not picked:
        print()
        print('结论：实例拉到了，但这批记录里没有附件。')
        print('下一步：把 --days 调大，让时间窗覆盖到带附件的单据。')
        return 5

    url = probe_temp_url(client, picked[0], picked[1])
    if not url:
        return 6
    if not probe_download(url, picked[1].get('name') or ''):
        return 7

    print()
    print('全部通过：换 token、筛实例、换附件地址、下载四个环节都可用。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
