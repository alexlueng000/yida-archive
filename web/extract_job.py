"""手动按审批日期提取：独立后台进程和可恢复的状态记录。"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from web import server as web
from yida_archive.fetch_openapi import CST

JOBS_DIR = Path(web.REPO) / '.cache' / 'date-extractions'
_guard = threading.Lock()


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf8')
    os.replace(tmp, path)


def _alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        try:
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259)
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def latest():
    files = sorted(JOBS_DIR.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files:
        try:
            job = json.loads(path.read_text(encoding='utf8'))
        except (OSError, ValueError):
            continue
        if job.get('status') in ('queued', 'running'):
            pid = job.get('pid')
            if (pid and not _alive(pid)) or (not pid and time.time() - job['created'] > 30):
                job.update(status='failed', message='后台任务意外停止，请查看日志后重试。')
                _write(path, job)
        log = JOBS_DIR / (job['id'] + '.log')
        try:
            with log.open('rb') as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 8192))
                lines = f.read().decode('utf8', errors='replace').splitlines()
            job['recent'] = lines[-8:]
        except OSError:
            job['recent'] = []
        return job
    return None


def start(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('请选择有效的审批日期')
    try:
        day = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError('请选择有效的审批日期') from exc
    if day > dt.datetime.now(CST).date():
        raise ValueError('审批日期不能晚于今天')
    with _guard:
        current = latest()
        if current and current['status'] in ('queued', 'running'):
            raise ValueError('已有手动拉取任务正在运行')
        ident = uuid.uuid4().hex
        path = JOBS_DIR / (ident + '.json')
        job = {'id': ident, 'date': value, 'status': 'queued',
               'created': time.time(), 'message': '任务已提交，正在启动'}
        _write(path, job)
        try:
            subprocess.Popen([sys.executable, '-u', '-m', 'web.extract_job', '--worker', ident, value],
                             cwd=web.REPO, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        except OSError as exc:
            job.update(status='failed', message='无法启动后台任务：' + str(exc))
            _write(path, job)
            raise ValueError(job['message']) from exc
        return job


def worker(ident, day):
    if not re.fullmatch(r'[0-9a-f]{32}', ident):
        return 2
    path = JOBS_DIR / (ident + '.json')
    job = json.loads(path.read_text(encoding='utf8'))
    job.update(status='running', pid=os.getpid(), message='正在拉取并归档，请稍候')
    _write(path, job)
    try:
        env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
        with (JOBS_DIR / (ident + '.log')).open('ab') as log:
            completed = subprocess.run([sys.executable, '-u', '-m', 'yida_archive.nightly',
                                        '--date', day, '--write'], cwd=web.REPO,
                                       stdout=log, stderr=subprocess.STDOUT, env=env,
                                       stdin=subprocess.DEVNULL, check=False)
        job.update(status='succeeded' if completed.returncode == 0 else 'failed',
                   message='拉取归档完成' if completed.returncode == 0 else '拉取归档未完成，请查看运行记录并重试',
                   exit_code=completed.returncode, finished=time.time())
    except Exception:
        job.update(status='failed', message='启动拉取程序失败，请查看服务器日志', finished=time.time())
    _write(path, job)
    return 0 if job['status'] == 'succeeded' else 1


if __name__ == '__main__':
    if len(sys.argv) != 4 or sys.argv[1] != '--worker':
        raise SystemExit(2)
    raise SystemExit(worker(sys.argv[2], sys.argv[3]))
