import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from tools.sqlite_snapshot import build
from tools.sqlite_verify import verify

class SnapshotTests(unittest.TestCase):
    def test_import_and_verify_without_touching_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); root = base / 'archive'; cache = base / 'cache'; dest = base / 'preview.db'
            project = root / 'Buyer' / 'P1'; project.mkdir(parents=True)
            attachment = project / 'contract.pdf'; attachment.write_bytes(b'contract')
            sha = hashlib.sha256(b'contract').hexdigest()
            ledger = {'schema': 2, 'purchaser': 'Buyer', 'project': 'P1', 'units': {'u1': {'path': 'Buyer/P1/contract.pdf', 'hash': sha, 'type': 'contract', 'file': {'name': 'origin.pdf', 'obs': [{'instance_id':'instance1'}]}}, 'reserved': {'hashes': {}}}}
            ledger_path = project / '编号记录.json'; ledger_path.write_text(json.dumps(ledger), encoding='utf-8')
            catalog = cache / 'web-projects' / 'projects.json'; catalog.parent.mkdir(parents=True)
            catalog.write_text(json.dumps({'id1': {'purchaser':'Buyer','project_no':'P1'}, 'id2': {'purchaser':'Buyer','project_no':'P2'}}), encoding='utf-8')
            before = ledger_path.read_bytes()
            counts = build(root, cache, dest)
            self.assertEqual((counts['projects'], counts['attachments'], counts['observations']), (2, 2, 1))
            self.assertEqual(ledger_path.read_bytes(), before)
            result, errors = verify(dest, root)
            self.assertEqual(errors, [])
            self.assertEqual((result['files_checked'], result['units_without_path']), (1, 1))
            with self.assertRaises(ValueError): build(root, cache, dest)
            attachment.write_bytes(b'changed')
            self.assertTrue(verify(dest, root)[1])

if __name__ == '__main__': unittest.main()
