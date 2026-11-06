"""SQLite account storage with safe import of legacy password hashes."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

SCHEMA = '''CREATE TABLE IF NOT EXISTS accounts (
    username TEXT PRIMARY KEY,
    salt TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    iterations INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)'''

def connect(path):
    db = sqlite3.connect(str(path), timeout=30)
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=30000')
    return db

def initialize(path):
    with closing(connect(path)) as db:
        with db:
            db.execute(SCHEMA)

def account(path):
    with closing(connect(path)) as db:
        row = db.execute('SELECT username, salt, password_hash, iterations FROM accounts LIMIT 1').fetchone()
        return dict(zip(('username', 'salt', 'hash', 'iterations'), row)) if row else None

def create_account(path, record, iterations):
    with closing(connect(path)) as db:
        with db:
            # Guard setup across processes, not just threads.
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM accounts LIMIT 1').fetchone():
                raise ValueError('账号已设置')
            db.execute('INSERT INTO accounts(username,salt,password_hash,iterations) VALUES(?,?,?,?)',
                       (record['username'], record['salt'], record['hash'], iterations))

def import_account(path, json_path, iterations):
    source = Path(json_path)
    if not source.exists():
        return False
    record = json.loads(source.read_text(encoding='utf-8-sig'))
    if not isinstance(record, dict) or not all(isinstance(record.get(k), str) and record[k] for k in ('username','salt','hash')):
        raise ValueError('原账号记录无效，未导入')
    if len(bytes.fromhex(record['salt'])) != 16 or len(bytes.fromhex(record['hash'])) != 32:
        raise ValueError('原账号记录无效，未导入')
    initialize(path)
    current = account(path)
    if current:
        if any(current[k] != record[k] for k in ('username','salt','hash')) or current['iterations'] != iterations:
            raise ValueError('数据库中已有不同账号，未覆盖')
        return False
    create_account(path, record, iterations)
    return True
