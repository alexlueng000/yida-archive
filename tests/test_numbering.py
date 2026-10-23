"""需求文档命名规则：跨保存编号、上传顺序与自动修订。"""
import tempfile
import unittest
from pathlib import Path

from test_archive_v2 import attachment, grouped, prepare, ContentClient
from yida_archive import archive, naming, numbering, fetch_json
from yida_archive.config import Config
from yida_archive.models import Instance


class TestNumbering(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config({'scope': {'project_no': 'P1', 'project_secret': False}, 'storage': {'out_dir': self.tmp.name}})

    def test_equal_timestamps_preserve_attachment_order(self):
        files = prepare(grouped(attachment('采购合同.pdf', 'a', b'A'),
                                attachment('付款凭证.pdf', 'b', b'B'),
                                attachment('装箱单.pdf', 'c', b'C')), self.cfg)
        self.assertEqual([f.new_name for f in files],
                         ['3-采购合同-P1.pdf', '23-付款凭证（财务部）-P1.pdf', '15-箱单-P1.pdf'])

    def test_existing_numbers_survive_removal_and_earlier_new_upload(self):
        first = prepare(grouped(attachment('采购合同.pdf', 'a', b'A', 5),
                                attachment('付款凭证.pdf', 'b', b'B', 6)), self.cfg)
        numbering.write(self.cfg)
        files = prepare(grouped(attachment('装箱单.pdf', 'c', b'C', 1),
                                attachment('付款凭证.pdf', 'b', b'B', 6)), self.cfg)
        self.assertEqual([f.new_name for f in files], ['15-箱单-P1.pdf', '23-付款凭证（财务部）-P1.pdf'])

    def test_subnumbers_never_recycle(self):
        prepare(grouped(attachment('采购合同A.pdf', 'a', b'A'),
                        attachment('采购合同B.pdf', 'b', b'B')), self.cfg)
        numbering.write(self.cfg)
        files = prepare(grouped(attachment('采购合同B.pdf', 'b', b'B'),
                                attachment('采购合同C.pdf', 'c', b'C')), self.cfg)
        self.assertEqual([f.new_name for f in files], ['3-2-采购合同-P1.pdf', '3-3-采购合同-P1.pdf'])

    def test_automatic_versions_keep_original_number_and_extension(self):
        files = prepare(grouped(attachment('采购合同A.pdf', 'a', b'A', 1),
                                attachment('付款凭证.png', 'b', b'B', 2),
                                attachment('采购合同A_v2.docx', 'c', b'C', 3),
                                attachment('采购合同A_v3.PDF', 'd', b'D', 4)), self.cfg)
        self.assertEqual([f.new_name for f in files], ['', '23-付款凭证（财务部）-P1.png', '', '3-采购合同v3-P1.PDF'])
        self.assertEqual(sum(f.superseded for f in files), 2)

    def test_versions_across_runs_and_stale_upload_rejected(self):
        prepare(grouped(attachment('采购合同A.pdf', 'a', b'A')), self.cfg)
        numbering.write(self.cfg)
        current = prepare(grouped(attachment('采购合同A_v2.pdf', 'b', b'B')), self.cfg)
        self.assertEqual(current[0].new_name, '3-采购合同v2-P1.pdf')
        numbering.write(self.cfg)
        with self.assertRaisesRegex(ValueError, '旧版本'):
            prepare(grouped(attachment('采购合同A.pdf', 'a', b'A')), self.cfg)

    def test_identical_names_alone_do_not_delete_distinct_documents(self):
        files = prepare(grouped(attachment('采购合同.pdf', 'a', b'A'),
                                attachment('采购合同.pdf', 'b', b'B')), self.cfg)
        self.assertEqual(sum(bool(f.new_name) for f in files), 2)
        self.assertFalse(any(f.superseded for f in files))

    def test_conflicting_named_versions_are_not_silently_replaced(self):
        files = prepare(grouped(attachment('采购合同v2.pdf', 'a', b'A'),
                                attachment('采购合同v2.pdf', 'b', b'B')), self.cfg)
        self.assertEqual(sum(bool(f.new_name) for f in files), 2)

    def test_upstream_document_id_links_renamed_revisions(self):
        a = attachment('采购合同甲.pdf', 'a', b'A', 1)
        b = attachment('采购合同乙.pdf', 'b', b'B', 2)
        a.document_id = b.document_id = 'contract-123'
        a.revision, b.revision = 1, 2
        files = prepare(grouped(a, b), self.cfg)
        self.assertEqual([f.new_name for f in files], ['', '3-采购合同v2-P1.pdf'])
        snapshot = Path(self.tmp.name) / 'snapshot.json'
        fetch_json.dump([Instance('合同审批', 'F', 'I', '', 1, attachments=[a, b])], 'P1', '', snapshot)
        loaded = fetch_json.load(snapshot)[0].attachments
        self.assertEqual([(a.document_id, a.revision) for a in loaded], [('contract-123', 1), ('contract-123', 2)])

    def test_purchaser_naming_and_type_restriction(self):
        self.cfg.raw.update(scope={'project_no': 'P1', 'purchaser': '甲单位'}, purchasers={
            '甲单位': {'classification': {'allowed_types': ['采购合同']},
                       'naming': {'pattern': '{project}-{seq}-{type}{ext}',
                                  'type_labels': {'采购合同': '设备合同'}}}})
        files = prepare(grouped(attachment('采购合同.pdf', 'a', b'A'),
                                attachment('付款凭证.pdf', 'b', b'B')), self.cfg)
        self.assertEqual([f.new_name for f in files], ['P1-3-设备合同.pdf', '付款凭证.pdf'])

    def test_bid_contract_label_matches_requirement_example(self):
        f = prepare(grouped(attachment('中标合同.pdf', 'a', b'A')), self.cfg)[0]
        self.assertEqual(f.new_name, '1-中标合同（内贸）-P1.pdf')

    def test_filename_version_number_does_not_control_generated_version(self):
        first = prepare(grouped(attachment('采购合同A_v99.pdf', 'a', b'A', 1)), self.cfg)
        self.assertEqual(first[0].new_name, '3-采购合同-P1.pdf')
        numbering.write(self.cfg)
        second = prepare(grouped(attachment('采购合同A.pdf', 'b', b'B', 2)), self.cfg)
        self.assertEqual(second[0].new_name, '3-采购合同v2-P1.pdf')

    def test_document_id_needs_no_user_revision_number(self):
        a = attachment('采购合同A.pdf', 'a', b'A', 1)
        b = attachment('采购合同改名.pdf', 'b', b'B', 2)
        a.document_id = b.document_id = 'business-file-1'
        files = prepare(grouped(a, b), self.cfg)
        self.assertEqual([f.new_name for f in files], ['', '3-采购合同v2-P1.pdf'])

    def test_preview_does_not_commit_or_modify_existing_ledger(self):
        prepare(grouped(attachment('采购合同.pdf', 'a', b'A')), self.cfg)
        path = numbering.project_dir(self.cfg) / numbering.FILENAME
        self.assertFalse(path.exists())
        numbering.write(self.cfg)
        original = path.read_bytes()
        prepare(grouped(attachment('付款凭证.pdf', 'b', b'B')), self.cfg)
        self.assertEqual(path.read_bytes(), original)

    def test_invalid_ledger_does_not_silently_restart_numbering(self):
        base = numbering.project_dir(self.cfg)
        base.mkdir(parents=True)
        (base / numbering.FILENAME).write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError):
            prepare(grouped(attachment('采购合同.pdf', 'a', b'A')), self.cfg)

    def test_cli_removes_only_tracked_old_version(self):
        old = prepare(grouped(attachment('采购合同A.pdf', 'a', b'A')), self.cfg)
        client = ContentClient({'a': b'A', 'b': b'B'})
        done, failed = archive.write(old, self.cfg, client, self.tmp.name)
        numbering.finalize(self.cfg, old, done, cleanup=True)
        previous = Path(done[0][1])
        unrelated = previous.parent / '用户笔记.txt'
        unrelated.write_text('keep')
        new = prepare(grouped(attachment('采购合同A_v2.pdf', 'b', b'B')), self.cfg)
        done, failed = archive.write(new, self.cfg, client, self.tmp.name)
        numbering.finalize(self.cfg, new, done, cleanup=True)
        self.assertFalse(previous.exists())
        self.assertEqual(Path(done[0][1]).read_bytes(), b'B')
        self.assertTrue(unrelated.exists())

    def test_old_csv_requires_migration_before_revision(self):
        import csv
        base = numbering.project_dir(self.cfg)
        base.mkdir(parents=True)
        with (base / '清单.csv').open('w', encoding='utf-8-sig', newline='') as fh:
            writer = csv.DictWriter(fh, fieldnames=['新文件名', '内容哈希', '类型', '版本号', '归档目录', '原始文件名'])
            writer.writeheader()
            old = attachment('采购合同A.pdf', 'a', b'A')
            writer.writerow({'新文件名': '3-采购合同-P1.pdf', '内容哈希': old.content_hash,
                             '类型': '采购合同', '版本号': '', '归档目录': 'P1_文件分类/公共/合同',
                             '原始文件名': old.name})
        with self.assertRaisesRegex(ValueError, '迁移'):
            prepare(grouped(attachment('采购合同A_v2.pdf', 'b', b'B')), self.cfg)
