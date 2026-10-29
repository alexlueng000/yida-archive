# -*- coding: utf-8 -*-
"""夜间批量作业：扫当天审批通过的流程，把附件归到项目下。

分三步，当前实现到第 2 步：
  1. 扫描：按本地日期取 19 个流程里当天审批通过、带附件的实例
  2. 归组：按项目号分组。没有项目号字段的流程，用合同号字段当项目号
  3. 归档：逐项目跑 pipeline.run，复用已有的去重、归类、命名、落盘

为什么要二分时间窗：开放平台的实例接口不认 pageNumber/pageSize，每次固定
回 10 条，只有 totalCount 可信。所以按总数把时间窗对半切，切到 10 条以内
再一次取回，保证不漏单。
"""

import argparse
import copy
import datetime as dt
import json
import os
import re
import sys
import time
import unicodedata
from contextlib import contextmanager

from . import by_instance, extract, fetch_openapi as api, naming, pipeline
from .config import Config
from .models import AttachmentObs, Instance

CACHE_DIR = os.path.join(api.PROJECT_ROOT, '.cache', 'openapi')
META_CACHE = os.path.join(CACHE_DIR, 'project-meta.json')

# 需求暂缓：所有项目统一进公共区，不按「是否南科大企业项目」拆分。
# 与网页端一致（web/server.py 里同样写死 project_secret=False）。
# 以后要启用，把这里改成 True 即可，回查「是否南科大」的逻辑一直都在。
ENCRYPTED_SPLIT = False

# 没有项目号的单据（如「总代理协议及重大项目合作协议」就没有项目号字段）
# 落到这个目录下等人认领。命名规则要项目号，这类只能保留原文件名。
UNATTRIBUTED_DIR = '_未归组项目'


def _form(name, form_uuid, project='', contract='', customer='', secret=''):
    return {'name': name, 'form_uuid': form_uuid, 'project_field': project,
            'contract_field': contract, 'customer_field': customer,
            'secret_field': secret}


