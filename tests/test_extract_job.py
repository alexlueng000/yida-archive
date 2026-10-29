import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import unittest
from pathlib import Path
from unittest.mock import patch

from web import extract_job
from web import server as web
from yida_archive import nightly


class ExtractionJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(extract_job, 'JOBS_DIR', Path(self.tmp.name) / 'jobs')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_date_validation_and_duplicate_running_job(self):
        for value in ('2026-02-30', '20261028', '', '9999-12-31', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                extract_job.start(value)
        with patch.object(extract_job.subprocess, 'Popen') as popen:
            job = extract_job.start('2026-10-28')
            self.assertEqual(job['status'], 'queued')
            self.assertEqual(popen.call_args.args[0][-1], '2026-10-28')
            with self.assertRaisesRegex(ValueError, '正在运行'):
                extract_job.start('2026-10-27')

    def test_worker_records_failure_and_log(self):
        ident = 'a' * 32
        path = extract_job.JOBS_DIR / (ident + '.json')
        extract_job._write(path, {'id': ident, 'date': '2026-10-28', 'created': time.time(), 'status': 'queued'})
        with patch.object(extract_job.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)):
            self.assertEqual(extract_job.worker(ident, '2026-10-28'), 1)
        self.assertEqual(extract_job.latest()['status'], 'failed')
        self.assertEqual(extract_job.latest()['exit_code'], 1)

    def test_worker_uses_original_date_and_reports_success(self):
        ident = 'b' * 32
        path = extract_job.JOBS_DIR / (ident + '.json')
        extract_job._write(path, {'id': ident, 'date': '2026-10-28', 'created': time.time(), 'status': 'queued'})
        with patch.object(extract_job.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(extract_job.worker(ident, '2026-10-28'), 0)
        self.assertEqual(run.call_args.args[0][-3:], ['--date', '2026-10-28', '--write'])
        self.assertEqual(extract_job.latest()['status'], 'succeeded')

    def test_missing_worker_is_reported_as_failed(self):
        ident = 'c' * 32
        path = extract_job.JOBS_DIR / (ident + '.json')
        extract_job._write(path, {'id': ident, 'date': '2026-10-28', 'created': time.time(),
                                  'status': 'running', 'pid': 12345})
        with patch.object(extract_job, '_alive', return_value=False):
            self.assertEqual(extract_job.latest()['status'], 'failed')
        self.assertEqual(json.loads(path.read_text(encoding='utf8'))['status'], 'failed')

    def test_http_start_and_status(self):
        import threading
        server = web.Server(('127.0.0.1', 0), web.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = 'http://127.0.0.1:%d/api/extraction' % server.server_address[1]
        request = urllib.request.Request(url, data=json.dumps({'date': '2026-10-28'}).encode(),
                                         headers={'Content-Type': 'application/json'})
        with patch.object(extract_job.subprocess, 'Popen'):
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.status, 202)
                self.assertEqual(json.load(response)['job']['date'], '2026-10-28')
            with urllib.request.urlopen(url) as response:
                self.assertEqual(json.load(response)['job']['status'], 'queued')
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 400)

    def test_lock_waits_until_other_process_releases(self):
        repo = str(Path(__file__).resolve().parents[1])
        code = ('import sys; from yida_archive import nightly; '
                'nightly.api.PROJECT_ROOT=sys.argv[1]; '
                'exec("with nightly.execution_lock():\\n print(\\"acquired\\",flush=True)")')
        with patch.object(nightly.api, 'PROJECT_ROOT', self.tmp.name):
            with nightly.execution_lock():
                child = subprocess.Popen([sys.executable, '-u', '-c', code, self.tmp.name], cwd=repo,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                try:
                    time.sleep(1)
                    self.assertIsNone(child.poll())
                except Exception:
                    child.kill()
                    child.wait()
                    raise
            output, _ = child.communicate(timeout=12)
        self.assertEqual(child.returncode, 0, output)
        self.assertIn('acquired', output)


if __name__ == '__main__':
    unittest.main()
