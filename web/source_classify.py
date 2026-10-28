"""Background classification of an original directory into its own project archive."""
import copy
import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from web import server as web, projects, source_browser
from yida_archive.config import load as load_config

JOBS_DIR = Path(web.REPO) / '.cache' / 'source-classification'
CONFIG_ROOT = Path(web.REPO)
_jobs_lock = threading.RLock()
_active = None


def job_path(ident):
    if not re.fullmatch(r'[0-9a-f]{32}', ident or ''):
        raise ValueError('归类任务编号无效')
    return Path(JOBS_DIR) / (ident + '.json')


def checkpoint(job, **updates):
    with _jobs_lock:
        job.update(updates, updated=int(time.time() * 1000))
        target = job_path(job['id'])
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix('.tmp')
        temp.write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
        os.replace(temp, target)


def get_job(ident):
    with _jobs_lock:
        path = job_path(ident)
        if not path.is_file():
            raise FileNotFoundError('归类任务不存在')
        job = json.loads(path.read_text(encoding='utf-8'))
        if job['status'] in ('queued', 'running') and _active != ident:
            checkpoint(job, status='failed', message='服务已重启，本次归类未完成。请重新点击归类，原文件仍保留。')
        return job


def latest(source_id, relative):
    with _jobs_lock:
        matches = []
        for path in Path(JOBS_DIR).glob('*.json'):
            job = json.loads(path.read_text(encoding='utf-8'))
            if job['source_id'] == source_id and job['relative'] == relative:
                matches.append(job)
        return get_job(max(matches, key=lambda j: j['created'])['id']) if matches else None


def directory_history(source_id):
    """Summarize persisted attempts independently from the latest successful run."""
    result = {}
    with _jobs_lock:
        for path in Path(JOBS_DIR).glob('*.json'):
            try:
                job = json.loads(path.read_text(encoding='utf-8'))
                if job.get('source_id') != source_id or not job.get('relative'):
                    continue
                entry = result.setdefault(job['relative'], {})
                if job.get('created', 0) >= entry.get('latest_at', -1):
                    entry.update(latest_at=job.get('created', 0), latest_status=job.get('status'))
                if job.get('status') == 'succeeded' and job.get('updated', 0) >= entry.get('completed_at', -1):
                    summary = (job.get('result') or {}).get('summary') or {}
                    entry.update(completed_at=job.get('updated', 0), rejected=len(summary.get('rejected') or []))
            except (OSError, ValueError, TypeError):
                continue
    return result


def identify_project(source):
    no = source['name'].removesuffix('-全流程')
    purchaser = '未指定采购单位'
    for name in ('config.' + no + '.全流程.yaml', 'config.' + no + '.yaml'):
        path = Path(CONFIG_ROOT) / name
        if path.is_file():
            cfg = load_config(str(path))
            no, purchaser = cfg.project_no or no, cfg.purchaser or purchaser
            break
    if not no or web.naming.sanitize(no) != no or web.naming.sanitize(purchaser) != purchaser:
        raise ValueError('原目录未能匹配有效的项目号和采购单位')
    items = projects.catalogue()
    matches = [i for i in items.values() if i['project_no'] == no]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        exact = [i for i in matches if i['purchaser'] == purchaser]
        if len(exact) != 1:
            raise ValueError('该项目号对应多个采购单位，请先在项目配置中明确采购单位')
        return exact[0]
    ident = projects.key_for(purchaser, no)
    item = dict(id=ident, purchaser=purchaser, project_no=no, name=no, created=int(time.time()*1000))
    items[ident] = item
    projects.save_catalogue(items)
    return item


def scan(source, relative):
    base = Path(source['path']).resolve()
    folder = source_browser.safe_path(base, relative)
    if not folder.is_dir():
        raise ValueError('待归类目录不存在')
    files = []
    for path in sorted(folder.rglob('*')):
        source_browser.safe_path(base, path.relative_to(base).as_posix())
        if path.is_file():
            files.append(path)
    if not files:
        raise ValueError('当前目录及子目录中没有可归类的文件')
    return files


def stage_time(name, fallback):
    match = re.match(r'^\d+-(\d{8}-\d{4})-', name)
    if match:
        try:
            return int(datetime.strptime(match[1], '%Y%m%d-%H%M').timestamp()*1000)
        except ValueError:
            pass
    return int(fallback*1000)


