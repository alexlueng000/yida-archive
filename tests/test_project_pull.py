import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from yida_archive import project_pull
from yida_archive.models import Instance
from web import extract_job


class ProjectPullTests(unittest.TestCase):
    def test_validation(self):
        for value in ('', '../test', 'a/b', None, 'a; rm'):
            with self.assertRaises(ValueError):
                project_pull.project_key(value)
        self.assertEqual(project_pull.project_key(' 23JZBTPFB50-10 '), '23JZBTPFB50-10')

    def test_all_forms_exact_match_and_dedup(self):
        forms = [{'name': '合同', 'form_uuid': 'A', 'project_field': 'project'},
                 {'name': '付款', 'form_uuid': 'B', 'contract_field': 'contract'}]
        client = Mock()
        client.iter_window.return_value = [1, 1, 2, 3]
        def build(form, record):
            return Instance(form['name'], form['form_uuid'], str(record), '', 0,
                            project_key='P-10' if record == 1 else 'P-100')
        with patch.object(project_pull.nightly, 'is_finished', side_effect=lambda r: r != 3), \
             patch.object(project_pull.nightly, 'build_instance', side_effect=build):
            rows = project_pull.scan(client, 'P-10', forms)
        self.assertEqual([(r.form_uuid, r.instance_id) for r in rows], [('A', '1'), ('B', '1')])
        self.assertEqual(client.iter_window.call_count, 2)
        for call in client.iter_window.call_args_list:
            self.assertEqual(call.args[1].year, 1970)
            self.assertEqual(call.kwargs['approvedResult'], 'agree')

    def test_project_job_dispatch_and_shared_busy_guard(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(extract_job, 'JOBS_DIR', Path(tmp)), \
             patch.object(extract_job.subprocess, 'Popen'), \
             patch.object(extract_job.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            job = extract_job.start_project('23JZBTPFB50-10')
            self.assertEqual(job['kind'], 'project')
            with self.assertRaises(ValueError):
                extract_job.start('2026-01-01')
            self.assertEqual(extract_job.worker(job['id'], job['date']), 0)
            command = run.call_args.args[0]
            self.assertIn('yida_archive.project_pull', command)
            self.assertEqual(command[-3:], ['--project', '23JZBTPFB50-10', '--write'])
            self.assertEqual(extract_job.latest()['status'], 'succeeded')
