# -*- coding: utf-8 -*-
"""按「每次上传」列出文件。

一次上传 = 一条流程单据（比如一次合同审批、一次货款支付）。
这里不做去重、不归类，就是把每条单据里上传的文件原样列出来，
需要的话下载到「每次上传」目录下，一条单据一个文件夹。

跨单据重复的文件会在各自单据下各出现一次 —— 这是故意的，
这个模式要的就是「每次上传了什么」。
"""

import csv
import io
import os
import shutil
import time

from . import naming


def groups(instances, form_names=None):
    """挑出有附件的单据，按提交时间排。"""
    out = []
    for inst in instances:
        if form_names and inst.form_name not in form_names:
            continue
        if not inst.attachments:
            continue
        out.append(inst)
    out.sort(key=lambda i: (i.submitted_at, i.form_name, i.title))
    return out


def folder_name(inst, index):
    """单据目录名：序号-时间-流程-件数-标题。

    件数（该单据的附件个数）是必需的，不是装饰：同一个项目在同一个流程里
    常有多次提交，而宜搭的标题模板会把它们生成得一模一样。实测
    24JZBTPFB157-401 的「合同审批」有 7 次提交，标题全是「袁剑峰发起的
    24JZBTPFB157-401合同审批」，只差时间；3 件和 29 件的那两条在目录名里
    看起来几乎一样，用户就据此外比对过线上的单据，白查了半天。
    件数写在流程名后面，紧挨着标题，扫一眼就能看出「这不是同一次」。
    """
    if inst.submitted_at:
        ts = time.strftime('%Y%m%d-%H%M',
                           time.localtime(inst.submitted_at / 1000.0))
    else:
        ts = '时间未知'
    return naming.sanitize('%02d-%s-%s-%d件-%s'
                           % (index, ts, inst.form_name,
                              len(inst.attachments), inst.title))


def plan(instances, cfg, form_names=None):
    """返回 [(单据, [(附件, 目标绝对路径), ...]), ...]。"""
    base = os.path.join(os.path.abspath(cfg.out_dir), '每次上传')
    taken = set()
    out = []
    for i, inst in enumerate(groups(instances, form_names), start=1):
        d = os.path.join(base, folder_name(inst, i))
        rows = []
        for att in inst.attachments:
            path = _unique(os.path.join(d, naming.sanitize(att.name)), taken)
            taken.add(path.lower())
            rows.append((att, path))
        out.append((inst, rows))
    return out


def _unique(path, taken):
    """同名文件加序号，不覆盖。"""
    if path.lower() not in taken:
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while ('%s_%d%s' % (stem, n, ext)).lower() in taken:
        n += 1
    return '%s_%d%s' % (stem, n, ext)


def stamp_time(path, submitted_at):
    """把文件或目录的时间盖成这条单据的提交时间。

    不盖的话，资源管理器「修改日期」那一列显示的全是下载时间（今天），
    26 个单据目录挤在同一天——想按时间找某一次提交就无从下手，只能一个
    个读目录名。盖成提交时间以后，点那一列表头排序就等于按提交顺序排。

    只改 mtime/atime，不动内容。
    """
    if not submitted_at:
        return False
    t = submitted_at / 1000.0
    try:
        os.utime(path, (t, t))
    except OSError:
        return False
    return True


def stamp_tree(top, submitted_at):
    """把整棵子树盖成同一时间。目录要从深到浅盖，否则写完子项又被改回去。"""
    for root, _dirs, files in os.walk(top, topdown=False):
        for name in files:
            stamp_time(os.path.join(root, name), submitted_at)
        stamp_time(root, submitted_at)
    stamp_time(top, submitted_at)


def write(planned, client, log=None):
    """下载到各自单据目录。返回 (成功, 失败)。"""
    done, failed = [], []
    landed = {}
    for inst, rows in planned:
        for att, dest in rows:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            key = att.file_uuid or ('%s|%s' % (att.name, att.size))
            if key in landed:
                try:
                    shutil.copyfile(landed[key], dest)
                    stamp_time(dest, inst.submitted_at)
                    done.append((att, dest))
                except OSError as exc:
                    failed.append((att, str(exc)))
                continue
            # 断点续传：目标已存在且非空，就是上次已经下好了，不再重新拉。
            # 判断条件是「非空」而不是「存在」——上次中断留下的 0 字节或
            # .part 残骸不能算数，否则坏文件会被当成成果永远跳过。
            # 命中的文件也记进 landed：后面同一个 uuid 出现在别的单据里时，
            # 直接从这里复制，省掉一次网络往返。
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                stamp_time(dest, inst.submitted_at)
                landed[key] = dest
                done.append((att, dest))
                continue
            tmp = dest + '.part'
            try:
                client.download(att.url, tmp)
                if os.path.getsize(tmp) == 0:
                    raise RuntimeError('下载到 0 字节')
                os.replace(tmp, dest)
            except Exception as exc:
                _rm(tmp)
                failed.append((att, str(exc)))
                if log:
                    log('下载失败 %s: %s' % (att.name, exc))
                continue
            stamp_time(dest, inst.submitted_at)
            landed[key] = dest
            done.append((att, dest))
        # 文件直接放在单据目录中，最后恢复单据的提交时间。
        if rows:
            for sub in {os.path.dirname(d) for _, d in rows}:
                stamp_time(sub, inst.submitted_at)
    return done, failed


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


def write_csv(planned, cfg, path):
    """每次上传的文件清单。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with io.open(path, 'w', encoding='utf-8-sig', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['单据序号', '流程', '单据标题', '提交时间', '提交人',
                    '附件字段', '文件名', '大小', '下载地址'])
        for i, (inst, rows) in enumerate(planned, start=1):
            ts = _fmt(inst.submitted_at)
            for att, _ in rows:
                w.writerow([i, inst.form_name, inst.title, ts,
                            inst.originator, att.field_label, att.name,
                            att.size if att.size is not None else '',
                            att.url])
    return path


def write_markdown(planned, cfg, path, failed=None):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    out = ['# 每次上传的文件', '',
           '- 项目号：%s' % cfg.project_no,
           '- 采购单位：%s' % cfg.purchaser, '']
    total = sum(len(rows) for _, rows in planned)
    out.append('共 %d 次上传，合计 %d 个附件。' % (len(planned), total))
    out.append('')
    for i, (inst, rows) in enumerate(planned, start=1):
        out.append('## %d. %s（%s）' % (i, inst.title or inst.form_name,
                                       _fmt(inst.submitted_at)))
        out.append('')
        out.append('- 流程：%s' % inst.form_name)
        if inst.originator:
            out.append('- 提交人：%s' % inst.originator)
        out.append('- 附件：%d 个' % len(rows))
        out.append('')
        out.append('| 附件字段 | 文件名 | 大小 |')
        out.append('| --- | --- | --- |')
        for att, _ in rows:
            out.append('| %s | %s | %s |'
                       % (att.field_label or att.field_id, att.name,
                          _human(att.size)))
        out.append('')
    if failed:
        out.append('## 下载失败（%d）' % len(failed))
        out.append('')
        for att, err in failed:
            out.append('- %s：%s' % (att.name, err))
        out.append('')
    with io.open(path, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out))
    return path


def _fmt(ms):
    if not ms:
        return ''
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(ms / 1000.0))


def _human(size):
    if size is None:
        return ''
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return '%.0f %s' % (size, unit) if unit == 'B' else \
                '%.1f %s' % (size, unit)
        size /= 1024.0