# 19 个流程，字段 id 来自各表单 schema。
# 取键顺序：有项目号字段用项目号；没有的，这张表的合同号字段里填的就是项目号。
# 例外是合同审批：它的「合同号（外部）」填的是 SZICC260908WF238 这类外部编码，
# 不是项目号，所以那张表必须用项目号字段。
FORMS = [
    _form('合同审批', 'FORM-HT866U91JVN4V3LWAWXX5AYC8UG63K2N7EM9LQZ5',
          project='textField_ky9zhf07', contract='textField_l7psgjt7',
          secret='radioField_lopccl2k'),
    _form('进口货物报关申请', 'FORM-HT866U91JVN4V3LWAWXX5AYC8UG63K2N7EM9LJX5',
          contract='textField_ky8c6n97'),
    _form('报告、报表和函件', 'FORM-HT866U91JVN4V3LWAWXX5AYC8UG63K2N7EM9LIX5',
          contract='textField_l2e43d9w'),
    _form('总合作协议（非常规）', 'FORM-HT866U91JVN4V3LWAWXX5AYC8UG63K2N7EM9LHX5'),
    _form('总代理协议及重大项目合作协议',
          'FORM-HT866U91JVN4V3LWAWXX5AYC8UG63K2N7EM9LFX5',
          customer='selectField_kyo7p7f4'),
    _form('物流服务询价/委托', 'FORM-HT866U91JVN4V3LWAWXX5AYC8UG63K2N7EM9LMX5',
          contract='textField_l2e48j6z'),
    _form('货款支付', 'FORM-HT866U91JVN4V3LWAWXX5AYC8UG63K2N7EM9LNX5',
          project='textField_ky8afinr', contract='textField_lec66o1j'),
    _form('保函开立申请', 'FORM-HT866U91JVN4V3LWAWXX5AYC8UG63K2N7EM9LQX5',
          contract='textField_l2e4ar33', customer='textField_m8qnot4z'),
    _form('开立发票申请', 'FORM-HT866U91JVN4V3LWAWXX5AYC8UG63K2N7EM9LPX5',
          project='textField_ky8b3veg'),
    _form('结算', 'FORM-HT866U91JVN4V3LWAWXX5AYC8UG63K2N7EM9LRX5',
          contract='textField_kzfcngdr', secret='radioField_mj2aaeno'),
    _form('项目营运支出', 'FORM-HT866U91JVN4V3LWAWXX5AYC8UG63K2N7EM9LSX5',
          contract='textField_l2e4dmir'),
    _form('信用证开立申请', 'FORM-HT866U91JVN4V3LWAWXX5AYC8UG63K2N7EM9LUX5',
          project='textField_ky8jn7st', contract='textField_lnv4mi8q'),
    _form('香港信用证交单申请', 'FORM-HT866U91JVN4V3LWAWXX5AYC8UG63K2N7EM9LZX5',
          project='textField_l2e4tbuz', contract='textField_lmkdla60'),
    _form('即远期信用证承兑/拒付', 'FORM-HT866U91JVN4V3LWAWXX5AYC8UG63K2N7EM9L0Y5',
          project='textField_ky8cxek5', contract='textField_lnv3udho'),
    _form('结算退款申请表', 'FORM-HT866U91JVN4V3LWAWXX5AYC8UG63K2N7EM9L1Y5',
          contract='textField_kzfcngdr'),
    _form('项目完结审批', 'FORM-C9E50E785EB849C583C7B956299560BBOWDW',
          project='textField_loxz77va', contract='textField_loxz77vg'),
    _form('信用证变更/撤销申请', 'FORM-4809DBFAE9924AE6A4654919F5261F279PQ3',
          project='textField_ky8jn7st', contract='textField_lnv4mi8q'),
    _form('退款', 'FORM-6F6E0EEC2D4E4B6C81F4527122C6CA423NIF',
          project='textField_ky8afinr', contract='textField_lec66o1j'),
    _form('减免税/税号归类申请', 'FORM-NC966W81GG7CJ7Q69L6NNDVJT61M3C0H43OJL6',
          project='textField_ky9zhf07', contract='textField_l7psgjt7'),
]

CONTRACT_FORM = FORMS[0]['form_uuid']


def text_of(form_data, field_id):
    """取一个字段的文本值。单选、下拉在不同接口里可能是字符串或 dict。"""
    if not field_id:
        return ''
    value = extract.get_field(form_data, field_id)
    if isinstance(value, dict):
        for key in ('text', 'label', 'value', 'name'):
            if value.get(key):
                return str(value[key]).strip()
        return ''
    if isinstance(value, list):
        return ' '.join(str(v) for v in value if isinstance(v, (str, int))).strip()
    if value is None:
        return ''
    return str(value).strip()


def name_of(who):
    """从人员结构里取中文名。平台会套成 {'name': {'nameInChinese': ...}}，得往里钻。"""
    if isinstance(who, str):
        return who
    if not isinstance(who, dict):
        return ''
    for key in ('nameInChinese', 'name', 'userId'):
        value = who.get(key)
        if isinstance(value, dict):
            nested = name_of(value)
            if nested:
                return nested
        elif value:
            return str(value)
    return ''


def person_of(record):
    return name_of(record.get('originator'))


def normalize_key(value):
    """把项目号的写法变体归一。

    同一个项目在结算里写 26JZBTPB328-(02)、在合同审批里写 26JZBTPB328-02，
    不归一会落进两个文件夹。全角括号先按 NFKC 转半角，再去掉包在括号里的
    批次号外层括号，最后删空格、统一大写。
    """
    text = unicodedata.normalize('NFKC', str(value or '')).strip()
    if not text:
        return ''
    text = re.sub(r'\s+', '', text)
    text = re.sub(r'-\s*\(\s*([^()]+?)\s*\)', r'-\1', text)
    text = re.sub(r'^\(\s*([^()]+?)\s*\)$', r'\1', text)
    return text.upper()


def key_field_of(form):
    """取项目号的字段。没有项目号字段的流程，合同号字段里填的就是项目号。"""
    return form.get('project_field') or form.get('contract_field') or ''


