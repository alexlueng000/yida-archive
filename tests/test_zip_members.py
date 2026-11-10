import io
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from pathlib import Path

from test_ai_classification import files_for, decision
from yida_archive import zip_members, classify, ai_classify, naming
from yida_archive.config import Config


class ZipTests(unittest.TestCase):
    def test_unreadable_contract_falls_back_and_names_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config({'ai': {'enabled': True},
                          'storage': {'work_dir': tmp, 'out_dir': tmp}})
            files = files_for('双章合同.pdf')
            classify.classify(files, cfg)
            with patch.object(ai_classify, 'pdf_text_layer', side_effect=ai_classify.AIError('读取失败')):
                ai_classify.apply(files, cfg, lambda f: b'body')
            naming.assign(files, cfg, 'P1')
            self.assertEqual(files[0].type_name, '合同')
            self.assertEqual(files[0].new_name, '合同-双章合同.pdf')

    def test_members_classified_deduplicated_and_paths_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config({'storage': {'work_dir': tmp, 'out_dir': tmp},
                          'scope': {'project_no': 'P1'}})
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w') as z:
                z.writestr('资料/收款小条.pdf', b'one')
                z.writestr('复制/收款小条.pdf', b'one')
                z.writestr('../escape.pdf', b'bad')
            data = buf.getvalue()
            files = files_for('附件.zip', data)
            zip_members.expand(files, cfg, lambda f: data)
            self.assertEqual(len(files), 2)
            self.assertEqual(len(files[1].obs), 2)
            self.assertEqual(Path(files[1].cache_path).read_bytes(), b'one')
            classify.classify(files, cfg)
            naming.assign(files, cfg, 'P1')
            self.assertEqual(files[1].folder, '结算移交')
            zip_members.expand(files, cfg, lambda f: data)
            self.assertEqual(len(files), 2)

    def test_evidence_normalization_still_requires_same_page(self):
        result = decision()
        result['evidence'] = [{'location': '第1页', 'quote': '货物数量１０件'}]
        sections = [{'location': '第1页', 'text': '货物 数量\n10 件'}]
        ai_classify.validate(result, sections, Config().rules, 0.9)
        result['evidence'][0]['location'] = '第2页'
        with self.assertRaises(ai_classify.AIError):
            ai_classify.validate(result, sections, Config().rules, 0.9)
