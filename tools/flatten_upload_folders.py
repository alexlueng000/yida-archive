"""将已有“每次上传/单据/附件字段/文件”整理为“每次上传/单据/文件”。"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def flatten(base, manifest=None):
    base = Path(base).absolute()
    if not base.is_dir() or base.is_symlink() or base.resolve() != base:
        raise ValueError('目标必须是已存在的真实目录')
    moves, folders, stamps = [], [], {}
    for record in sorted(base.iterdir()):
        if not record.is_dir():
            continue
        if record.is_symlink() or record.resolve().parent != base:
            raise ValueError('记录目录包含链接')
        stat = record.stat()
        stamps[record] = (stat.st_atime_ns, stat.st_mtime_ns)
        taken = {p.name.casefold() for p in record.iterdir()}
        for field in sorted(record.iterdir()):
            if not field.is_dir():
                continue
            if field.is_symlink() or field.resolve().parent != record:
                raise ValueError('字段目录包含链接')
            folders.append(field)
            for source in sorted(field.iterdir()):
                if source.is_symlink() or not source.is_file() or source.resolve().parent != field:
                    raise ValueError('字段目录包含更深的目录或链接，停止整理：' + str(source))
                target = record / source.name
                index = 2
                while target.name.casefold() in taken:
                    target = record / ('%s_%d%s' % (source.stem, index, source.suffix))
                    index += 1
                taken.add(target.name.casefold())
                moves.append((source, target, digest(source)))
    result = {'root': str(base), 'records': len(stamps), 'files': len(moves),
              'removed_folders': len(folders), 'moves': [
                  {'from': str(a.relative_to(base)), 'to': str(b.relative_to(base)), 'sha256': h}
                  for a, b, h in moves]}
    if manifest is None:
        return result
    manifest = Path(manifest)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    completed = []
    try:
        for source, target, expected in moves:
            if target.exists() or digest(source) != expected:
                raise ValueError('文件在整理期间发生变化：' + str(source))
            source.rename(target)
            completed.append((source, target))
            if digest(target) != expected:
                raise ValueError('移动后内容校验失败：' + str(target))
        for folder in folders:
            folder.rmdir()  # 只移除已清空的字段目录
    except Exception:
        for source, target in reversed(completed):
            source.parent.mkdir(exist_ok=True)
            target.rename(source)
        raise
    finally:
        for folder, stamp in stamps.items():
            os.utime(folder, ns=stamp)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory')
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args()
    manifest = (Path(__file__).resolve().parents[1] / '.cache' /
                ('flatten-upload-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.json')) if args.write else None
    result = flatten(args.directory, manifest)
    result.pop('moves')
    result['manifest'] = str(manifest) if manifest else None
    print(json.dumps(result, ensure_ascii=False))