def build_instance(form, record):
    """把一条开放平台记录转成 pipeline 认的 Instance。"""
    data = api.form_data_of(record)
    inst = Instance(
        form_name=form['name'],
        form_uuid=form['form_uuid'],
        instance_id=api.instance_id_of(record),
        title=str(record.get('title') or ''),
        submitted_at=api.parse_gmt(record.get('createTimeGMT')),
        originator=person_of(record),
        status=str(record.get('instanceStatus') or ''),
        approved_at=api.parse_gmt(record.get('modifiedTimeGMT')),
        project_key=normalize_key(text_of(data, key_field_of(form))),
    )
    for field_id, item in api.attachments_of(record):
        inst.attachments.append(AttachmentObs(
            form_name=inst.form_name,
            form_uuid=inst.form_uuid,
            instance_id=inst.instance_id,
            instance_title=inst.title,
            submitted_at=inst.submitted_at,
            field_id=field_id,
            field_label=field_id,
            name=str(item.get('name') or ''),
            size=item.get('size'),
            file_uuid=str(item.get('fileUuid') or ''),
            url=str(item.get('downloadUrl') or item.get('url') or ''),
            approved_at=inst.approved_at,
        ))
    return inst


def is_finished(record):
    """审批通过并且流程已完结。

    服务端已经按这两个条件筛过，这里再核一遍：筛选参数写错时平台不报错，
    只是静默返回别的数据，所以不能只信服务端。
    """
    return (str(record.get('instanceStatus') or '') == 'COMPLETED'
            and str(record.get('approvedResult') or '').lower() == 'agree')


def scan_day(day, client=None, hours_back=0, on_form=None, stats=None):
    """取某一天（北京时间）审批通过并完结的全部实例，按表单顺序返回。"""
    start, end = api.day_window(day)
    if hours_back:
        start -= dt.timedelta(hours=hours_back)
    # modifiedTimeGMT 是最后修改时间，审批后又修改过的实例可能落在次日或更晚。
    # 搜到当前时刻，再以操作记录的最终同意时间筛回目标日期。
    modified_end = max(end, dt.datetime.now(api.CST))
    client = client or api.OpenApiClient()
    stats = stats if stats is not None else {}
    stats.setdefault('kept', 0)
    stats.setdefault('skipped', 0)
    stats.setdefault('times', [])
    out = []
    for form in FORMS:
        found = []
        for record in client.iter_window(
                form['form_uuid'], start, modified_end,
                instanceStatus='COMPLETED', approvedResult='agree'):
            if not is_finished(record):
                stats['skipped'] += 1
                continue
            inst = build_instance(form, record)
            # modifiedTimeGMT 只是最后一次修改，审批通过后还可能被抄送或重定向改掉，
            # 所以再查一次操作记录，取最后一条「同意」的时间。查不到就沿用修改时间。
            try:
                when = api.final_agree_time(client.operation_records(inst.instance_id))
            except Exception:
                when = 0
                stats['records_failed'] = stats.get('records_failed', 0) + 1
            if when:
                inst.approved_at = when
                for obs in inst.attachments:
                    obs.approved_at = when
                if not (start.timestamp() * 1000 <= when < end.timestamp() * 1000):
                    stats['outside_approval_window'] = stats.get('outside_approval_window', 0) + 1
                    continue
            else:
                stats['approval_time_unverified'] = stats.get('approval_time_unverified', 0) + 1
                continue
            stats['kept'] += 1
            stats['times'].append((inst.submitted_at, inst.approved_at))
            found.append(inst)
        if on_form:
            on_form(form, found)
        out.extend(found)
    return out


