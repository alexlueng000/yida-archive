"""服务器不经人工确认：分类、排除、方向和输出决策的回归验证。"""
import unittest
from test_english_classification import classify_names
from yida_archive import classify
from yida_archive.config import Config


class TestAutomaticRules(unittest.TestCase):
    def test_payment_subject_beats_referenced_contract(self):
        for name in ['支付两方合同SRS部分尾款凭证.png',
                     '两方支付凭证（参考19-157-428合同）.pdf',
                     '垫片 支付凭证.png', '支付压电脉冲陶瓷阀全款凭证.png']:
            with self.subTest(name=name):
                files = classify_names(name)
                classify.mark_secret(files, Config())
                self.assertEqual(files[0].type_name, '付款凭证')
                self.assertTrue(files[0].secret)
                self.assertTrue(files[0].new_name)

    def test_credit_letter_and_transfer_are_payment_receipts(self):
        for name in [
            '23-50-10_进口信用证来单登记到单通知书.pdf',
            '23-50-10_进口信用证来单登记对外付款承兑通知书.pdf',
            'LC0730123A00404_cdf88081a2eb11eeaab200505692a007400ACo1.pdf',
            'TT.pdf', 'TT20261019.pdf', '项目号-TT付款.pdf',
            'lc0730123.pdf', 'L.C付款.pdf', 'T/T水单.pdf',
        ]:
            with self.subTest(name=name):
                file = classify_names(name)[0]
                self.assertEqual(file.type_name, '付款凭证')
                self.assertEqual(file.folder, '收付款')
                self.assertTrue(file.new_name.startswith('23-付款凭证'))

    def test_payment_abbreviations_do_not_match_inside_english_words(self):
        for name in ['attachment.pdf', 'welcome.pdf', 'letter.pdf', 'LC草稿.pdf']:
            with self.subTest(name=name):
                self.assertNotEqual(classify_names(name)[0].type_name, '付款凭证')

    def test_communication_and_unsupported_documents_skip(self):
        for name in ['发展确认结算表.png', '进口增值税发票.pdf', '发票.pdf',
                     '可行性测算表.xlsx', '机电产品报价.pdf', '最终用户名单.xlsx',
                     '离岸报价.pdf', '采购合同草稿.pdf', '箱单模板.xlsx',
                     'Pascking list sample.pdf', 'PO 779 作废.pdf']:
            with self.subTest(name=name):
                f = classify_names(name)[0]
                self.assertFalse(f.folder)
                self.assertEqual(f.new_name, name)
                self.assertTrue(f.review_note)

    def test_specific_aliases(self):
        for name, expected in [
            ('3183019442.awb.202405021725340003.tif', '运单/提单'),
            ('FedEx-AWB-5Sep2024.pdf', '运单/提单'),
            ('Pascking list-Spare gaskets.pdf', '箱单'), ('PL--C202400252 供货商.pdf', '箱单'),
            ('内贸补充合同.pdf', '中标合同（内贸）'), ('外贸补充合同.pdf', '外贸报关合同'),
            ('结算单24-157-401 0820.xlsx', '结算表'), ('发票（进口）.pdf', '发票（进口）')]:
            with self.subTest(name=name):
                self.assertEqual(classify_names(name)[0].type_name, expected)

    def test_water_receipt_requires_payment_context(self):
        files = classify_names('水单USD21264.pdf')
        self.assertEqual(files[0].folder, '收付款')
        self.assertEqual(files[0].confidence, 'manual')
        self.assertFalse(files[0].type_name)
        files[0].obs[0].form_name = '货款支付'
        classify.classify(files, Config())
        self.assertEqual(files[0].type_name, '付款凭证')

    def test_downloaded_payment_folder_identifies_water_receipt(self):
        for name in ('两方水单-70%.png', '三方水单1.pdf'):
            f = classify_names(name)[0]
            f.obs[0].form_name = '06-20230921-1038-货款支付-13件-项目'
            classify.classify([f], Config())
            self.assertEqual(f.type_name, '付款凭证')
        f = classify_names('水单.pdf')[0]
        f.obs[0].form_name = '06-20230921-1038-其他流程-13件-标题提到货款支付'
        classify.classify([f], Config())
        self.assertFalse(f.type_name)

    def test_payment_direction(self):
        for name, expected in [('退款凭证.pdf', '退款凭证'), ('收款凭证.pdf', '收款凭证'),
                               ('收汇回单.pdf', '收款凭证')]:
            self.assertEqual(classify_names(name)[0].type_name, expected)

    def test_conflicting_names_or_field_mappings_skip(self):
        self.assertFalse(classify_names('采购合同和装箱单.pdf')[0].folder)
        cfg = Config({'classification': {'field_hints': [
            {'field_label': '附件', 'folder': '合同', 'type': '协议'},
            {'field_label': '附件', 'folder': '合同', 'type': '采购合同'}]}})
        self.assertFalse(classify_names('扫描件.pdf', cfg=cfg)[0].folder)

    def test_exclusion_beats_generic_field_mapping(self):
        cfg = Config({'classification': {'field_hints': [
            {'field_label': '附件', 'folder': '合同', 'type': '采购合同'}]}})
        self.assertFalse(classify_names('采购合同聊天记录.png', cfg=cfg)[0].folder)

    def test_receipt_aliases_with_conflicting_direction_skip(self):
        files = classify_names('付款凭证.pdf', '收款凭证.pdf')
        files[0].obs.extend(files[1].obs)
        classify.classify(files[:1], Config())
        self.assertFalse(files[0].folder)
        self.assertIn('冲突', files[0].review_note)
