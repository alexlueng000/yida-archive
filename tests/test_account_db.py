import hashlib, json, sqlite3, tempfile, unittest
from pathlib import Path
from web import account_db

class AccountDBTest(unittest.TestCase):
    def test_import_preserves_account_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); db=root/'a.sqlite'; legacy=root/'auth.json'
            salt=bytes(range(16)); digest=hashlib.pbkdf2_hmac('sha256',b'password123',salt,600000).hex()
            record={'username':'admin','salt':salt.hex(),'hash':digest}
            legacy.write_text(json.dumps(record), encoding='utf-8')
            account_db.initialize(db)
            self.assertTrue(account_db.import_account(db,legacy,600000))
            self.assertFalse(account_db.import_account(db,legacy,600000))
            self.assertEqual(account_db.account(db)['hash'],digest)
            record['username']='other'; legacy.write_text(json.dumps(record), encoding='utf-8')
            with self.assertRaises(ValueError): account_db.import_account(db,legacy,600000)
            self.assertEqual(account_db.account(db)['username'],'admin')
            with self.assertRaises(ValueError): account_db.create_account(db,record,600000)
if __name__=='__main__': unittest.main()
