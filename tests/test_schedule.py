import datetime as dt
import json
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from yida_archive import schedule
from web import server as web


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, path in [('SETTINGS', Path(self.tmp.name) / 'settings.json'),
                           ('STATE', Path(self.tmp.name) / 'state.json')]:
            patcher = patch.object(schedule, name, path)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.runner = Mock(return_value=subprocess.CompletedProcess([], 0))

    def at(self, day, hour, minute):
        return dt.datetime(2026, 10, day, hour, minute, tzinfo=schedule.api.CST)

    def test_defaults_and_validation(self):
        self.assertEqual(schedule.get_settings(), {'time': '02:00', 'timezone': 'Asia/Shanghai'})
        for value in ('2:00', '24:00', '12:60', '', None):
            with self.assertRaises(ValueError):
                schedule.set_time(value)
        self.assertEqual(schedule.set_time('23:45')['time'], '23:45')
        self.assertEqual(schedule.get_settings()['time'], '23:45')

    def test_runs_once_after_configured_time_with_previous_day(self):
        schedule.set_time('03:45')
        self.assertEqual(schedule.tick(self.at(29, 3, 44), self.runner), 0)
        self.runner.assert_not_called()
        self.assertEqual(schedule.tick(self.at(29, 3, 45), self.runner), 0)
        self.assertEqual(self.runner.call_args.args[0][-3:], ['--date', '2026-10-28', '--write'])
        self.assertEqual(schedule._read(schedule.STATE)['status'], 'succeeded')
        schedule.set_time('04:30')
        schedule.tick(self.at(29, 5, 0), self.runner)
        self.assertEqual(self.runner.call_count, 1)
        schedule.tick(self.at(30, 4, 30), self.runner)
        self.assertEqual(self.runner.call_args.args[0][-3:], ['--date', '2026-10-29', '--write'])

    def test_failure_reported_and_not_retried_every_minute(self):
        self.runner.return_value = subprocess.CompletedProcess([], 1)
        self.assertEqual(schedule.tick(self.at(29, 2, 0), self.runner), 1)
        self.assertEqual(schedule._read(schedule.STATE)['status'], 'failed')
        self.assertEqual(schedule.tick(self.at(29, 2, 1), self.runner), 0)
        self.assertEqual(self.runner.call_count, 1)

    def test_interrupted_run_retries_original_date_after_restart(self):
        schedule._write(schedule.STATE, {'trigger_date': '2026-10-28', 'approval_date': '2026-10-27',
                                         'scheduled_time': '02:00', 'status': 'running'})
        schedule.tick(self.at(29, 0, 10), self.runner)
        self.assertEqual(self.runner.call_args.args[0][-3:], ['--date', '2026-10-27', '--write'])
        self.assertEqual(schedule._read(schedule.STATE)['trigger_date'], '2026-10-28')

    def test_http_updates_persistent_time(self):
        server = web.Server(('127.0.0.1', 0), web.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = 'http://127.0.0.1:%d/api/schedule' % server.server_address[1]
        def request(value):
            return urllib.request.Request(url, data=json.dumps({'time': value}).encode(),
                                          headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request('04:15')) as response:
            self.assertEqual(json.load(response)['settings']['time'], '04:15')
        with urllib.request.urlopen(url) as response:
            self.assertEqual(json.load(response)['settings']['time'], '04:15')
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(request('25:00'))


if __name__ == '__main__':
    unittest.main()
