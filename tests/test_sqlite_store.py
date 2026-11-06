import tempfile, unittest
from pathlib import Path
from yida_archive import sqlite_store as store

class StoreTest(unittest.TestCase):
    def test_revision_and_rollback(self):
        with tempfile.TemporaryDirectory() as folder:
            db=Path(folder)/'archive.sqlite'
            store.initialize(db)
            data={'schema':2,'purchaser':'甲','project':'P1','types':{},'units':{'reserved':{'hashes':{}}}}
            self.assertEqual(store.read(db,'甲','P1'),(0,None))
            self.assertEqual(store.write(db,'甲','P1',data,0),1)
            rev, loaded=store.read(db,'甲','P1')
            self.assertEqual(loaded,data)
            with self.assertRaises(store.RevisionConflict): store.write(db,'甲','P1',data,0)
            self.assertEqual(store.read(db,'甲','P1'),(rev,data))
            with self.assertRaises(ValueError): store.write(db,'甲','P2',data,0)
if __name__=='__main__': unittest.main()
