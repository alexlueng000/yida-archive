import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yida_archive import nightly
from yida_archive.models import AttachmentObs, Instance
from web import source_browser


class OriginalDownloadTests(unittest.TestCase):
    def test_default_date_is_fixed_before_waiting_for_lock(self):
        from contextlib import contextmanager
        seen = []
        @contextmanager
        def lock():
            seen.append('lock')
            yield
        with patch.object(nightly, 'execution_lock', lock), \
             patch.object(nightly, '_main', side_effect=lambda args: seen.append(args) or 0):
            self.assertEqual(nightly.main(['--write']), 0)
        self.assertEqual(seen[0], 'lock')
        expected = (datetime.datetime.now(nightly.api.CST).date() - datetime.timedelta(days=1)).isoformat()
        self.assertEqual(seen[1], ['--write', '--date', expected])

    def test_scan_uses_confirmed_approval_date(self):
        day = datetime.date(2026, 10, 28)
        start, _ = nightly.api.day_window(day)
        current = int(start.timestamp() * 1000) + 1000
        class Client:
            def iter_window(self, *args, **kwargs):
                return iter([{'instanceStatus': 'COMPLETED', 'approvedResult': 'agree', 'id': n} for n in range(3)])
            def operation_records(self, instance_id):
                return instance_id
        def build(form, record):
            return Instance('流程', 'form', str(record['id']), '', 0)
        def final(ident):
            return {'0': current, '1': current - 86400000, '2': 0}[ident]
        stats = {}
        with patch.object(nightly, 'FORMS', [{'name': '流程', 'form_uuid': 'form'}]), \
             patch.object(nightly, 'build_instance', build), \
             patch.object(nightly.api, 'final_agree_time', final):
            rows = nightly.scan_day(day, client=Client(), stats=stats)
        self.assertEqual([r.instance_id for r in rows], ['0'])
        self.assertEqual(stats['outside_approval_window'], 1)
        self.assertEqual(stats['approval_time_unverified'], 1)

    def test_stable_sources_preserve_each_instance_and_are_browsable(self):
        class Downloader:
            def download(self, url, path):
                Path(path).write_bytes(url.encode())

        def instance(ident, name):
            obs = AttachmentObs('流程', 'form', ident, name, 1, 'field', '附件',
                                '合同.pdf', 1, ident, ident)
            return Instance('流程', 'form', ident, name, 1, attachments=[obs])

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(nightly.api, 'PROJECT_ROOT', tmp), patch.object(source_browser, 'SOURCE_ROOT', Path(tmp)):
                day = datetime.date(2026, 10, 28)
                rows = [instance('a', '一'), instance('b', '二')]
                self.assertEqual(nightly.save_originals(rows, 'SN2026-C351-002', day, Downloader())[0], 2)
                nightly.save_originals(list(reversed(rows)), 'SN2026-C351-002', day, Downloader())
                sources = source_browser.roots()
                self.assertEqual(len(sources), 1)
                self.assertEqual(sources[0]['project'], 'SN2026-C351-002')
                files = list(Path(sources[0]['path']).rglob('*.pdf'))
                self.assertEqual(len(files), 2)
                self.assertEqual({p.read_bytes() for p in files}, {b'a', b'b'})


if __name__ == '__main__':
    unittest.main()
