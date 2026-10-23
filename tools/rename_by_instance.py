# -*- coding: utf-8 -*-
"""按当前规则治理已落盘的「每次上传」目录：改名 + 盖时间。

用途：`by_instance.folder_name` 的格式变了以后（2026-09-29 在流程名和标题
之间加了附件件数），不必把几百个附件重下一遍，就地改名即可。同理，早先
下下来的目录时间全是下载时间，用 `--stamp-mtime` 就地盖成提交时间。

    py tools/rename_by_instance.py --snapshot snapshots/xxx.json \\
        --out-dir archive-xxx-全流程                     # 只打印方案
    py tools/rename_by_instance.py --snapshot snapshots/xxx.json \\
        --out-dir archive-xxx-全流程 --write             # 真改名
    py tools/rename_by_instance.py --snapshot snapshots/xxx.json \\
        --out-dir archive-xxx-全流程 --stamp-mtime       # 盖成提交时间

两个开关可以一起给，先改名再盖章。

改名的三道安全阀，任何一道不过就整体不动：
  1. 自检：盘上的目录名必须逐字等于「旧格式」或「新格式」之一，否则报出来
     不动——用重建旧名对比而不是把「N件」从新名里抠掉，因为标题里恰好含
     「件」字时抠字符串会误判；
  2. 长度：改名后最长的那条文件路径不得超过 240 字符（Windows 上限 260）；
  3. 目标目录已存在就跳过，不覆盖。

改名和盖章都靠快照里的单据顺序，所以快照必须是当初下载那次生成的。
目录按两位数序号对回快照，序号对不上就会报出来。
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from yida_archive import by_instance, fetch_json, naming

# Windows 的 MAX_PATH 是 260。旧名已经贴着上限的目录再加几字符就可能超，
# 超了 os.rename 会报错、下载下来的文件反而更难找，所以留 20 字符余量。
LIMIT = 240


def _old_name(inst, index):
    """按「加件数之前」的格式拼目录名，用来跟盘上的旧名对账。

    故意不复用 by_instance.folder_name：复用的话它一变，这里跟着变，
    自检就永远通过，等于没检。
    """
    if inst.submitted_at:
        ts = time.strftime('%Y%m%d-%H%M',
                           time.localtime(inst.submitted_at / 1000.0))
    else:
        ts = '时间未知'
    return naming.sanitize('%02d-%s-%s-%s'
                           % (index, ts, inst.form_name, inst.title))


def _deepest(base, old, new):
    """新旧名互换以后，最长的那个文件路径有多长。"""
    src = os.path.join(base, old)
    worst, path = 0, ''
    for root, _, files in os.walk(src):
        for name in files:
            rel = os.path.relpath(os.path.join(root, name), src)
            cand = os.path.join(base, new, rel)
            if len(cand) > worst:
                worst, path = len(cand), cand
    return worst, path


def build(base, instances):
    """返回 (matched, notes)。

    matched = [(盘上现名, 应有的新名, 单据), ...]，按序号。改名和盖章都用它。
    """
    if not os.path.isdir(base):
        return [], ['目录不存在：%s' % base]

    on_disk = {}
    for name in os.listdir(base):
        if not os.path.isdir(os.path.join(base, name)):
            continue
        head = name.split('-', 1)[0]
        if head.isdigit():
            on_disk[int(head)] = name

    planned = by_instance.groups(instances)
    matched, notes = [], []

    for i, inst in enumerate(planned, start=1):
        old = on_disk.pop(i, None)
        if old is None:
            notes.append('序号 %02d 盘上没有对应目录' % i)
            continue
        new = by_instance.folder_name(inst, i)
        if new == old:
            # 已经是新名是正常状态，不往「未处理」里塞——那栏只放真问题，
            # 否则 26 条「本来就对」会把 1 条「真没改成」淹掉。
            matched.append((old, new, inst))
            continue
        # 自检 1：盘上的名字必须是「旧格式」或「新格式」之一。
        # 用重建旧名来比，而不是把「N件」从新名里抠掉——标题里恰好含
        # 「函件-」这种字串时抠字符串会误判。
        if old != _old_name(inst, i):
            notes.append('序号 %02d 快照与盘上对不上，跳过：\n'
                         '        盘上 %s\n        既不是旧格式也不是新格式\n'
                         '        （快照可能不是当初下载那次生成的）'
                         % (i, old))
            continue
        matched.append((old, new, inst))

    for k in sorted(on_disk):
        notes.append('盘上多出序号 %02d：%s（快照里没有）' % (k, on_disk[k]))
    return matched, notes


def main(argv):
    p = argparse.ArgumentParser(description='按当前规则治理每次上传目录：改名 + 盖时间')
    p.add_argument('--snapshot', required=True, help='当初下载时生成的快照 JSON')
    p.add_argument('--out-dir', required=True, help='归档根目录，动的是它下面的「每次上传」')
    p.add_argument('--write', action='store_true', help='真改名（默认只打印方案）')
    p.add_argument('--stamp-mtime', action='store_true',
                   help='把每个目录和文件的修改时间盖成单据提交时间')
    args = p.parse_args(argv)

    if not args.write and not args.stamp_mtime:
        print('--write 和 --stamp-mtime 至少给一个，否则没有任何动作。')
        return 2

    if not os.path.exists(args.snapshot):
        print('快照不存在：%s' % args.snapshot)
        return 2

    instances = fetch_json.load(args.snapshot)
    base = os.path.join(os.path.abspath(args.out_dir), '每次上传')
    matched, notes = build(base, instances)
    todo = [(old, new, inst) for old, new, inst in matched if old != new]

    renamed = {}
    if args.write and todo:
        # 自检 2：长度。超限的一条都不改，免得改到一半。
        too_long = []
        for old, new, _ in todo:
            worst, path = _deepest(base, old, new)
            if worst > LIMIT:
                too_long.append('%s\n        最长 %d 字符（上限 %d）：%s'
                                % (new, worst, LIMIT, path))
        if too_long:
            print('这些目录改名后路径会超长，全部不动：')
            for item in too_long:
                print('  · %s' % item)
            return 3

        for old, new, _ in todo:
            print('  %s\n    -> %s' % (old, new))

        done, failed = 0, []
        for old, new, _ in todo:
            dst = os.path.join(base, new)
            # 自检 3：目标已存在就不覆盖。
            if os.path.exists(dst):
                failed.append('%s\n        目标已存在' % old)
                continue
            try:
                os.rename(os.path.join(base, old), dst)
            except OSError as exc:
                # 「拒绝访问」几乎都是目录里有文件正被 Word/Excel 打开
                # （Office 会在同目录留 ~$ 开头的隐藏锁文件，锁住整个目录）。
                # 一个目录改不了不该拖垮其余的，跳过并报出来。
                failed.append('%s\n        %s' % (old, exc))
                continue
            renamed[old] = new
            done += 1
        print('')
        print('改名完成 %d 个，跳过 %d 个。' % (done, len(failed)))
        for why in failed:
            print('  · %s' % why)
        if failed:
            print('')
            print('跳过的那几个，多半是文件正被 Office 开着。关掉再跑一次同样的'
                  '命令即可（已改过的会自动跳过）。')
    elif args.write:
        print('没有需要改名的目录。')

    if args.stamp_mtime:
        stamped, skipped = 0, []
        for old, new, inst in matched:
            cur = renamed.get(old, new)
            path = os.path.join(base, cur)
            if not os.path.isdir(path):
                # 上面改名被跳过（Office 锁着），原名还在，照原名盖。
                path = os.path.join(base, old)
            if not os.path.isdir(path):
                skipped.append('%s（目录不在）' % cur)
                continue
            if not inst.submitted_at:
                skipped.append('%s（单据没有提交时间）' % cur)
                continue
            by_instance.stamp_tree(path, inst.submitted_at)
            stamped += 1
        print('')
        print('盖时间完成 %d 个（目录和里面的文件都盖成提交时间）。' % stamped)
        for why in skipped:
            print('  · %s' % why)

    if notes:
        print('')
        print('未处理：')
        for note in notes:
            print('  · %s' % note)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
