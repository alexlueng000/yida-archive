"""英文附件归类及容易误判的业务边界。"""
import unittest

from yida_archive import classify, dedupe, naming
from yida_archive.config import Config
from yida_archive.models import AttachmentObs, Instance


def classify_names(*names, cfg=None):
    cfg = cfg or Config({'scope': {'project_no': 'P1', 'project_secret': False}})
    obs = [AttachmentObs('流程', 'F', 'I', '', 1, 'a', '附件',
                         name, 4, str(i), '', '') for i, name in enumerate(names)]
    files = dedupe.group([Instance('流程', 'F', 'I', '', 1, attachments=obs)])[0]
    classify.classify(files, cfg)
    naming.assign(files, cfg, cfg.project_no)
    return files


class TestEnglishClassification(unittest.TestCase):
    def test_contract_communications_are_not_contracts(self):
        for name in ['发展确认两方合同迟交货和不能交货条款.png',
                     '发展确认两方合同迟交货和不能交货条款2.png',
                     '采购合同聊天记录.pdf', '采购合同条款对比.docx',
                     '发展确认垫付预付款残余气体分析仪等设备两方合同.png']:
            with self.subTest(name=name):
                f = classify_names(name)[0]
                self.assertEqual(f.type_name, '')
                self.assertEqual(f.new_name, name)
                self.assertIn('合同正文', f.review_note)

    def test_contract_images_and_confirmed_versions_still_classify(self):
        for name in ['采购合同扫描件.png', '两方合同（进口货物）修订.doc',
                     'Your confirned PO 799-2024 厂商确认采购合同版本.jpg']:
            with self.subTest(name=name):
                self.assertEqual(classify_names(name)[0].type_name, '采购合同')

    def test_review_can_confirm_contract_supporting_material(self):
        cfg = Config({})
        files = classify_names('采购合同聊天记录.png', cfg=cfg)
        cfg.raw['file_reviews'] = {files[0].ident: {'type': '采购合同'}}
        classify.classify(files, cfg)
        self.assertEqual(files[0].type_name, '采购合同')

    def test_user_examples_and_output_paths(self):
        files = classify_names(
            'Invoice-387-3-2024-Spare gaskets-Tiangang Yang-by Precise Instruments COM. LIM.pdf',
            'Packing list-Spare gaskets-Tiangang Yang-by Precise Instruments COM. LIM.pdf',
            'PO 779-2024.pdf')
        self.assertEqual({(f.folder, f.type_name, f.confidence) for f in files}, {
            ('清关送货', '发票（进口）', 'suggested'), ('清关送货', '箱单', 'confirmed'),
            ('合同', '采购合同', 'suggested')})
        for f in files:
            self.assertIn(f.type_name, f.new_name)
            self.assertTrue(f.new_name.endswith('-P1.pdf'))
            self.assertEqual(naming.target_dir(f, Config({'scope': {'project_no': 'P1', 'project_secret': False}})).replace('\\', '/'),
                             '未指定采购单位/P1/公共/' + f.folder)

    def test_case_separators_and_order_numbers(self):
        for name, kind in [('PACKING_LIST.pdf', '箱单'), ('packing-list.pdf', '箱单'),
                           ('Purchase_Order.pdf', '采购合同'), ('P.O.779.pdf', '采购合同'),
                           ('PO779.pdf', '采购合同'), ('INVOICE_123.pdf', '发票（进口）')]:
            with self.subTest(name=name):
                self.assertEqual(classify_names(name)[0].type_name, kind)

    def test_word_boundaries(self):
        for name in ['report.pdf', 'support.pdf', 'policy.pdf', 'POA.pdf', 'invoiced.pdf']:
            with self.subTest(name=name):
                self.assertEqual(classify_names(name)[0].type_name, '')

    def test_samples_drafts_and_proforma(self):
        for name in ['sample of PO-779-2024.docx', 'PO draft.pdf', 'Invoice_template.pdf',
                     'Proforma Invoice.pdf', 'Pro-forma invoice.pdf', 'PO采购合同草稿.pdf']:
            with self.subTest(name=name):
                f = classify_names(name)[0]
                self.assertEqual(f.type_name, '')
                self.assertTrue(f.review_note)

    def test_explicit_domestic_type_wins(self):
        self.assertEqual(classify_names('增值税发票-Invoice.pdf')[0].type_name, '发票（国内货物类）')

    def test_combined_document_needs_review(self):
        f = classify_names('Invoice and Packing List.pdf')[0]
        self.assertEqual(f.type_name, '')
        self.assertIn('多个', f.review_note)

    def test_custom_rules_are_respected(self):
        cfg = Config({'classification': {'rules': [
            {'folder': '合同', 'type': '协议', 'keywords': ['协议']}]}})
        self.assertEqual(classify_names('PO779.pdf', cfg=cfg)[0].type_name, '')

    def test_manual_review_can_override_sample_guard(self):
        cfg = Config({})
        files = classify_names('sample of PO.docx', cfg=cfg)
        cfg.raw['file_reviews'] = {files[0].ident: {'type': '采购合同'}}
        classify.classify(files, cfg)
        self.assertEqual(files[0].type_name, '采购合同')
        self.assertEqual(files[0].confidence, 'confirmed')
