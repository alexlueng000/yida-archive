"""Compare a preview database with its unchanged ledger files and archived bytes."""
import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

def file_hash(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def verify(database, root):
    root = root.resolve()
    con = sqlite3.connect('file:' + database.resolve().as_posix() + '?mode=ro', uri=True)
    errors = []
    counts = {'ledgers': 0, 'files_checked': 0, 'units_without_path': 0}
    try:
        if con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            errors.append('数据库完整性检查失败')
        if con.execute('PRAGMA foreign_key_check').fetchone():
            errors.append('数据库外键检查失败')
        for source, expected in con.execute('SELECT source_path, sha256 FROM ledgers'):
            counts['ledgers'] += 1
            p = Path(source)
            if not p.is_file() or file_hash(p) != expected:
                errors.append('账本缺失或发生变化: ' + source)
        for project, key, path, expected in con.execute('SELECT project_id, unit_key, archive_path, content_hash FROM attachments'):
            if not path:
                counts['units_without_path'] += 1
                continue
            p = (root / path).resolve()
            if root not in p.parents or not p.is_file():
                errors.append('附件缺失或路径越界: ' + path)
            elif not expected:
                errors.append('附件缺少哈希: ' + path)
            elif file_hash(p) != expected:
                errors.append('附件哈希不一致: ' + path)
            else:
                counts['files_checked'] += 1
        return counts, errors
    finally:
        con.close()

def main():
    p = argparse.ArgumentParser(description='校验 SQLite 预览快照的账本与附件；只读，不修改文件')
    p.add_argument('--database', type=Path, required=True)
    p.add_argument('--archive-root', type=Path, required=True)
    a = p.parse_args()
    try:
        counts, errors = verify(a.database, a.archive_root)
        print(json.dumps({'counts': counts, 'errors': errors}, ensure_ascii=False, indent=2))
        return 1 if errors else 0
    except (OSError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 1
if __name__ == '__main__':
    raise SystemExit(main())
