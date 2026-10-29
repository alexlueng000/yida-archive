# -*- coding: utf-8 -*-
"""把取数、去重、归类、命名、落盘、出报告串起来。

一次 run 的顺序：
  取数 -> 分组去重 -> 下载算哈希 -> 合并同内容 -> 归类 -> 命名 -> 落盘 -> 出报告

dry_run 时跳过下载和落盘，只给方案。
"""

import os
import json

from . import ai_classify, archive, classify, dedupe, naming, numbering, report


class Result:
    def __init__(self):
        self.instances = []
        self.files = []
        self.findings = None
        self.summary = {}
        self.done = []
        self.failed = []
        self.paths = {}


def _log_factory(verbose):
    def log(msg):
        if verbose:
            print('  ' + msg)
    return log


def run(cfg, instances, client=None, dry_run=True, do_hash=True,
        verbose=True, out_prefix='archive'):
    log = _log_factory(verbose)
    res = Result()
    res.instances = instances

    atts = sum(len(i.attachments) for i in instances)
    print('取到 %d 条单据，附件出现 %d 次' % (len(instances), atts))
    if not atts:
        print('没有附件，检查项目号和表单配置。')
        return res

    files, findings = dedupe.group(instances)
    print('去重后 %d 个文件' % len(files))

    if do_hash and client is not None and not dry_run:
        print('下载并计算内容哈希...')
        archive.hash_all(files, client, cfg.work_dir, log=log)
        before = len(files)
        files = archive.merge_after_hash(files)
        if len(files) != before:
            print('按内容哈希再合并：%d -> %d' % (before, len(files)))

    files = dedupe.refine(files)
    findings = dedupe.findings_for(files)
    numbering.restore_reviews(cfg)
    classify.classify(files, cfg)
    ai_classify.apply(files, cfg, ai_classify.file_content, log=log)
    files = numbering.include_history(files, cfg)
    classify.mark_secret(files, cfg)
    naming.assign(files, cfg, cfg.project_no)

    res.files = files
    res.findings = findings
    res.summary = classify.describe(files, cfg)

    print(('已归类 %d，自动跳过 %d，其中加密 %d' if cfg.unmatched_policy == 'skip'
           else '已归类 %d，未归类 %d，其中加密 %d')
          % (res.summary['classified'], res.summary['manual'],
             res.summary['secret']))

    if dry_run:
        items = archive.plan(files, cfg)
        res.done = items
        print('演练模式：不下载、不落盘。落盘方案 %d 条' % len(items))
        for f, dest in items[:10]:
            print('  %s' % os.path.relpath(dest, os.path.abspath(cfg.out_dir)))
        if len(items) > 10:
            print('  ... 其余 %d 条未显示' % (len(items) - 10))
        return res
    else:
        if client is None:
            raise RuntimeError('落盘模式需要下载能力，请用在线取数。')
        archive.create_folders(cfg)
        done, failed = archive.write(files, cfg, client, cfg.work_dir, log=log)
        res.done, res.failed = done, failed
        if not failed:
            numbering.finalize(cfg, files, done, cleanup=True)
        print('落盘成功 %d，失败 %d' % (len(done), len(failed)))

    report_dir = str(numbering.project_dir(cfg))
    os.makedirs(os.path.abspath(report_dir), exist_ok=True)
    with open(os.path.join(report_dir, '归档规范.json'), 'w', encoding='utf-8') as fh:
        json.dump({'schema': 2, 'project': cfg.project_no, 'purchaser': cfg.purchaser}, fh,
                  ensure_ascii=False)
    res.summary = classify.describe(files, cfg)
    csv_path = report.write_csv(
        files, cfg, os.path.join(report_dir, out_prefix + '-清单.csv'))
    md_path = report.write_markdown(
        files, findings, cfg, res.summary,
        os.path.join(report_dir, out_prefix + '-结果.md'))
    manifest = None
    if not dry_run:
        manifest = archive.write_manifest(
            res.done, res.failed, cfg,
            os.path.join(report_dir, out_prefix + '-落盘清单.tsv'))
    res.paths = {'csv': csv_path, 'markdown': md_path, 'manifest': manifest}

    print('清单：%s' % csv_path)
    print('报告：%s' % md_path)
    if manifest:
        print('落盘清单：%s' % manifest)
    return res
