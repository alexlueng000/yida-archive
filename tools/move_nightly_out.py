"""把夜间任务旧落盘目录并入网页「归类项目」读取的归档根目录。

夜间任务原来写到 ./archive，网页「归类项目」只读 ./归档结果。
两边都是 {采购单位}/{项目号}/编号记录.json 这一套结构，所以按项目目录搬迁即可，
账本里的 path 都是相对落盘根目录的，搬完不用改写。

默认只打印计划，加 --apply 才真的搬。遇到同名项目目录直接报错停下，
不覆盖、不合并，留给人确认。
"""
import argparse
import os
import sys
from pathlib import Path


def child_dirs(folder):
    return sorted(p for p in folder.iterdir() if p.is_dir() and not p.is_symlink())


def plan(source, target, only_ledgered=False):
    """only_ledgered 只搬已经有编号记录.json 的项目：夜间任务一边跑一边搬时，
    正在写的那一个还没落账本，跳过它就不会把写了一半的目录搬走。"""
    moves, skipped, conflicts = [], [], []
    if not source.is_dir():
        return moves, skipped, conflicts
    for group in child_dirs(source):           # 采购单位，或 _未归组项目
        for item in child_dirs(group):         # 项目号，或 _未归组项目 下的日期
            if only_ledgered and not (item / '编号记录.json').is_file():
                continue
            dest = target / group.name / item.name
            if dest.exists():
                conflicts.append((item, dest))
            else:
                moves.append((item, dest))
    return moves, skipped, conflicts


def main(argv=None):
    parser = argparse.ArgumentParser(description='把夜间任务旧目录并入归档结果')
    parser.add_argument('--source', default='archive')
    parser.add_argument('--target', default='归档结果')
    parser.add_argument('--apply', action='store_true', help='真的搬，不加只打印计划')
    parser.add_argument('--only-ledgered', action='store_true',
                        help='只搬已写完编号记录的项目，任务还在跑时用这个')
    args = parser.parse_args(argv)

    source, target = Path(args.source).resolve(), Path(args.target).resolve()
    moves, _, conflicts = plan(source, target, only_ledgered=args.only_ledgered)
    for item, dest in moves:
        ledger = '有账本' if (item / '编号记录.json').is_file() else '没有编号记录'
        print('搬 %s -> %s（%s）' % (item, dest, ledger))
    if conflicts:
        print('目标已存在同名目录，已停止，未做任何修改：')
        for item, dest in conflicts:
            print('  %s -> %s' % (item, dest))
        return 1
    if not args.apply:
        print('共 %d 个目录待搬；加 --apply 执行' % len(moves))
        return 0
    if os.path.splitdrive(source)[0] != os.path.splitdrive(target)[0]:
        print('源和目标不在同一个盘，不能用 rename，请手工处理')
        return 1
    target.mkdir(parents=True, exist_ok=True)
    for item, dest in moves:
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.rename(item, dest)
        print('已搬 %s' % dest)
    if not args.only_ledgered:
        # 任务还在跑时不动空目录，免得和它新建项目的时机撞上。
        for folder in sorted(source.rglob('*'), key=lambda p: -len(p.parts)):
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
        if source.is_dir() and not any(source.iterdir()):
            source.rmdir()
            print('已删除空目录 %s' % source)
    return 0


if __name__ == '__main__':
    sys.exit(main())
