"""验证内容证据、版本确认、缓存复用及实际输出，而非只比对文件名。"""
import contextlib
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest

from yida_archive import archive, classify, dedupe, fetch_local, naming, pipeline
from yida_archive.config import Config
from yida_archive.models import AttachmentObs, Instance


def attachment(name, uuid, content=None, ts=1000, size=4):
    return AttachmentObs('合同审批', 'F', 'I' + str(ts), 'T', ts, 'a', '附件',
                         name, len(content) if content is not None else size, uuid, uuid,
                         hashlib.sha256(content).hexdigest() if content is not None else '')


def grouped(*obs):
    return dedupe.group([Instance('合同审批', 'F', 'I', 'T', 1000, attachments=list(obs))])[0]


def prepare(files, cfg):
    classify.classify(files, cfg)
    classify.mark_secret(files, cfg)
    naming.assign(files, cfg, cfg.project_no)
    return files


class ContentClient:
    def __init__(self, data):
        self.data, self.calls = data, []

    def download(self, url, path):
        self.calls.append(url)
        Path(path).write_bytes(self.data[url])


class TestArchiveV2(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config({'scope': {'project_no': 'P1', 'project_secret': False}, 'storage': {
            'out_dir': os.path.join(self.tmp.name, 'out'),
            'work_dir': os.path.join(self.tmp.name, 'cache')}})

    def test_equal_name_size_distinct_hashes_keep_both(self):
        files = grouped(attachment('采购合同.pdf', 'a', b'AAAA'), attachment('采购合同.pdf', 'b', b'BBBB'))
        self.assertEqual(len(files), 2)
        prepare(files, self.cfg)
        self.assertEqual(len({f.new_name for f in files}), 2)
        self.assertTrue(dedupe.findings_for(files).version_conflicts)

    def test_hash_conflict_overrides_uuid(self):
        files = grouped(attachment('采购合同.pdf', 'a', b'AAAA'), attachment('采购合同.pdf', 'a', b'BBBB'),
                        attachment('采购合同.pdf', 'a'))
        self.assertEqual(len(files), 3)

    def test_missing_uuid_and_size_never_merge(self):
        files = grouped(attachment('采购合同.pdf', '', size=None), attachment('采购合同.pdf', '', size=None))
        prepare(files, self.cfg)
        self.assertEqual(len({f.new_name for f in files}), 2)

    def test_renamed_copy_retains_alias_and_classification(self):
        files = grouped(attachment('scan.pdf', 'a', b'ABCD'), attachment('采购合同.pdf', 'b', b'ABCD'))
        prepare(files, self.cfg)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].type_name, '采购合同')
        self.assertEqual(files[0].upload_count, 2)

    def test_unverified_name_size_not_merged(self):
        self.assertEqual(len(grouped(attachment('a.pdf', 'a'), attachment('a.pdf', 'b'))), 2)

    def test_sequence_is_chronological_not_rule_order(self):
        files = grouped(attachment('付款凭证.pdf', 'a', b'A', 1), attachment('采购合同.pdf', 'b', b'B', 2))
        prepare(files, self.cfg)
        self.assertEqual([f.new_name for f in files], ['23-付款凭证（财务部）-P1.pdf', '3-采购合同-P1.pdf'])

    def test_confirmed_latest_only_and_version_before_project(self):
        files = grouped(attachment('采购合同.pdf', 'a', b'A', 1), attachment('采购合同.pdf', 'b', b'B', 2))
        self.cfg.raw['file_reviews'] = {f.ident: {'version_group': '采购A', 'version_order': i + 1}
                                        for i, f in enumerate(files)}
        prepare(files, self.cfg)
        self.assertEqual([f.new_name for f in files], ['', '3-采购合同v2-P1.pdf'])
        self.assertTrue(files[0].superseded)
        self.assertEqual(len(archive.plan(files, self.cfg)), 1)

    def test_unverified_versions_cannot_discard_old(self):
        files = grouped(attachment('采购合同.pdf', 'a'), attachment('采购合同.pdf', 'b'))
        self.cfg.raw['file_reviews'] = {f.ident: {'version_group': 'A', 'version_order': i + 1}
                                        for i, f in enumerate(files)}
        with self.assertRaises(ValueError):
            prepare(files, self.cfg)

    def test_cache_prevents_second_download_and_output_matches(self):
        files = grouped(attachment('采购合同.pdf', 'a'), attachment('采购合同改名.pdf', 'b'))
        client = ContentClient({'a': b'ABCD', 'b': b'ABCD'})
        archive.hash_all(files, client, self.cfg.work_dir)
        files = prepare(dedupe.refine(files), self.cfg)
        done, failed = archive.write(files, self.cfg, client, self.cfg.work_dir)
        self.assertEqual(client.calls, ['a', 'b'])
        self.assertEqual((len(done), failed), (1, []))
        self.assertEqual(Path(done[0][1]).read_bytes(), b'ABCD')
        self.assertIn(str(Path('未指定采购单位') / 'P1'), done[0][1])

    def test_existing_different_file_not_overwritten_and_repeat_idempotent(self):
        files = prepare(grouped(attachment('采购合同.pdf', 'a', b'ABCD')), self.cfg)
        dest = Path(archive.plan(files, self.cfg)[0][1])
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b'original')
        client = ContentClient({'a': b'ABCD'})
        done, _ = archive.write(files, self.cfg, client, self.cfg.work_dir)
        again, _ = archive.write(files, self.cfg, client, self.cfg.work_dir)
        self.assertEqual(dest.read_bytes(), b'original')
        self.assertEqual(done[0][1], again[0][1])
        self.assertEqual(Path(done[0][1]).read_bytes(), b'ABCD')

    def test_download_failure_stays_visible(self):
        files = grouped(attachment('采购合同.pdf', 'missing'))
        client = ContentClient({})
        archive.hash_all(files, client, self.cfg.work_dir)
        prepare(files, self.cfg)
        done, failed = archive.write(files, self.cfg, client, self.cfg.work_dir)
        self.assertEqual(len(failed), 1)
        self.assertFalse(done)
        self.assertEqual(client.calls, ['missing'])

    def test_dry_run_has_no_download(self):
        inst = Instance('合同审批', 'F', 'I', 'T', 1000, attachments=[attachment('采购合同.pdf', 'a')])
        client = ContentClient({})
        with contextlib.redirect_stdout(io.StringIO()):
            result = pipeline.run(self.cfg, [inst], client=client, dry_run=True)
        self.assertEqual(client.calls, [])
        self.assertEqual(result.summary['unverified'], 1)

    def test_output_traversal_rejected(self):
        self.cfg.raw['storage']['structure'] = '../escape/{folder}'
        files = prepare(grouped(attachment('采购合同.pdf', 'a')), self.cfg)
        with self.assertRaises(ValueError):
            archive.plan(files, self.cfg)

    def test_local_import_preserves_timestamp_field_and_original(self):
        root = Path(self.tmp.name) / '每次上传'
        path = root / '01-20260917-1627-货款支付-付款单' / '合同附件' / '采购合同.pdf'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'ABCD')
        (path.parent / '~$采购合同.xlsx').write_bytes(b'lock')
        instances = fetch_local.load(root)
        self.assertEqual(len(instances[0].attachments), 1)
        self.assertEqual(instances[0].attachments[0].field_label, '合同附件')
        self.assertGreater(instances[0].submitted_at, 0)
        self.assertEqual(path.read_bytes(), b'ABCD')

    def test_default_excludes_feasibility_and_po_sample(self):
        files = prepare(grouped(attachment('可行性测算表.xlsx', 'a'), attachment('sample of PO-779-2024.docx', 'b')), self.cfg)
        self.assertEqual(len(archive.plan(files, self.cfg)), 2)
        self.assertTrue(all(Path(path).parent.name == '待归类' for _, path in archive.plan(files, self.cfg)))

    def test_five_folders_under_each_root(self):
        archive.create_folders(self.cfg)
        project = Path(self.cfg.out_dir) / '未指定采购单位' / 'P1'
        for root in ('公共', '加密'):
            self.assertEqual({p.name for p in (project / root).iterdir()}, set(self.cfg.folders + ['待归类']))
