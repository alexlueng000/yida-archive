"""V1.5需求验收：跨项目编号、待归类持久化、项目保密及历史迁移保护。"""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_archive_v2 import attachment, grouped, prepare, ContentClient
from yida_archive import archive, classify, naming, numbering
from yida_archive.config import Config
from tools.migration_preview import preview
from web import server as web


class RequirementsV15(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = Config({'scope': {'project_no': 'P1', 'purchaser': '甲单位',
                                    'project_secret': False},
                           'storage': {'out_dir': self.temp.name}})

    def test_fixed_numbers_survive_upload_order_and_unit_whitelist(self):
        self.cfg.raw['purchasers'] = {'甲单位': {'classification': {
            'allowed_types': ['付款凭证', '箱单', '采购合同']}}}
        files = prepare(grouped(attachment('装箱单.pdf', 'a', b'A', 1),
                                attachment('采购合同.pdf', 'b', b'B', 2),
                                attachment('付款凭证.png', 'c', b'C', 3)), self.cfg)
        self.assertEqual([f.new_name for f in files], [
            '15-箱单-P1.pdf', '3-采购合同-P1.pdf', '23-付款凭证（财务部）-P1.png'])
        self.cfg.raw['scope']['project_no'] = 'P2'
        other = prepare(grouped(attachment('付款凭证.png', 'd', b'D', 1)), self.cfg)
        self.assertEqual(other[0].new_name, '23-付款凭证（财务部）-P2.png')

    def test_catalog_has_28_types_and_required_boundary_numbers(self):
        self.assertEqual(len(self.cfg.rules), 28)
        ordinals = classify.type_ordinal(self.cfg)
        for key, expected in {('办证', '彩页说明书'): 6,
                              ('办证', '减免税进口货物说明表'): 7,
                              ('清关送货', '发货单/送货单/货物签收单'): 20,
                              ('收付款', '收款凭证'): 21,
                              ('结算移交', '发票（国内货物类）'): 24,
                              ('结算移交', '发票（国内服务类）'): 25,
                              ('结算移交', '移交资料收件回执'): 28}.items():
            self.assertEqual(ordinals[key], expected)
        for name in ('发货单.pdf', '送货单.pdf', '货物签收单.pdf'):
            file = prepare(grouped(attachment(name, name, name.encode())), self.cfg)[0]
            self.assertTrue(file.new_name.startswith('20-'))

    def test_pending_files_survive_save_and_restart_with_original_names(self):
        files = prepare(grouped(attachment('未知资料.dat', 'a', b'UNKNOWN')), self.cfg)
        client = ContentClient({'a': b'UNKNOWN'})
        done, failed = archive.write(files, self.cfg, client, self.temp.name)
        self.assertFalse(failed)
        target = Path(done[0][1])
        self.assertEqual(target, Path(self.temp.name) / '甲单位/P1/公共/待归类/未知资料.dat')
        numbering.finalize(self.cfg, files, done)
        restarted = Config(copy.deepcopy(self.cfg.raw))
        history = numbering.include_history([], restarted)
        prepare(history, restarted)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].new_name, '未知资料.dat')
        self.assertEqual(target.read_bytes(), b'UNKNOWN')

    def test_unit_disallowed_type_is_pending_not_lost(self):
        self.cfg.raw['purchasers'] = {'甲单位': {'classification': {'allowed_types': ['箱单']}}}
        file = prepare(grouped(attachment('采购合同.pdf', 'a', b'A')), self.cfg)[0]
        self.assertFalse(file.folder)
        self.assertEqual(file.new_name, '采购合同.pdf')
        self.assertTrue(naming.target_dir(file, self.cfg).endswith('/公共/待归类'))

    def test_project_secret_applies_to_contract_payment_and_pending(self):
        files = grouped(attachment('采购合同.pdf', 'a', b'A'),
                        attachment('付款凭证.png', 'b', b'B'),
                        attachment('未知.bin', 'c', b'C'))
        for secret in (True, False):
            self.cfg.raw['scope']['project_secret'] = secret
            prepare(files, self.cfg)
            self.assertTrue(all(f.secret is secret for f in files))
            root = '加密' if secret else '公共'
            self.assertTrue(all('/' + root + '/' in naming.target_dir(f, self.cfg) for f in files))

    def test_missing_secret_blocks_write_and_export(self):
        self.cfg.raw['scope']['project_secret'] = None
        files = prepare(grouped(attachment('采购合同.pdf', 'a', b'A')), self.cfg)
        with self.assertRaisesRegex(ValueError, '是否南科大企业项目'):
            archive.write(files, self.cfg, ContentClient({'a': b'A'}), self.temp.name)
        self.assertFalse(list(Path(self.temp.name).rglob('*.pdf')))
        with patch.object(web, 'OUTPUT_ROOT', self.temp.name), \
             patch.dict(web.STATE, {'project_secret': '', 'last_result': {'ok': True}}):
            with self.assertRaisesRegex(ValueError, '是否南科大企业项目'):
                web.export_zip()

    def test_old_ledger_is_blocked_and_preview_does_not_modify_files(self):
        source = Path(self.temp.name) / 'P1_文件分类'
        old = source / '公共/合同/1-采购合同-P1.pdf'
        old.parent.mkdir(parents=True)
        old.write_bytes(b'OLD')
        ledger = {'schema': 1, 'project': 'P1', 'units': {'a': {
            'path': 'P1_文件分类/公共/合同/1-采购合同-P1.pdf', 'type': '采购合同',
            'hash': hashlib.sha256(b'OLD').hexdigest(), 'latest': 1}}}
        (source / '编号记录.json').write_text(json.dumps(ledger), encoding='utf-8')
        before = {p: p.read_bytes() for p in source.rglob('*') if p.is_file()}
        with self.assertRaisesRegex(ValueError, '迁移'):
            numbering.load(self.cfg)
        result = preview(source, self.cfg)
        self.assertTrue(result['can_migrate'], result)
        self.assertEqual(result['files'][0]['new_path'], str(
            Path(self.temp.name) / '甲单位/P1/公共/合同/3-采购合同-P1.pdf'))
        self.assertEqual(before, {p: p.read_bytes() for p in source.rglob('*') if p.is_file()})

    def test_manual_classification_persists_after_state_reset(self):
        body = b'UNKNOWN'
        digest = hashlib.sha256(body).hexdigest()
        fresh = dict(project_no='P1', purchaser='甲单位', project_secret='否',
                     unmatched_policy='manual', last_result=None,
                     file_reviews={digest: {'type': '箱单'}}, blobs={'a': body},
                     batches=[{'id': 'b', 'name': '上传', 'created': 1, 'files': [{
                         'name': '未知资料.bin', 'size': len(body), 'uuid': 'a', 'sha256': digest}]}])
        with patch.object(web, 'STATE', fresh), patch.object(web, 'OUTPUT_ROOT', self.temp.name), \
             patch.object(web, 'SERVER_CONFIG', {}):
            self.assertTrue(web.save_result()['ok'])
            web.STATE['file_reviews'] = {}
            result = web.analyze()
            self.assertEqual(result['files'][0]['type'], '箱单')
            self.assertEqual(web._config().raw['file_reviews'][digest]['type'], '箱单')

    def test_purchasers_with_same_project_do_not_share_history(self):
        files = prepare(grouped(attachment('采购合同.pdf', 'a', b'A')), self.cfg)
        done, _ = archive.write(files, self.cfg, ContentClient({'a': b'A'}), self.temp.name)
        numbering.finalize(self.cfg, files, done)
        self.cfg.raw['scope']['purchaser'] = '乙单位'
        self.assertEqual(numbering.include_history([], self.cfg), [])

    def test_project_switch_clears_in_memory_reviews(self):
        state = dict(project_no='P1', purchaser='甲单位', project_secret='否',
                     unmatched_policy='manual', file_reviews={'hash': {'type': '箱单'}})
        with patch.object(web, 'STATE', state):
            web._update_settings({'project_no': 'P2'})
            self.assertEqual(web.STATE['file_reviews'], {})

    def test_cli_restores_reviews_and_explicit_correction_wins(self):
        base = numbering.project_dir(self.cfg)
        base.mkdir(parents=True)
        (base / '人工确认.json').write_text(json.dumps({'hash': {'type': '箱单'},
                                                      'other': {'type': '采购合同'}}), encoding='utf-8')
        self.cfg.raw['file_reviews'] = {'hash': {'type': '协议'}}
        numbering.restore_reviews(self.cfg)
        self.assertEqual(self.cfg.raw['file_reviews']['hash']['type'], '协议')
        self.assertEqual(self.cfg.raw['file_reviews']['other']['type'], '采购合同')
