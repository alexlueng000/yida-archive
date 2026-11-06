"""Transactional storage primitives for the local SQLite migration."""
import json
import sqlite3
from contextlib import closing

class RevisionConflict(Exception):
    pass

def connect(path):
    con = sqlite3.connect(str(path), timeout=30)
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA busy_timeout=30000')
    return con

def initialize(path):
    with closing(connect(path)) as con:
        with con:
            con.execute('CREATE TABLE IF NOT EXISTS archive_state (purchaser TEXT NOT NULL, project_no TEXT NOT NULL, revision INTEGER NOT NULL, ledger TEXT NOT NULL, PRIMARY KEY(purchaser,project_no))')

def read(path, purchaser, project_no):
    with closing(connect(path)) as con:
        row = con.execute('SELECT revision, ledger FROM archive_state WHERE purchaser=? AND project_no=?', (purchaser,project_no)).fetchone()
    return (row[0], json.loads(row[1])) if row else (0, None)

def write(path, purchaser, project_no, ledger, expected_revision):
    if ledger.get('schema') != 2 or ledger.get('purchaser') != purchaser or ledger.get('project') != project_no:
        raise ValueError('编号账本与项目不匹配')
    payload = json.dumps(ledger, ensure_ascii=False, separators=(',', ':'))
    with closing(connect(path)) as con:
        with con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT revision FROM archive_state WHERE purchaser=? AND project_no=?', (purchaser,project_no)).fetchone()
            current = row[0] if row else 0
            if current != expected_revision:
                raise RevisionConflict('项目账本已被其他任务更新，请重新读取')
            if row:
                con.execute('UPDATE archive_state SET revision=?, ledger=? WHERE purchaser=? AND project_no=?', (current+1,payload,purchaser,project_no))
            else:
                con.execute('INSERT INTO archive_state VALUES(?,?,?,?)', (purchaser,project_no,1,payload))
            return current+1
