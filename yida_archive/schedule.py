"""北京时间每日任务的可配置时间和 systemd 触发状态。"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from . import fetch_openapi as api

ROOT = Path(api.PROJECT_ROOT)
SETTINGS = ROOT / '.cache' / 'nightly-schedule.json'
STATE = ROOT / '.cache' / 'nightly-schedule-state.json'
DEFAULT_TIME = '02:00'


def _read(path):
    try:
        data = json.loads(path.read_text(encoding='utf8'))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def validate_time(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
        raise ValueError('请选择有效的时间，格式为 HH:MM')
    return value


def get_settings():
    data = _read(SETTINGS)
    return {'time': validate_time(data.get('time', DEFAULT_TIME)), 'timezone': 'Asia/Shanghai'}


def set_time(value):
    value = validate_time(value)
    _write(SETTINGS, {'time': value})
    return get_settings()


def get_status():
    timer = 'unknown'
    if sys.platform.startswith('linux'):
        try:
            result = subprocess.run(['systemctl', 'is-active', 'yida-archive-nightly.timer'],
                                    capture_output=True, text=True, timeout=3, check=False)
            if result.returncode == 0 and result.stdout.strip() == 'active':
                timer = 'active'
            elif result.stdout.strip() in ('inactive', 'failed', 'activating', 'deactivating'):
                timer = 'inactive'
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {'settings': get_settings(), 'last_run': _read(STATE) or None, 'timer': timer}


def tick(now=None, runner=None):
    """systemd 每分钟触发；每天最多发起一次，异常中断后重试当天。"""
    now = now or dt.datetime.now(api.CST)
    now = now.astimezone(api.CST)
    settings = get_settings()
    target = dt.time.fromisoformat(settings['time'])
    state = _read(STATE)
    if state.get('status') == 'running' and state.get('approval_date') and state.get('trigger_date'):
        trigger_day, day = state['trigger_date'], state['approval_date']
        scheduled_time = state.get('scheduled_time', settings['time'])
    else:
        if now.time().replace(tzinfo=None) < target:
            return 0
        trigger_day = now.date().isoformat()
        if state.get('trigger_date') == trigger_day and state.get('status') in ('succeeded', 'failed'):
            return 0
        day = (now.date() - dt.timedelta(days=1)).isoformat()
        scheduled_time = settings['time']
    # 写入 running 后若主机断电，下次启动会重新执行当天任务；归档本身可重跑。
    state = {'trigger_date': trigger_day, 'approval_date': day,
             'scheduled_time': scheduled_time, 'status': 'running',
             'started_at': now.isoformat()}
    _write(STATE, state)
    command = [sys.executable, '-u', '-m', 'yida_archive.nightly', '--date', day, '--write']
    print('定时任务：审批日期 %s，配置时间 %s（北京时间）' % (day, scheduled_time), flush=True)
    try:
        result = (runner or subprocess.run)(command, cwd=str(ROOT), check=False)
        code = result.returncode
    except Exception as exc:
        code = 1
        print('定时任务无法启动：%s' % exc, flush=True)
    state.update(status='succeeded' if code == 0 else 'failed', exit_code=code,
                 finished_at=dt.datetime.now(api.CST).isoformat())
    _write(STATE, state)
    return code


if __name__ == '__main__':
    raise SystemExit(tick())
