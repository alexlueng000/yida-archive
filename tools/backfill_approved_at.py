"""把「最终审批通过时间」补进已归档项目的编号记录。

夜间任务是 2026-10-28 之后才开始写这个时间的，之前落盘的项目账本里没有。
这个脚本按日期重新扫一遍接口，把时间按「单据 + 文件」对回账本里的每条来源记录，
网页上就能显示「审批通过 10-27 16:12」用来和宜搭里的单据核对。

只改编号记录.json 里的 approved_at 和 submitted_at，不动任何文件；默认只打印会改
多少条，加 --apply 才写盘。账本里已有的时间默认保留，加 --refresh 才按接口值覆盖
（用于修正早期把 modifiedTimeGMT 当审批时间、又按 UTC 解析算偏 8 小时的旧数据）。
对不上的一律跳过并列出来，不猜。
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from yida_archive import nightly  # noqa: E402


def approval_map(instances):
    """(单据, 文件) 到 (审批通过时间, 提交时间)。文件 id 优先，文件名+大小兜底。"""
    by_uuid, by_name = {}, {}
    for inst in instances:
        value = (inst.approved_at, inst.submitted_at)
        for obs in inst.attachments:
            if obs.file_uuid:
                by_uuid.setdefault((inst.form_uuid, inst.instance_id, obs.file_uuid), value)
            by_name.setdefault((inst.form_uuid, inst.instance_id, obs.name, obs.size), value)
    return by_uuid, by_name


def ledgers(root):
    for path in sorted(Path(root).glob('*/*/编号记录.json')):
        if path.is_symlink() or path.parent.is_symlink() or path.parent.parent.is_symlink():
            continue
        yield path


def patch(path, by_uuid, by_name, apply, refresh=False):
    data = json.loads(path.read_text(encoding='utf-8'))
    changed = missing = 0
    for unit in data.get('units', {}).values():
        raw = unit.get('file') or {}
        for obs in raw.get('obs') or []:
            key_uuid = (obs.get('form_uuid', ''), obs.get('instance_id', ''), obs.get('file_uuid', ''))
            key_name = (obs.get('form_uuid', ''), obs.get('instance_id', ''),
                        obs.get('name', ''), obs.get('size'))
            found = by_uuid.get(key_uuid) if obs.get('file_uuid') else None
            found = found or by_name.get(key_name)
            if not found:
                missing += 1
                continue
            when, submitted = found
            touched = False
            if when and (refresh or not obs.get('approved_at')):
                obs['approved_at'] = when
                touched = True
            if submitted and (refresh or not obs.get('submitted_at')):
                obs['submitted_at'] = submitted
                touched = True
            if touched:
                changed += 1
    if changed and apply:
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp, path)
    return changed, missing


def main(argv=None):
    parser = argparse.ArgumentParser(description='补齐编号记录里的审批通过时间')
    parser.add_argument('--date', action='append', required=True,
                        help='审批通过的日期，北京时间；可写多次')
    parser.add_argument('--out', default='归档结果')
    parser.add_argument('--apply', action='store_true', help='真的写盘，不加只统计')
    parser.add_argument('--refresh', action='store_true',
                        help='账本里已有的时间也按接口值覆盖（修正算错时间的旧数据）')
    args = parser.parse_args(argv)

    import datetime as dt
    instances = []
    for value in args.date:
        day = dt.date.fromisoformat(value)
        found = nightly.scan_day(day)
        print('%s 取到单据 %d 条' % (day, len(found)))
        instances.extend(found)
    by_uuid, by_name = approval_map(instances)
    print('可用的来源记录 %d 条（按文件 id）、%d 条（按文件名）' % (len(by_uuid), len(by_name)))

    total = missed = projects = 0
    for path in ledgers(args.out):
        changed, missing = patch(path, by_uuid, by_name, args.apply, args.refresh)
        total += changed
        missed += missing
        projects += 1 if changed else 0
        if changed or missing:
            print('%s：补 %d 条，对不上 %d 条' % (path.parent.name, changed, missing))
    print('%s：%d 个项目共 %d 条来源记录，对不上 %d 条'
          % ('已写入' if args.apply else '待写入', projects, total, missed))
    return 0


if __name__ == '__main__':
    sys.exit(main())
