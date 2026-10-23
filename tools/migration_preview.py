"""只读历史归档，输出旧路径到新路径预演，不移动或删除任何附件。"""
import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from yida_archive import archive, naming
from yida_archive.config import load, canonical_type, TYPE_NUMBERS


def preview(source, cfg):
    source = Path(source).resolve()
    rows, issues = [], []
    ledger = source / '编号记录.json'
    if ledger.exists():
        state = json.loads(ledger.read_text(encoding='utf-8'))
        if state.get('project') != cfg.project_no:
            raise ValueError('历史账本的项目号与配置不一致')
        for unit in state.get('units', {}).values():
            if unit.get('path'):
                rows.append({'path': unit['path'], 'type': unit.get('type', ''),
                             'hash': unit.get('hash', ''), 'version': unit.get('latest', 1),
                             'name': (unit.get('file') or {}).get('name', '')})
    else:
        manifest = next((source / n for n in ('清单.csv', 'archive-清单.csv')
                         if (source / n).is_file()), None)
        if manifest is None:
            raise ValueError('历史目录中没有编号记录或归档清单')
        with manifest.open(encoding='utf-8-sig', newline='') as fh:
            for row in csv.DictReader(fh):
                if row.get('新文件名'):
                    rows.append({'path': str(Path(row['归档目录']) / row['新文件名']),
                                 'type': row.get('类型', ''), 'hash': row.get('内容哈希', ''),
                                 'version': int(row.get('版本号') or 1),
                                 'name': row.get('原始文件名', '')})
    if cfg.project_secret is None:
        issues.append('尚未确认是否南科大企业项目，目标路径暂按加密区预演')
    allowed = {r['type']: r['folder'] for r in cfg.rules}
    counts = Counter(canonical_type(r['type']) for r in rows if canonical_type(r['type']) in allowed)
    used, subs, tracked = set(), defaultdict(int), set()
    result = []
    for row in sorted(rows, key=lambda r: r['path']):
        # 历史路径以输出根为基准；只能读取指定项目目录内的普通文件。
        rel = Path(row['path'])
        candidates = [Path(cfg.out_dir).resolve() / rel, source.parent / rel, source / rel]
        old = next((p.resolve() for p in candidates if p.is_file()
                    and source in p.resolve().parents), None)
        if old is None or old.is_symlink():
            issues.append('历史附件缺失或路径不合法：' + row['path'])
            continue
        if any(p.is_symlink() for p in [old, *old.parents] if p == source or source in p.parents):
            issues.append('历史附件包含链接：' + row['path'])
            continue
        if old in tracked:
            continue
        tracked.add(old)
        digest = archive._sha256(old)
        if not row['hash'] or digest != row['hash']:
            issues.append('附件哈希缺失或与清单不符：' + row['path'])
        kind = canonical_type(row['type'])
        folder = allowed.get(kind, '')
        if folder:
            subs[kind] += 1
            label = naming.sanitize(cfg.type_label(kind))
            if row['version'] > 1:
                label += 'v%d' % row['version']
            pattern = cfg.multi_pattern if counts[kind] > 1 else cfg.pattern
            name = naming.sanitize(pattern.format(seq=TYPE_NUMBERS[kind], sub=subs[kind],
                                                  type=label, project=naming.sanitize(cfg.project_no),
                                                  ext=old.suffix))
        else:
            name = naming.sanitize(row['name'] or old.name)
        stub = SimpleNamespace(secret=cfg.project_secret is not False, folder=folder, type_name=kind)
        dest = (Path(cfg.out_dir) / naming.target_dir(stub, cfg) / name).resolve()
        if Path(cfg.out_dir).resolve() not in dest.parents:
            raise ValueError('目标路径超出输出根目录')
        target = archive._dedupe_path(str(dest), used)
        used.add(target.lower())
        result.append({'old_path': str(old), 'new_path': target, 'sha256': digest,
                       'type': kind if folder else '待归类'})
    untracked = [str(p) for p in source.rglob('*') if p.is_file() and p.resolve() not in tracked
                 and p.name not in {'编号记录.json', '清单.csv', 'archive-清单.csv', '结果.md',
                                    'archive-结果.md', '落盘清单.tsv', 'archive-落盘清单.tsv',
                                    '人工确认.json', '归档规范.json'}]
    return {'mode': 'preview_only', 'project': cfg.project_no, 'purchaser': cfg.purchaser,
            'files': result, 'issues': issues, 'untracked_files': untracked,
            'can_migrate': not issues and not untracked}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, help='历史项目归档目录')
    parser.add_argument('--config', required=True)
    parser.add_argument('--output', help='预演JSON文件；不指定则输出到控制台')
    args = parser.parse_args()
    result = preview(args.source, load(args.config))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        dest = Path(args.output).resolve()
        source = Path(args.source).resolve()
        if dest == source or source in dest.parents or dest.exists():
            raise ValueError('预演文件必须为历史目录之外的新文件，不能覆盖已有文件')
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding='utf-8')
        print('已生成只读预演：' + str(dest))
    else:
        print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