def summarize(instances, show_limit=8):
    """按流程汇总：几条单据、几份附件、能不能直接定到项目。"""
    total_files = sum(len(i.attachments) for i in instances)
    print()
    print('%-24s %6s %6s' % ('流程', '单据', '附件'))
    by_form = {}
    for inst in instances:
        row = by_form.setdefault(inst.form_name, [0, 0])
        row[0] += 1
        row[1] += len(inst.attachments)
    for form in FORMS:
        row = by_form.get(form['name'])
        if row:
            print('%-24s %6d %6d' % (form['name'], row[0], row[1]))
    print('%-24s %6d %6d' % ('合计', len(instances), total_files))

    print()
    print('带附件的单据明细（最多 %d 条）' % show_limit)
    shown = 0
    for inst in instances:
        if not inst.attachments:
            continue
        if shown >= show_limit:
            break
        shown += 1
        print('  [%s] %s' % (inst.form_name, inst.title[:46]))
        print('      实例 %s  发起人 %s  附件 %d 份' % (
            inst.instance_id[:8], inst.originator, len(inst.attachments)))
        for att in inst.attachments[:3]:
            print('      - %s  %s 字节' % (att.name[:48], att.size))
    return total_files


def group_by_project(instances):
    """按项目号分组。没有项目号的单独拎出来，不猜、不留到别的项目里。"""
    groups, missing = {}, []
    for inst in instances:
        if not inst.project_key:
            missing.append(inst)
            continue
        groups.setdefault(inst.project_key, []).append(inst)
    return groups, missing


def report_projects(groups, missing):
    print()
    print('按项目号分组：%d 个项目' % len(groups))
    for key in sorted(groups):
        rows = groups[key]
        files = sum(len(i.attachments) for i in rows)
        print('  %-22s 单据 %-3d 附件 %-4d' % (key, len(rows), files))
    if missing:
        print('  没有项目号、无法归组：%d 条' % len(missing))
        for inst in missing[:5]:
            print('    [%s] %s' % (inst.form_name, inst.title[:44]))


