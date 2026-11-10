import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_archive_v2 import attachment, grouped, prepare, ContentClient
from test_ai_classification import decision, SECTIONS
from tools import reclassify_pending as tool
from yida_archive import archive, numbering, ai_classify, document_text
from yida_archive.config import Config


class ReclassifyAllTests(unittest.TestCase):
    def test_all_rechecks_confirmed_and_persists_same_type_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = Config({'scope': {'project_no': 'P1', 'project_secret': False},
                          'storage': {'out_dir': root, 'work_dir': root + '/cache'},
                          'file_reviews': {},
                          'ai': {'enabled': True, 'model': 'fake', 'base_url': 'https://example.invalid/v1'}})
            files = prepare(grouped(attachment('采购合同.docx', 'a', b'body')), cfg)
            done, failed = archive.write(files, cfg, ContentClient({'a': b'body'}), root)
            self.assertFalse(failed)
            numbering.finalize(cfg, files, done)
            ledger = numbering.project_dir(cfg) / numbering.FILENAME
            original = ledger.read_bytes()
            import os
            with patch.object(tool.web, '_config', return_value=cfg), patch.object(tool.web, 'OUTPUT_ROOT', root), patch.dict(tool.web.STATE, blobs={}), patch.dict(os.environ, ARCHIVE_AI_API_KEY='test'), patch.object(document_text, 'extract', return_value=SECTIONS), patch.object(ai_classify.Client, 'classify', return_value=(decision('采购合同'), {})) as call:
                self.assertEqual(tool.run_project(ledger, False), 0)
                call.assert_not_called()
                self.assertEqual(tool.run_project(ledger, False, recheck_all=True), 1)
                self.assertEqual(call.call_count, 1)
                self.assertEqual(ledger.read_bytes(), original)
                self.assertEqual(tool.run_project(ledger, True, recheck_all=True), 1)
                self.assertEqual(call.call_count, 1)  # Revalidates cached model output.
            unit = next(iter(json.loads(ledger.read_text(encoding='utf-8'))['units'].values()))
            self.assertEqual(unit['file']['ai_result']['status'], 'classified')
            self.assertTrue(unit['file']['ai_result']['cached'])

    def test_all_keeps_manual_type(self):
        from types import SimpleNamespace
        f = SimpleNamespace(type_name='采购合同', ident='manual', name='采购合同.docx')
        cfg = SimpleNamespace(raw={'file_reviews': {'manual': {'type': '采购合同'}}})
        with tempfile.TemporaryDirectory() as root:
            ledger = Path(root) / '编号记录.json'
            ledger.write_text(json.dumps({'purchaser': 'A', 'project': 'P1'}), encoding='utf-8')
            with patch.object(tool.web, '_config', return_value=cfg), patch.object(numbering, 'project_dir', return_value=Path(root)), patch.object(numbering, 'include_history', return_value=[f]), patch.object(tool, 'result_snapshot', return_value='same'), patch.object(tool.zip_members, 'expand'), patch('yida_archive.document_dedupe.merge', return_value=[f]), patch.object(ai_classify, 'apply') as apply:
                self.assertEqual(tool.run_project(ledger, True, recheck_all=True), 0)
                apply.assert_not_called()
