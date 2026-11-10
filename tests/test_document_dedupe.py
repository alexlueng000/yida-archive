import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from test_archive_v2 import attachment, grouped, prepare, ContentClient
from yida_archive import archive, numbering
from yida_archive.config import Config
from yida_archive.document_dedupe import fingerprint, merge


def document(metadata='one', text='contract', image=b'seal', extra=None):
    parts = {
        '[Content_Types].xml': '<Types/>',
        'word/document.xml': '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p w:rsidR="' + metadata + '"><w:r><w:t>' + text + '</w:t></w:r></w:p></w:body></w:document>',
        'docProps/core.xml': '<properties>' + metadata + '</properties>',
        'word/media/image1.png': image,
    }
    parts.update(extra or {})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, body in parts.items():
            z.writestr(name, body)
    return buf.getvalue()


class DocumentDedupeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config({'scope': {'project_no': 'P1', 'project_secret': False},
                           'storage': {'out_dir': self.tmp.name}})

    def files(self, *bodies):
        files = grouped(*(attachment('采购合同' + str(i) + '.docx', str(i), body)
                          for i, body in enumerate(bodies)))
        for i, f in enumerate(files):
            p = Path(self.tmp.name) / ('source' + str(i))
            p.write_bytes(bodies[i])
            f.cache_path = str(p)
        return files

    def test_metadata_only_merge_preserves_sources_and_physical_hash(self):
        files = self.files(document(), document('two'))
        old_hash = files[0].content_hash
        other_hash = files[1].content_hash
        result = merge(files)
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0].obs), 2)
        self.assertEqual(result[0].content_hash, old_hash)
        self.assertEqual(result[0].equivalent_hashes, [other_hash])
        self.assertEqual({o.content_hash for o in result[0].obs}, {old_hash, other_hash})

    def test_business_differences_never_merge(self):
        for variant in (document(text='changed'), document(image=b'other seal'),
                        document(extra={'word/comments.xml': '<comments>important</comments>'}),
                        document(extra={'word/header1.xml': '<header>changed</header>'}),
                        document(extra={'word/styles.xml': '<styles>changed</styles>'}),
                        document(extra={'word/embeddings/data.bin': b'changed'})):
            with self.subTest(variant=hashlib.sha256(variant).hexdigest()):
                self.assertEqual(len(merge(self.files(document(), variant))), 2)

    def test_malformed_signed_and_modified_files_are_not_merged(self):
        self.assertIsNone(fingerprint(io.BytesIO(b'not zip')))
        self.assertIsNone(fingerprint(io.BytesIO(document(extra={'_xmlsignatures/sig.xml': '<sig/>'}))))
        files = self.files(document(), document('two'))
        Path(files[1].cache_path).write_bytes(document('three'))
        self.assertEqual(len(merge(files)), 2)

    def test_explicit_reviews_stay_separate(self):
        files = self.files(document(), document('two'))
        self.assertEqual(len(merge(files, {files[1].ident: {'type': '采购合同'}})), 2)

    def test_memory_uploads_supported(self):
        files = self.files(document(), document('two'))
        bodies = {f.ident: Path(f.cache_path).read_bytes() for f in files}
        for f in files:
            f.cache_path = ''
        self.assertEqual(len(merge(files, loader=lambda f: bodies[f.ident])), 1)

    def test_history_cleanup_and_repeat_run(self):
        files = prepare(self.files(document(), document('two')), self.cfg)
        done, failed = archive.write(files, self.cfg, ContentClient({}), self.tmp.name)
        self.assertFalse(failed)
        numbering.finalize(self.cfg, files, done)
        old_paths = [Path(p) for _, p in done]
        history = numbering.include_history([], self.cfg)
        self.assertEqual(len(history), 1)
        prepare(history, self.cfg)
        done, failed = archive.write(history, self.cfg, ContentClient({}), self.tmp.name)
        self.assertFalse(failed)
        numbering.finalize(self.cfg, history, done, cleanup=True)
        self.assertEqual(sum(p.exists() for p in old_paths), 1)
        self.assertEqual(len(numbering.load(self.cfg)['units']), 1)
        again = numbering.include_history([], self.cfg)
        self.assertEqual(len(again), 1)
        self.assertEqual(len(again[0].obs), 2)


    def test_web_staged_save_removes_historical_duplicate(self):
        from web import server as web
        from yida_archive import dedupe
        files = prepare(self.files(document(), document('two')), self.cfg)
        done, failed = archive.write(files, self.cfg, ContentClient({}), self.tmp.name)
        self.assertFalse(failed)
        numbering.finalize(self.cfg, files, done)
        files = numbering.include_history([], self.cfg)
        prepare(files, self.cfg)
        self.cfg.raw['file_reviews'] = {}
        with tempfile.TemporaryDirectory() as stage, patch.object(web, 'OUTPUT_ROOT', self.tmp.name), patch.dict(web.STATE, blobs={}):
            result = web._save_to(stage, prepared=(self.cfg, files, dedupe.findings_for(files)))
            self.assertTrue(result['ok'], result)
            self.assertEqual(len(list(Path(stage).rglob('*.docx'))), 1)
            self.assertEqual(len(numbering.include_history([], self.cfg)), 1)


if __name__ == '__main__':
    unittest.main()