def load_meta_cache():
    try:
        with open(META_CACHE, encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_meta_cache(cache):
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = META_CACHE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(cache, fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, META_CACHE)


def lookup_contract(client, key):
    """按项目号回查合同审批，拿采购单位和「是否南科大」。

    当天的单据里很多项目的合同是几个月前审批的，不在时间窗内，所以只能
    按项目号精确查。平台这个搜索是模糊匹配，查回来还要再核一遍项目号。
    """
    body = {
        'appType': client.app_type,
        'systemToken': client.system_token,
        'userId': client.user_id,
        'formUuid': CONTRACT_FORM,
        'language': 'zh_CN',
        'pageNumber': 1,
        'instanceStatus': 'COMPLETED',
        'approvedResult': 'agree',
        'searchFieldJson': json.dumps({'textField_ky9zhf07': key}, ensure_ascii=False),
    }
    payload = client.call(api.INSTANCES_PATH, body)
    best = None
    for record in api.find_records(payload):
        data = api.form_data_of(record)
        if normalize_key(text_of(data, 'textField_ky9zhf07')) != key:
            continue
        # 同一项目可能有多份合同（改版、分期），取最新那份的标记
        stamp = api.parse_gmt(record.get('createTimeGMT'))
        if best is None or stamp > best[0]:
            best = (stamp, record, data)
    if best is None:
        return None
    _, record, data = best
    flag = text_of(data, 'radioField_lopccl2k')
    return {
        'customer': text_of(data, 'selectField_kyo8vh4l'),
        'secret': True if flag == '是' else (False if flag == '否' else None),
        'title': str(record.get('title') or ''),
    }


def project_meta(client, keys, refresh=False):
    """批量回查项目元数据，结果落到本地缓存，避免每晚重复查。"""
    cache = load_meta_cache()
    out, changed, misses = {}, [], []
    for key in keys:
        hit = cache.get(key) or {}
        if not refresh and hit.get('secret') in (True, False) and hit.get('customer'):
            out[key] = hit
            continue
        found = lookup_contract(client, key)
        if found:
            cache[key] = found
            out[key] = found
            changed.append(key)
        else:
            out[key] = hit
            misses.append(key)
    if changed:
        save_meta_cache(cache)
    if misses:
        print('回查不到合同的项目 %d 个：%s' % (len(misses), '、'.join(misses[:6])))
    return out


def project_config(base_raw, key, meta, out_dir=None):
    """每个项目一份配置：项目号、采购单位不同，加密标记统一为否。"""
    raw = copy.deepcopy(base_raw)
    scope = raw.setdefault('scope', {})
    scope['project_no'] = key
    scope['purchaser'] = meta.get('customer') or '未指定采购单位'
    scope['project_secret'] = bool(meta.get('secret')) if ENCRYPTED_SPLIT else False
    if out_dir:
        raw.setdefault('storage', {})['out_dir'] = out_dir
    return Config(raw)


class Downloader:
    """pipeline 要的下载接口：给一个附件地址，落到本地。

    附件地址是带登录态的前端地址，机器下不了，先换临时免登地址再下。
    """

    def __init__(self, client=None):
        self.client = client or api.OpenApiClient()

    def download(self, url, dest_path):
        return api.download(self.client.temporary_url(url), dest_path)


def local_date(millis):
    return dt.datetime.fromtimestamp(millis / 1000.0, api.CST).date()


def report_time_evidence(stats):
    """复核筛选，并验证「最后修改时间」是否由审批动作推动。

    如果大量单据的创建日与最后修改日不同，说明修改时间跟着审批走，
    用它筛「当天审批完成」才靠得住；如果全部同一天，说明这个字段只是
    提交时间，筛选口径要换。
    """
    times = [(c, m) for c, m in stats.get('times', []) if c and m]
    cross = sum(1 for c, m in times if local_date(c) != local_date(m))
    print()
    print('复核：通过并完结 %d 条，被过滤 %d 条（应恒为 0）'
          % (stats.get('kept', 0), stats.get('skipped', 0)))
    if times:
        print('时间口径：创建日与最后修改日不同 %d/%d 条' % (cross, len(times)))
        print('  跨天比例高，说明最后修改时间随审批动作走，按它筛当天完成成立')


DIGEST_NAME = re.compile(r'^[0-9a-f]{64}$')


def purge_download_cache(work_dir):
    """删掉本轮下载的临时副本。

    hash_all 把附件按内容哈希存进 work_dir，落盘时直接从这里硬链接。
    这些副本跨轮没有用处（每轮都会重新下载），留着只会一天天占满磁盘。
    ai/ 子目录里的识别结果缓存要继续留着，那是花钱换来的，不能删。
    """
    removed = 0
    try:
        names = os.listdir(work_dir)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(work_dir, name)
        if DIGEST_NAME.match(name) and os.path.isfile(path):
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    return removed


def save_originals(instances, project, day, downloader):
    """保留每条单据的原始附件，批次重跑时使用稳定的实例目录。"""
    import hashlib
    root = os.path.join(api.PROJECT_ROOT, 'archive-' + naming.sanitize(project),
                        '每次上传', day.isoformat())
    os.makedirs(root, exist_ok=True)
    marker = os.path.join(os.path.dirname(os.path.dirname(root)), '.source-project.json')
    if os.path.exists(marker):
        with open(marker, encoding='utf-8') as f:
            if json.load(f).get('project') != project:
                raise ValueError('原始目录项目号冲突：' + marker)
    else:
        with open(marker, 'w', encoding='utf-8') as f:
            json.dump({'project': project}, f, ensure_ascii=False)
    planned = []
    for inst in by_instance.groups(instances):
        identity = hashlib.sha256((inst.form_uuid + ':' + inst.instance_id).encode('utf-8')).hexdigest()[:12]
        folder = os.path.join(root, by_instance.folder_name(inst, 1)[3:] + '-' + identity)
        taken = set()
        rows = []
        for att in inst.attachments:
            path = by_instance._unique(os.path.join(folder, naming.sanitize(att.name)), taken)
            taken.add(path.lower())
            rows.append((att, path))
        planned.append((inst, rows))
    done, failed = by_instance.write(planned, downloader, log=print)
    if failed:
        raise RuntimeError('原始附件下载失败 %d 份，停止该项目归档以便重试' % len(failed))
    return len(done), root


def archive_unattributed(instances, base_raw, out_dir, downloader, dry_run):
    """没有项目号的单据落到「未归组项目」区，按日期/流程/单据标题分开。

    命名规则靠项目号，这类单据给不出，所以保留原文件名。放在一个固定位置
    等人认领，也不能因为少一个字段就把附件丢掉。
    """
    storage = base_raw.get('storage') or {}
    out_root = out_dir or storage.get('out_dir', './archive')
    import hashlib
    placed, failures = 0, 0
    for inst in instances:
        if not inst.attachments:
            continue
        day = local_date(inst.submitted_at).isoformat() if inst.submitted_at else '无日期'
        folder = os.path.join(
            out_root, UNATTRIBUTED_DIR, day,
            naming.sanitize(inst.form_name),
            naming.sanitize(inst.title or inst.instance_id) + '-' +
            hashlib.sha256((inst.form_uuid + ':' + inst.instance_id).encode('utf8')).hexdigest()[:12])
        print()
        print('[未归组] %s  %s' % (inst.form_name, (inst.title or '')[:40]))
        print('  附件 %d 份，按提交日分目录，去向 %s' % (len(inst.attachments), folder))
        used = set()
        for att in inst.attachments:
            name = naming.sanitize(att.name)
            stem, ext = os.path.splitext(name)
            serial = 2
            while name.casefold() in used:      # 同一张单据里重名也不能互相覆盖
                name = '%s-%d%s' % (stem, serial, ext)
                serial += 1
            used.add(name.casefold())
            dest = os.path.join(folder, name)
            placed += 1
            if dry_run:
                print('    方案：%s' % dest)
                continue
            try:
                os.makedirs(folder, exist_ok=True)
                if os.path.isfile(dest) and os.path.getsize(dest) > 0:
                    print('    已存在：%s' % dest)
                    continue
                temporary = dest + '.part'
                downloader.download(att.url, temporary)
                if not os.path.getsize(temporary):
                    raise RuntimeError('下载到空文件')
                os.replace(temporary, dest)
                print('    存好：%s' % dest)
            except Exception as exc:            # 单个附件失败不影响其余
                failures += 1
                try:
                    os.remove(dest + '.part')
                except OSError:
                    pass
                print('    下载失败 %s：%s' % (att.name[:30], exc))
    return placed, failures


@contextmanager
def execution_lock():
    """独占夜间归档进程；进程异常退出时由操作系统释放。"""
    lock_path = os.path.join(api.PROJECT_ROOT, '.work', 'nightly.lock')
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, 'a+b') as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        warned = False
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (11, 13, 35, 36):
                    raise
                if not warned:
                    print('已有拉取任务运行中，正在等待完成…', flush=True)
                    warned = True
                time.sleep(5)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not any(arg == '--date' or arg.startswith('--date=') for arg in args):
        day = dt.datetime.now(api.CST).date() - dt.timedelta(days=1)
        args.extend(['--date', day.isoformat()])
    try:
        with execution_lock():
            return _main(args)
    except RuntimeError as exc:
        print('拉取任务无法启动：%s' % exc)
        return 1


