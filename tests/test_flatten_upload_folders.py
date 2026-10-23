import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.flatten_upload_folders import flatten


class FlattenTests(unittest.TestCase):
    def test_preserves_collisions_content_and_record_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / '每次上传'
            record = root / '01-记录'
            for field, data in [('附件', b'one'), ('上传发票', b'two')]:
                folder = record / field
                folder.mkdir(parents=True)
                (folder / 'same.pdf').write_bytes(data)
            stamp = record.stat().st_mtime_ns
            result = flatten(root, Path(tmp) / 'manifest.json')
            self.assertEqual(result['files'], 2)
            self.assertEqual({p.read_bytes() for p in record.iterdir()}, {b'one', b'two'})
            self.assertEqual(record.stat().st_mtime_ns, stamp)
            self.assertEqual(flatten(root)['files'], 0)

    def test_unexpected_depth_leaves_original_files_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / '每次上传'
            folder = root / '01' / '附件'
            folder.mkdir(parents=True)
            (folder / 'a.txt').write_text('keep')
            (folder / 'nested').mkdir()
            with self.assertRaises(ValueError):
                flatten(root, Path(tmp) / 'manifest.json')
            self.assertEqual((folder / 'a.txt').read_text(), 'keep')


if __name__ == '__main__':
    unittest.main(verbosity=2)
