# -*- coding: utf-8 -*-
"""出结果清单：CSV 给人核，Markdown 给人读。"""

import csv
import io
import os
import json

from . import naming


def write_csv(files, cfg, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with io.open(path, 'w', encoding='utf-8-sig', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['序号', '归档目录', '类型', '确认度', '新文件名',
                    '原始文件名', '大小', '上传次数', '来源流程',
                    '是否加密', '内容哈希', '首次提交时间', '验证状态',
                    '输出状态', '版本组', '版本号', '全部原名', '来源记录', '备注',
                    'AI状态', 'AI模型', 'AI证据', 'AI调用量', 'AI缓存命中'])
        for i, f in enumerate(sorted(files, key=_sort_key), start=1):
            w.writerow([
                i, naming.target_dir(f, cfg) if f.new_name else '', f.type_name or '', f.confidence or '',
                f.new_name or '', f.name, f.size if f.size is not None else '',
                f.upload_count, '/'.join(f.forms),
                '是' if f.secret else '否',
                f.content_hash or '',
                _fmt_time(f.first_seen),
                'SHA-256已验证' if f.content_hash else '未验证内容（仅UUID去重）',
                '已被新版替代' if f.superseded else ('计划输出' if f.new_name else '自动跳过（不存储）'),
                f.version_group, f.version_order or '',
                json.dumps(sorted({o.name for o in f.obs}), ensure_ascii=False),
                json.dumps([{'单据': o.instance_id, '标题': o.instance_title,
                             '字段': o.field_label, 'UUID': o.file_uuid,
                             '时间': o.submitted_at, '原名': o.name}
                            for o in f.obs], ensure_ascii=False),
                f.download_error or f.review_note,
                f.ai_result.get('status', ''), f.ai_result.get('model', ''),
                json.dumps(f.ai_result.get('evidence', []), ensure_ascii=False),
                json.dumps(f.ai_result.get('usage', {}), ensure_ascii=False),
                '是' if f.ai_result.get('cached') else '',
            ])
    return path


def write_markdown(files, findings, cfg, summary, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    out = []
    out.append('# 附件去重归类结果')
    out.append('')
    out.append('- 项目号：%s' % cfg.project_no)
    out.append('- 采购单位：%s' % cfg.purchaser)
    out.append('')
    out.append('## 汇总')
    out.append('')
    out.append('| 项 | 数量 |')
    out.append('| --- | --- |')
    out.append('| 附件出现次数（含重复上传） | %d |' % summary['total_uploads'])
    out.append('| 去重后文件数 | %d |' % summary['logical_files'])
    out.append('| 已归类 | %d |' % summary['classified'])
    out.append('| 正文未读、按文件名兜底 | %d |' % summary.get('rule_fallback', 0))
    out.append('| %s | %d |' % ('自动跳过' if cfg.unmatched_policy == 'skip' else '未归类', summary['manual']))
    out.append('| 其中加密目录 | %d |' % summary['secret'])
    out.append('| 内容未验证 | %d |' % summary.get('unverified', 0))
    out.append('| 已被确认新版替代（不输出） | %d |' % summary.get('superseded', 0))
    out.append('')
    out.append('文件类型采用全局固定编号1～28；同类型不同文件使用稳定子编号。只按 UUID 或 SHA-256 合并，同名同大小不构成重复证据。')
    out.append('未识别文件默认保留原名进入待归类。项目保密标记决定整个项目存入公共区或加密区；本地工具尚未接入指定人员访问权限。')
    out.append('')

    by_folder = {}
    for f in files:
        if not f.folder or not f.new_name:
            continue
        by_folder.setdefault(f.folder, []).append(f)

    out.append('## 归档明细')
    out.append('')
    for folder in cfg.folders:
        items = by_folder.get(folder) or []
        if not items:
            continue
        out.append('### %s（%d）' % (folder, len(items)))
        out.append('')
        out.append('| 新文件名 | 类型 | 确认度 | 原文件名 | 上传 | 来源 |')
        out.append('| --- | --- | --- | --- | --- | --- |')
        for f in sorted(items, key=_sort_key):
            out.append('| %s | %s | %s | %s | %d | %s |' % (
                f.new_name, f.type_name, f.confidence, f.name,
                f.upload_count, '/'.join(f.forms)))
        out.append('')

    manual = [f for f in files if not f.folder]
    if manual:
        out.append('## %s（%d）' % ('自动跳过' if cfg.unmatched_policy == 'skip' else '未归类', len(manual)))
        out.append('')
        out.append('| 原文件名 | 大小 | 上传 | 来源 | 原因 |')
        out.append('| --- | --- | --- | --- | --- |')
        for f in sorted(manual, key=_sort_key):
            out.append('| %s | %s | %d | %s | %s |' % (
                f.name, f.size if f.size is not None else '',
                f.upload_count, '/'.join(f.forms), f.review_note.replace('|', '／')))
        out.append('')

    ai_files = [f for f in files if f.ai_result]
    if ai_files:
        out.extend(['## 正文识别记录', '', '| 原文件名 | 状态 | 模型 | 判断依据与正文证据 |',
                    '| --- | --- | --- | --- |'])
        def cell(value):
            return str(value).replace('|', '／').replace('\n', ' ')
        for f in ai_files:
            evidence = '；'.join('%s：%s' % (e.get('location', ''), e.get('quote', ''))
                                 for e in f.ai_result.get('evidence', []))
            out.append('| %s | %s | %s | %s |' % (cell(f.name),
                cell(f.ai_result.get('status', '')), cell(f.ai_result.get('model', '')),
                cell(f.review_note + ('；' + evidence if evidence else ''))))
        out.append('')

    if findings.version_conflicts:
        out.append('## 同名不同文件（未确认版本关系时分别保留）')
        out.append('')
        for name, sizes in sorted(findings.version_conflicts.items()):
            out.append('- %s：大小 %s' % (name, '、'.join(str(s) for s in sizes)))
        out.append('')

    older = [f for f in files if f.superseded]
    if older:
        out.extend(['## 已确认旧版（仅保留来源记录，不输出附件）', ''])
        for f in older:
            out.append('- %s：版本组 %s，v%d，SHA-256 %s' %
                       (f.name, f.version_group, f.version_order, f.content_hash))
        out.append('')

    if findings.rename_candidates:
        out.append('## 大小相同但文件名不同（疑似改名副本）')
        out.append('')
        for size, names in sorted(findings.rename_candidates.items(),
                                  key=lambda kv: -kv[0]):
            out.append('- %s 字节：%s' % (size, '、'.join(names)))
        out.append('')

    with io.open(path, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out))
    return path


def _sort_key(f):
    """归类、首次上传时间和文件名共同保证报告排序稳定。"""
    return (f.folder or '￿', f.type_name or '￿', f.first_seen,
            f.new_name or '', f.name or '')


def _fmt_time(ms):
    if not ms:
        return ''
    import time
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(ms / 1000.0))
