# -*- coding: utf-8 -*-
"""盘点每个项目有多少单据、多少附件，用来挑归档样本。

要找一个「附件多、流程走完」的项目时，不能只看配置里那一个项目号。
接口不支持「列出所有项目」，所以这里按表单翻全量，再在本地按项目号聚合。

经 `run.py --survey` 调用。不带项目号取数时，每个表单都要翻到底，
单据多的话会比较慢。
"""

import json
import time

from . import extract

# 平台的状态码 -> 人话
STATUS_ZH = {
    'RUNNING': '审批中',
    'COMPLETED': '已完成',
    'TERMINATED': '已终止',
    'CANCELED': '已取消',
    'CANCELLED': '已取消',
    'DRAFT': '草稿',
    'SUSPEND': '已挂起',
    'SUSPENDED': '已挂起',
}

DONE = ('COMPLETED',)


def _project_of(form_data, field_id):
    """取项目号。字段可能是裸值，也可能是 {value,text} 结构。"""
    raw = extract.get_field(form_data, field_id)
    if isinstance(raw, dict):
        raw = raw.get('value') or raw.get('text')
    if not isinstance(raw, str):
        return '(空)' if raw in (None, '') else str(raw)
    return raw.strip() or '(空)'


def collect(client, cfg, on_form=None):
    """按项目号聚合全部单据。返回 {项目号: 统计}。"""
    stats = {}
    for form_cfg in cfg.forms:
        name = form_cfg.get('name', '')
        field_id = (form_cfg.get('project_field') or '').split('.')[0]
        n = 0
        for row in client.iter_records(form_cfg, project_no=''):
            fd = row.get('formData') or row.get('data') or {}
            proj = _project_of(fd, field_id)
            s = stats.setdefault(proj, {
                'instances': 0, 'attachments': 0,
                'forms': {}, 'statuses': {}, 'first': None, 'last': None,
            })
            s['instances'] += 1
            s['attachments'] += len(extract.find_attachments(fd))
            s['forms'][name] = s['forms'].get(name, 0) + 1
            status = row.get('instanceStatus') or '(无状态)'
            s['statuses'][status] = s['statuses'].get(status, 0) + 1
            ts = int(row.get('gmtCreate') or 0)
            if ts:
                s['first'] = ts if s['first'] is None else min(s['first'], ts)
                s['last'] = ts if s['last'] is None else max(s['last'], ts)
            n += 1
        if on_form:
            on_form(name, n)
    return stats


def _fmt_ts(ts):
    """带年份。只显示月日的话，跨年的区间看起来像倒着走（10-24~07-21）。"""
    if not ts:
        return '--'
    return time.strftime('%y-%m-%d', time.localtime(ts / 1000.0))


def _status_summary(statuses):
    return ' '.join('%s×%d' % (STATUS_ZH.get(k, k), v)
                    for k, v in sorted(statuses.items(), key=lambda kv: -kv[1]))


def finished(stats, cfg):
    """挑出「每个流程都走过、且单据全部已完成」的项目，按附件数从多到少。"""
    want = len(cfg.forms)
    out = []
    for proj, s in stats.items():
        if len(s['forms']) < want:
            continue
        if not all(k in DONE for k in s['statuses']):
            continue
        out.append((proj, s))
    return sorted(out, key=lambda kv: -kv[1]['attachments'])


def render(stats, cfg, limit=0):
    if not stats:
        return ('一个项目号都没读到。\n'
                '先确认配置里的 scope.forms[].project_field 字段 id 对不对，\n'
                '用 run.py --discover 看实际有哪些字段。')

    total_forms = len(cfg.forms)
    rows = sorted(stats.items(), key=lambda kv: -kv[1]['attachments'])
    shown = rows[:limit] if limit else rows

    out = ['共 %d 个项目号，%d 条单据'
           % (len(stats), sum(s['instances'] for s in stats.values())), '']
    head = '%-24s %4s %6s %8s  %-17s %s' % (
        '项目号', '单据', '附件位', '流程覆盖', '时间', '状态')
    out.append(head)
    out.append('-' * len(head))
    for proj, s in shown:
        out.append('%-24s %4d %6d %8s  %-17s %s' % (
            proj[:24], s['instances'], s['attachments'],
            '%d/%d' % (len(s['forms']), total_forms),
            '%s~%s' % (_fmt_ts(s['first']), _fmt_ts(s['last'])),
            _status_summary(s['statuses'])))
    if limit and len(rows) > limit:
        out.append('... 还有 %d 个没列出来（--survey-limit 0 可以全列）'
                   % (len(rows) - limit))

    out.append('')
    done = finished(stats, cfg)
    if done:
        out.append('全流程走完的（%d 个流程都走过，且单据全部已完成），'
                   '按附件数从多到少：' % total_forms)
        for proj, s in done[:5]:
            out.append('  %-24s %d 条单据，%d 个附件位，%s~%s'
                       % (proj, s['instances'], s['attachments'],
                          _fmt_ts(s['first']), _fmt_ts(s['last'])))
    else:
        out.append('没有哪个项目号同时满足「每个流程都走过」且「全部已完成」。')
        out.append('上面表格是按附件数排的，第一个就是附件最多的。')
    return '\n'.join(out)


