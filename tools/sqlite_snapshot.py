"""Build a read-only SQLite snapshot of existing archive metadata.

This is a local migration preview, not the application's authoritative store.
Existing JSON and archived attachments are never modified.
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = '''
CREATE TABLE projects (id INTEGER PRIMARY KEY, purchaser TEXT NOT NULL, project_no TEXT NOT NULL, catalogue_id TEXT, name TEXT, created INTEGER, raw_json TEXT, UNIQUE(purchaser, project_no));
CREATE TABLE ledgers (project_id INTEGER PRIMARY KEY REFERENCES projects(id), source_path TEXT NOT NULL, sha256 TEXT NOT NULL, raw_json TEXT NOT NULL);
CREATE TABLE attachments (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id), unit_key TEXT NOT NULL, archive_path TEXT, content_hash TEXT, category TEXT, original_name TEXT, raw_json TEXT NOT NULL, UNIQUE(project_id, unit_key));
CREATE TABLE observations (id INTEGER PRIMARY KEY, attachment_id INTEGER NOT NULL REFERENCES attachments(id), form_uuid TEXT, instance_id TEXT, form_name TEXT, instance_title TEXT, submitted_at INTEGER, approved_at INTEGER, file_uuid TEXT, original_name TEXT, raw_json TEXT NOT NULL);
CREATE TABLE metadata (source_path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, raw_json TEXT NOT NULL);
CREATE INDEX observations_instance ON observations(form_uuid, instance_id);
CREATE INDEX attachments_project ON attachments(project_id);
'''

def digest(data):
    return hashlib.sha256(data).hexdigest()

def build(root, cache, target):
    root, cache, target = root.resolve(), cache.resolve(), target.resolve()
    if target.exists():
        raise ValueError('目标数据库已存在，请选择新文件，避免覆盖已有数据')
    if root == target or root in target.parents:
        raise ValueError('数据库不能放在归档目录内')
    if cache == target or cache in target.parents:
        raise ValueError('数据库不能放在应用缓存目录内')
    if not root.is_dir():
        raise ValueError('归档目录不存在')
    sources = []
    for path in sorted(root.glob('*/*/编号记录.json')):
        if path.is_symlink() or path.parent.is_symlink() or path.parent.parent.is_symlink():
            raise ValueError('归档账本或项目目录包含符号链接: ' + str(path))
        sources.append((path, path.read_bytes(), 'ledger'))
    candidates = [cache / 'web-projects' / 'projects.json', cache / 'auth.json', cache / 'nightly-schedule.json', cache / 'nightly-schedule-state.json']
    candidates.extend(sorted((cache / 'date-extractions').glob('*.json')))
    candidates.extend(sorted((cache / 'web-projects').glob('*/session.json')))
    candidates.extend([cache / 'web-session' / 'session.json'])
    candidates.extend(sorted(root.glob('*/*/归类调整统计.json')))
    for path in candidates:
        if path.exists():
            if path.is_symlink(): raise ValueError('元数据包含符号链接: ' + str(path))
            sources.append((path, path.read_bytes(), 'metadata'))
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents accidental replacement of a previous snapshot.
    fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    con = None
    try:
        con = sqlite3.connect(str(target))
        con.execute('PRAGMA foreign_keys=ON')
        con.executescript(SCHEMA)
        con.execute('BEGIN IMMEDIATE')
        for path, data, kind in sources:
            obj = json.loads(data.decode('utf-8-sig'))
            text = json.dumps(obj, ensure_ascii=False, separators=(',', ':'))
            if kind == 'metadata':
                con.execute('INSERT INTO metadata VALUES(?,?,?)', (str(path), digest(data), text))
                if path == cache / 'web-projects' / 'projects.json':
                    if not isinstance(obj, dict): raise ValueError('项目索引格式无效')
                    for ident, item in obj.items():
                        if not isinstance(item, dict) or not item.get('purchaser') or not item.get('project_no'):
                            raise ValueError('项目索引条目无效: ' + str(ident))
                        con.execute('INSERT INTO projects(purchaser,project_no,catalogue_id,name,created,raw_json) VALUES(?,?,?,?,?,?) ON CONFLICT(purchaser,project_no) DO UPDATE SET catalogue_id=excluded.catalogue_id,name=excluded.name,created=excluded.created,raw_json=excluded.raw_json', (item['purchaser'],item['project_no'],ident,item.get('name'),item.get('created'),json.dumps(item,ensure_ascii=False)))
                continue
            if not isinstance(obj, dict) or obj.get('schema') != 2 or not obj.get('project') or not obj.get('purchaser') or not isinstance(obj.get('units'), dict):
                raise ValueError('账本格式无效: ' + str(path))
            con.execute('INSERT OR IGNORE INTO projects(purchaser,project_no) VALUES(?,?)', (obj['purchaser'], obj['project']))
            project = con.execute('SELECT id FROM projects WHERE purchaser=? AND project_no=?', (obj['purchaser'],obj['project'])).fetchone()[0]
            con.execute('INSERT INTO ledgers VALUES(?,?,?,?)', (project, str(path), digest(data), text))
            for key, unit in obj['units'].items():
                if not isinstance(unit, dict): raise ValueError('账本条目无效: ' + str(path))
                file = unit.get('file') or {}
                if not isinstance(file, dict): raise ValueError('附件记录无效: ' + str(path))
                cur = con.execute('INSERT INTO attachments(project_id,unit_key,archive_path,content_hash,category,original_name,raw_json) VALUES(?,?,?,?,?,?,?)', (project,key,unit.get('path'),unit.get('hash'),unit.get('type'),file.get('name'),json.dumps(unit,ensure_ascii=False)))
                for obs in file.get('obs') or []:
                    con.execute('INSERT INTO observations(attachment_id,form_uuid,instance_id,form_name,instance_title,submitted_at,approved_at,file_uuid,original_name,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?)', (cur.lastrowid,obs.get('form_uuid'),obs.get('instance_id'),obs.get('form_name'),obs.get('instance_title'),obs.get('submitted_at'),obs.get('approved_at'),obs.get('file_uuid'),obs.get('name'),json.dumps(obs,ensure_ascii=False)))
        for path, original, _ in sources:
            if path.read_bytes() != original: raise ValueError('导入期间源文件发生变化，请停止归档任务后重试: ' + str(path))
        con.commit()
        if con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise ValueError('数据库完整性检查失败')
        counts = {table: con.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ('projects','ledgers','attachments','observations','metadata')}
        return counts
    except Exception:
        if con is not None: con.rollback(); con.close(); con = None
        target.unlink(missing_ok=True)
        raise
    finally:
        if con is not None: con.close()

def main():
    parser = argparse.ArgumentParser(description='只读导入历史账本到新的 SQLite 快照，不修改原归档')
    parser.add_argument('--archive-root', type=Path, default=ROOT / '归档结果')
    parser.add_argument('--cache-root', type=Path, default=ROOT / '.cache')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(build(args.archive_root,args.cache_root,args.output),ensure_ascii=False,indent=2))
    except (ValueError, OSError, sqlite3.Error) as exc:
        print('导入失败：' + str(exc), file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