def _main(argv=None):
    parser = argparse.ArgumentParser(description='夜间扫描：当天审批通过的流程与附件')
    parser.add_argument('--date', help='北京时间的日期，默认昨天；今天可写 today')
    parser.add_argument('--hours-back', type=int, default=0,
                        help='时间窗往前多留几小时，补跑漏掉的单据')
    parser.add_argument('--limit', type=int, default=8, help='明细打印条数')
    parser.add_argument('--config', default='config.yaml', help='基础配置，项目级参数会按项目覆盖')
    parser.add_argument('--out', help='覆盖落盘根目录')
    parser.add_argument('--project', action='append', default=None,
                        help='只处理指定项目，可写多次')
    parser.add_argument('--write', action='store_true',
                        help='真正下载并落盘；不加只出方案')
    parser.add_argument('--refresh-meta', action='store_true',
                        help='忽略缓存，重新回查每个项目的采购单位和加密标记')
    args = parser.parse_args(argv)

    if args.date in (None, '', 'yesterday'):
        day = dt.datetime.now(api.CST).date() - dt.timedelta(days=1)
    elif args.date == 'today':
        day = dt.datetime.now(api.CST).date()
    else:
        day = dt.date.fromisoformat(args.date)

    start, end = api.day_window(day)
    print('扫描日期：%s（北京时间 00:00-24:00）' % day)
    print('时间窗（北京时间）：%s 至 %s' % (api.utc_text(start), api.utc_text(end)))

    counter = {'calls': 0}

    def on_form(form, found):
        counter['calls'] += 1
        files = sum(len(i.attachments) for i in found)
        print('  %-24s 单据 %-3d 附件 %-4d' % (form['name'], len(found), files))

    print()
    print('逐流程扫描')
    stats = {}
    instances = scan_day(day, hours_back=args.hours_back, on_form=on_form, stats=stats)
    summarize(instances, args.limit)
    groups, missing = group_by_project(instances)
    report_projects(groups, missing)
    report_time_evidence(stats)
    if stats.get('approval_time_unverified'):
        print('无法核实最终审批时间：%d 条，需重试查询' % stats['approval_time_unverified'])

    if args.project:
        wanted = set(args.project)
        groups = {k: v for k, v in groups.items() if k in wanted}
        missing = [i for i in missing if getattr(i, 'project_key', '') in wanted]
        print()
        print('按 --project 过滤后剩 %d 个项目' % len(groups))
    if not groups and not missing:
        print()
        print('没有可归档的项目，结束。')
        return 1 if stats.get('approval_time_unverified') else 0

    client = api.OpenApiClient()
    meta = project_meta(client, sorted(groups), refresh=args.refresh_meta)

    from .config import load as load_config
    base_raw = load_config(args.config).raw
    downloader = Downloader(client)
    print()
    print('逐项目归档（%s）' % ('落盘' if args.write else '演练，不下载不落盘'))
    failures = []
    for key in sorted(groups):
        rows = groups[key]
        files = sum(len(i.attachments) for i in rows)
        info = meta.get(key) or {}
        flag = info.get('secret')
        if ENCRYPTED_SPLIT:
            flag_text = {True: '是', False: '否', None: '未确认'}[flag]
        else:
            flag_text = '否（统一不加密）'
        print()
        print('=' * 60)
        print('%s  采购单位=%s  加密=%s  单据 %d  附件 %d' % (
            key, info.get('customer') or '(未回查到)', flag_text, len(rows), files))
        cfg = project_config(base_raw, key, info, args.out)
        try:
            if args.write:
                count, original_root = save_originals(rows, key, day, downloader)
                print('原始附件：%d 份，保存到 %s' % (count, original_root))
            result = pipeline.run(cfg, rows, client=downloader,
                                  dry_run=not args.write, verbose=False)
            if result.failed or any(f.download_error for f in result.files):
                raise RuntimeError('存在附件下载或归档失败，请核对并重试')
        except Exception as exc:                      # 单个项目失败不能拖垮整晚
            print('失败：%s' % exc)
            failures.append(key)
            continue
        finally:
            if args.write:
                purge_download_cache(cfg.work_dir)
        print('汇总：%s' % json.dumps(result.summary, ensure_ascii=False))

    if missing:
        print()
        print('=' * 60)
        placed, ungrouped_failures = archive_unattributed(missing, base_raw, args.out, downloader,
                                                          dry_run=not args.write)
        if ungrouped_failures:
            failures.append('未归组项目（下载失败 %d 份）' % ungrouped_failures)
        print()
        print('未归组单据 %d 条，附件 %d 份，去向 %s'
              % (len(missing), placed, UNATTRIBUTED_DIR))
    if failures:
        print('失败项目：%s' % '、'.join(failures))
    return 1 if failures or stats.get('approval_time_unverified') else 0


if __name__ == '__main__':
    sys.exit(main())