def _table(header, rows):
    out = ['| ' + ' | '.join(header) + ' |',
           '| ' + ' | '.join('---' for _ in header) + ' |']
    out.extend('| ' + ' | '.join(r) + ' |' for r in rows)
    return out


def render_markdown(stats, cfg, top=20, app_type='', limit=0):
    """推荐名单的 Markdown 版，给人看和转发的。

    比屏幕版多两样：一是每个推荐项目按流程拆开列（能直接看出「都走过」
    是走过哪几条），二是附上未走完项目的对照表，免得看的人以为
    附件最多的就是推荐的那个。
    """
    forms = [f.get('name', '') for f in cfg.forms]
    total_inst = sum(s['instances'] for s in stats.values())
    total_att = sum(s['attachments'] for s in stats.values())

    out = ['# 附件归档 · 项目推荐名单', '']
    out.append('> 由 `run.py --online --survey` 盘点生成，数据取自宜搭线上。')
    out.append('')
    out.append('- 生成时间：%s' % time.strftime('%Y-%m-%d %H:%M'))
    if app_type:
        out.append('- 应用：`%s`' % app_type)
    out.append('- 覆盖流程：%s' % '、'.join(forms))
    out.append('- 盘点范围：%d 个表单翻全，%d 条单据，%d 个项目号，'
               '合计 %d 个附件位'
               % (len(forms), total_inst, len(stats), total_att))
    out.append('')

    done = finished(stats, cfg)
    shown = done[:limit] if limit else done
    out.append('## 一、推荐项目（共 %d 个）' % len(done))
    out.append('')
    out.append('筛选条件：**%d 个流程都走过，且每条单据的状态都是「已完成」**。'
               '按附件位从多到少排。' % len(forms))
    out.append('')
    if done:
        rows = []
        for i, (proj, s) in enumerate(shown, 1):
            rows.append(['%d' % i, '`%s`' % proj]
                        + ['%d' % s['forms'].get(n, 0) for n in forms]
                        + ['%d' % s['instances'], '%d' % s['attachments'],
                           '%s ~ %s' % (_fmt_ts(s['first']),
                                        _fmt_ts(s['last']))])
        out.extend(_table(['#', '项目号'] + forms
                          + ['单据合计', '附件位', '时间跨度'], rows))
        if len(done) > len(shown):
            out.append('')
            out.append('（只列了前 %d 个，完整名单见下方 JSON。）' % len(shown))
    else:
        out.append('没有项目同时满足这两个条件。')
    out.append('')

    ranked = sorted(stats.items(), key=lambda kv: -kv[1]['attachments'])
    top_rows = []
    for proj, s in ranked[:top]:
        mark = '是' if (len(s['forms']) >= len(forms)
                        and all(k in DONE for k in s['statuses'])) else '否'
        top_rows.append(['`%s`' % proj, '%d' % s['instances'],
                         '%d' % s['attachments'],
                         '%d/%d' % (len(s['forms']), len(forms)),
                         '%s ~ %s' % (_fmt_ts(s['first']), _fmt_ts(s['last'])),
                         _status_summary(s['statuses']), mark])
    out.append('## 二、附件最多的 %d 个项目（对照用）' % len(top_rows))
    out.append('')
    out.append('**这一节不等于推荐名单。** 排在前面但「推荐」列写否的，'
               '都是流程没走完或流程没走全的——附件再多也不在推荐之列。')
    out.append('')
    out.extend(_table(['项目号', '单据数', '附件位', '流程覆盖', '时间跨度',
                       '状态', '推荐'], top_rows))
    out.append('')

    out.append('## 三、口径与边界')
    out.append('')
    out.append('- **盘点只覆盖配置里写了的表单**（%s）。应用里没配的表单，'
               '它的数据不在这张表里，「流程都走过」也只是这个范围内的说法。'
               % '、'.join(forms))
    out.append('- **「已完成」按平台的 `instanceStatus` 判**。'
               '「已终止」「审批中」都不算走完，所以附件最多的项目'
               '未必进推荐名单。')
    out.append('- **「附件位」不是文件数。** 同一份文件在多个流程、'
               '多条单据里反复上传，每次都算一个附件位。'
               '去重之后实际文件数通常只有一半左右。')
    out.append('- **项目号取自表单里配置的项目字段**。没填这个字段的单据'
               '归到「(空)」一档，不参与推荐。')
    out.append('- 项目号写错、或者同一项目在不同流程里写法不一致，'
               '都会被拆成两个项目号。要按项目归档前建议先抽查几条。')
    out.append('')
    return '\n'.join(out)


def to_json(stats, cfg, app_type=''):
    """原始统计。存下来可以离线重新出各种口径的表，不用再联网翻一遍。"""
    return {
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'app_type': app_type,
        'forms': [f.get('name', '') for f in cfg.forms],
        'project_count': len(stats),
        'instance_count': sum(s['instances'] for s in stats.values()),
        'attachment_count': sum(s['attachments'] for s in stats.values()),
        'projects': stats,
    }


def write_json(stats, cfg, path, app_type=''):
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(to_json(stats, cfg, app_type=app_type), fh,
                  ensure_ascii=False, indent=1, sort_keys=True)
    return path