def ingest(source, paths, job):
    base = Path(source['path']).resolve()
    cache = web.SESSION_DIR / 'source-files'
    cache.mkdir(parents=True, exist_ok=True)
    imported, unchanged, rejected, hashes = 0, 0, [], set()
    for index, path in enumerate(paths, 1):
        rel = path.relative_to(base).as_posix()
        source_browser.safe_path(base, rel)
        stat = path.stat()
        if not stat.st_size:
            rejected.append(dict(name=rel, error='空文件未归档'))
            continue
        temp = cache / (uuid.uuid4().hex + '.part')
        try:
            shutil.copyfile(path, temp)
            digest = web.archive._sha256(str(temp))
            after = path.stat()
            if (after.st_size, after.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                raise ValueError('原文件在读取期间发生变化，请重新归类：' + rel)
            target = cache / digest
            if not target.exists() or web.archive._sha256(str(target)) != digest:
                os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        hashes.add(digest)
        source_key = source['id'] + ':' + rel
        previous = [(b, f) for b in web.STATE['batches'] for f in b['files']
                    if f.get('source_key') == source_key]
        if any(f['sha256'] == digest for _, f in previous):
            unchanged += 1
        else:
            for batch, entry in previous:
                batch['files'].remove(entry)
            record = path.relative_to(base).parts[0] if path.parent != base else '原目录附件'
            batch_key = source['id'] + ':' + record
            batch = next((b for b in web.STATE['batches'] if b.get('source_batch') == batch_key), None)
            if batch is None:
                record_path = base / record if path.parent != base else base
                batch = dict(id=uuid.uuid4().hex[:16], source_batch=batch_key, name=record,
                             field='附件', created=stage_time(record, record_path.stat().st_mtime), files=[])
                web.STATE['batches'].append(batch)
            batch['files'].append(dict(id=uuid.uuid4().hex[:16], name=path.name, size=stat.st_size,
                field='附件', uuid='source-'+hashlib.sha256(source_key.encode()).hexdigest(),
                sha256=digest, cached_digest=digest, source_key=source_key))
            imported += 1
        checkpoint(job, message='读取附件 %d/%d：%s' % (index, len(paths), path.name),
                   processed=index, total=len(paths), progress=int(index/len(paths)*30))
    web.STATE['last_result'] = None
    web.save_session()
    if not hashes:
        raise ValueError('目录中只有空文件，没有可归档的内容')
    return dict(total=len(paths), imported=imported, unchanged=unchanged, unique=len(hashes),
                duplicates=len(paths)-len(rejected)-len(hashes), rejected=rejected)


def worker(source, relative, job):
    global _active
    try:
        checkpoint(job, status='running', message='正在读取当前目录及子目录…', progress=1)
        paths = scan(source, relative)
        with web._lock:
            project = identify_project(source)
            checkpoint(job, project=project, message='正在准备项目 ' + project['project_no'])
            with projects.session(project):
                summary = ingest(source, paths, job)
                checkpoint(job, message='正在分析文件内容并归档…', progress=35)
                def progress(message):
                    match = re.search(r'(\d+)/(\d+)', message)
                    percent = 35 + int(int(match[1])/max(int(match[2]), 1)*55) if match else 40
                    checkpoint(job, message=message, progress=percent)
                saved = web.save_result(progress=progress)
                web.save_session()
                if not saved.get('ok'):
                    raise ValueError('归档保存失败，原文件和读取缓存已保留，可重新归类。' +
                                     str(saved.get('failed') or saved.get('error') or ''))
                result = projects.detail(project)
                result['summary'] = summary
                checkpoint(job, status='succeeded', progress=100, message='归类完成，已自动归档', result=result)
    except Exception as exc:
        checkpoint(job, status='failed', message=str(exc), progress=0)
    finally:
        with _jobs_lock:
            if _active == job['id']:
                _active = None


def start(source, relative):
    global _active
    source_browser.safe_path(Path(source['path']).resolve(), relative)
    with _jobs_lock:
        if _active:
            active = get_job(_active)
            if active['source_id'] == source['id'] and active['relative'] == relative:
                return active
            raise ValueError('另一个目录正在归类，请完成后再操作')
        job = dict(id=uuid.uuid4().hex, source_id=source['id'], relative=relative,
                   created=int(time.time()*1000), status='queued', progress=0, message='归类任务已开始')
        checkpoint(job)
        _active = job['id']
        threading.Thread(target=worker, args=(source, relative, job), daemon=True).start()
        return copy.deepcopy(job)


def dispatch(handler, body=None):
    query = parse_qs(urlsplit(handler.path).query)
    def respond(value, code=200):
        data = json.dumps(value, ensure_ascii=False).encode('utf-8')
        handler.send_response(code)
        handler.send_header('Content-Type', 'application/json; charset=utf-8')
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('Content-Length', str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
    try:
        if handler.command == 'GET':
            job = (get_job(query['job'][0]) if query.get('job')
                   else latest(query.get('id', [''])[0], query.get('path', [''])[0]))
            return respond(dict(ok=True, job=job))
        source = next((s for s in source_browser.roots() if s['id'] == (body or {}).get('id')), None)
        if source is None:
            raise ValueError('项目原目录不存在，请刷新目录')
        relative = (body or {}).get('path') or ''
        return respond(dict(ok=True, job=start(source, relative)), 202)
    except FileNotFoundError as exc:
        return respond(dict(error=str(exc)), 404)
    except (ValueError, OSError) as exc:
        return respond(dict(error=str(exc)), 400)
