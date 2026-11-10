import json
import tempfile
import unittest
from pathlib import Path
from test_archive_v2 import attachment, grouped, prepare, ContentClient
from yida_archive import archive, naming, numbering
from yida_archive.config import Config


class PendingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config({'scope': {'project_no': 'P1', 'project_secret': False},
                           'storage': {'out_dir': self.tmp.name, 'pending_folder': '未归类'}})
        files = prepare(grouped(attachment('未知资料.dat', 'a', b'pending')), self.cfg)
        done, failed = archive.write(files, self.cfg, ContentClient({'a': b'pending'}), self.tmp.name)
        self.assertFalse(failed)
        numbering.finalize(self.cfg, files, done)
        self.path = Path(done[0][1])
        self.ledger = numbering.project_dir(self.cfg) / numbering.FILENAME

    def test_repeated_naming_preserves_pending_metadata(self):
        before = numbering.load(self.cfg)
        files = numbering.include_history([], self.cfg)
        naming.assign(files, self.cfg, 'P1')
        numbering.finalize(self.cfg, [], [])
        self.assertEqual(numbering.load(self.cfg), before)
        self.assertEqual(len(numbering.include_history([], self.cfg)), 1)

    def test_recovers_empty_or_stale_path_by_hash_without_writing_preview(self):
        for old in ('', 'old/missing.dat'):
            data = json.loads(self.ledger.read_text(encoding='utf-8'))
            unit = next(iter(data['units'].values()))
            unit.update(path=old, hash='')
            unit.pop('file', None)
            self.ledger.write_text(json.dumps(data), encoding='utf-8')
            original = self.ledger.read_bytes()
            files = numbering.include_history([], self.cfg)
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].name, '未知资料.dat')
            self.assertEqual(Path(files[0].cache_path), self.path)
            self.assertEqual(self.ledger.read_bytes(), original)

    def test_different_content_does_not_recover(self):
        data = json.loads(self.ledger.read_text(encoding='utf-8'))
        next(iter(data['units'].values())).update(path='', hash='')
        self.ledger.write_text(json.dumps(data), encoding='utf-8')
        self.path.write_bytes(b'changed')
        self.assertEqual(numbering.include_history([], self.cfg), [])
