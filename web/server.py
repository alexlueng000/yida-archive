# -*- coding: utf-8 -*-
"""项目档案本地应用，支持多个项目、自动去重分类和普通文件归档。

只监听 127.0.0.1，项目会话自动保存在 .cache/web-projects，重启后恢复。

跑法：
    py web\\server.py
    然后浏览器打开 http://127.0.0.1:8765
"""

import base64
import copy
import argparse
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if __name__ == '__main__':
    sys.modules['web.server'] = sys.modules[__name__]

from yida_archive import ai_classify, archive, classify, dedupe, fetch_json, naming, numbering, report
from yida_archive.config import Config, load as load_config
from yida_archive.models import AttachmentObs, Instance

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HOST = '127.0.0.1'
PORT = 8765
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
MAX_FILE = 64 * 1024 * 1024
MAX_TOTAL = 512 * 1024 * 1024
OUTPUT_ROOT = os.path.join(REPO, '归档结果')
SERVER_CONFIG = {}

SESSION_DIR = Path(REPO) / '.cache' / 'web-session'
_lock = threading.RLock()
STATE = {
    'project_no': 'B22026LD001',
    'purchaser': '测试单位',
    'unmatched_policy': 'manual',
    'project_secret': '',
    'batches': [],
    'blobs': {},          # uuid -> bytes
    'last_result': None,
    'file_reviews': {},
}


def save_session():
    """先落盘附件，再原子替换进度，避免中断写坏上次记录。调用者持锁。"""
    folder = SESSION_DIR / 'blobs'
    folder.mkdir(parents=True, exist_ok=True)
    snapshot = {key: value for key, value in STATE.items() if key != 'blobs'}
    refs = {}
    for uid, data in STATE['blobs'].items():
        digest = hashlib.sha256(data).hexdigest()
        target = folder / digest
        if not target.exists():
            temp = folder / (digest + '.tmp')
            temp.write_bytes(data)
            os.replace(temp, target)
        refs[uid] = digest
    snapshot = dict(snapshot, blobs=refs, saved_at=int(time.time() * 1000))
    temp = SESSION_DIR / 'session.tmp'
    temp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding='utf-8')
    os.replace(temp, SESSION_DIR / 'session.json')
    # 清空或删除的附件也从本地测试缓存移除。
    keep = set(refs.values())
    for target in folder.iterdir():
        if target.name not in keep:
            target.unlink()


def load_session():
    path = SESSION_DIR / 'session.json'
    if not path.exists():
        return False
    snapshot = json.loads(path.read_text(encoding='utf-8'))
    blobs = {}
    for uid, digest in snapshot['blobs'].items():
        if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('本地测试附件索引无效')
        data = (SESSION_DIR / 'blobs' / digest).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('本地测试附件校验失败')
        blobs[uid] = data
    restored = {key: snapshot[key] for key in STATE if key != 'blobs'}
    restored['blobs'] = blobs
    STATE.update(restored)
    return True


def _update_settings(body):
    """人工确认按采购单位和项目隔离，切换项目不能沿用内存校正。"""
    if any(key in body and body[key] != STATE[key] for key in ('project_no', 'purchaser')):
        STATE['file_reviews'] = {}
    for key in ('project_no', 'purchaser', 'unmatched_policy', 'project_secret'):
        if key in body:
            STATE[key] = body[key]


# ---------- 数据组装 ----------
def _instances():
    """把批次转成 pipeline 认的 Instance。一个批次 = 一次上传。"""
    out = []
    for b in STATE['batches']:
        inst = Instance(
            form_name=b['name'] or '未命名批次',
            form_uuid='WEB',
            instance_id=b['id'],
            title=b['name'] or '未命名批次',
            submitted_at=b['created'],
            originator='本地测试',
        )
        for f in b['files']:
            inst.attachments.append(AttachmentObs(
                form_name=inst.form_name,
                form_uuid='WEB',
                instance_id=b['id'],
                instance_title=inst.title,
                submitted_at=b['created'],
                field_id='webField',
                field_label=f.get('field') or '附件',
                name=f['name'],
                size=f['size'],
                # 故意给每个文件一个独立 uuid，模拟宜搭「重新上传会发新 uuid」，
                # 这样才是真的在测去重，而不是靠 uuid 蒙对。
                file_uuid=f['uuid'],
                url='',
                content_hash=f['sha256'],
            ))
        out.append(inst)
    return out


def _config(overrides=None):
    o = overrides or {}
    raw = copy.deepcopy(SERVER_CONFIG)
    raw['scope'] = {
            'project_no': o.get('project_no') or STATE['project_no'],
            'purchaser': o.get('purchaser') or STATE['purchaser'],
            'project_secret': False,
        }
    raw.setdefault('classification', {})['unmatched_policy'] = 'manual'
    raw.setdefault('storage', {})['out_dir'] = OUTPUT_ROOT
    raw['storage']['structure'] = '{purchaser}/{project}/{folder}'
    raw['storage']['pending_folder'] = '未归类'
    raw['file_reviews'] = STATE['file_reviews']
    raw['preserve_distinct_revisions'] = any(
        f.get('cached_digest') for b in STATE['batches'] for f in b['files'])
    cfg = Config(raw)
    numbering.restore_reviews(cfg)
    return cfg


def _source_cache(files):
    digests = {f['cached_digest'] for b in STATE['batches'] for f in b['files']
               if f.get('cached_digest')}
    for f in files:
        if f.content_hash not in digests:
            continue
        digest = f.content_hash
        if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('原目录附件缓存索引无效')
        path = SESSION_DIR / 'source-files' / digest
        if not path.is_file() or path.is_symlink():
            raise ValueError('原目录附件缓存不存在，请重新归类')
        f.cache_path = str(path)


def _prepare(cfg, log=None):
    files, _ = dedupe.group(_instances())
    files = dedupe.refine(files)
    _source_cache(files)
    classify.classify(files, cfg)
    ai_classify.apply(files, cfg, _bytes_of, log=log)
    files = numbering.include_history(files, cfg)
    _source_cache(files)
    classify.mark_secret(files, cfg)
    naming.assign(files, cfg, cfg.project_no)
    return files, dedupe.findings_for(files)


def analyze(overrides=None):
    cfg = _config(overrides)
    instances = _instances()
    uploads = sum(len(i.attachments) for i in instances)
    if not uploads:
        return {'ok': False, 'message': '还没有上传任何文件。'}

    files, findings = _prepare(cfg)

    summary = classify.describe(files, cfg)
    summary['uploads'] = uploads
    summary['batches'] = len(instances)

    grouped = {}
    manual = []
    for f in files:
        row = _file_row(f, cfg)
        if f.superseded:
            continue
        if f.folder and f.new_name:
            grouped.setdefault(f.folder, []).append(row)
        else:
            manual.append(row)

    result = {
        'ok': True,
        'summary': summary,
        'unmatched_policy': cfg.unmatched_policy,
        'folders': [{'name': k, 'files': v}
                    for k, v in sorted(grouped.items())],
        'manual': manual,
        'files': [_file_row(f, cfg) for f in files],
        'types': [{'type': r['type'], 'folder': r['folder']} for r in cfg.rules],
        'output_root': OUTPUT_ROOT,
        'findings': {
            'version_conflicts': [
                {'name': n, 'sizes': s}
                for n, s in sorted(findings.version_conflicts.items())],
            'rename_candidates': [
                {'size': s, 'names': n}
                for s, n in sorted(findings.rename_candidates.items(),
                                   key=lambda kv: -kv[0])],
        },
        'merged': _merged_view(files),
    }
    STATE['last_result'] = result
    return result


def _file_row(f, cfg):
    return {
        'id': f.ident,
        'version_group': f.version_group,
        'version_order': f.version_order,
        'superseded': f.superseded,
        'note': f.review_note,
        'ai': f.ai_result,
        'new_name': f.new_name,
        'type': f.type_name,
        'folder': f.folder,
        'confidence': f.confidence,
        'secret': f.secret,
        'origin': f.name,
        'size': f.size,
        'uploads': f.upload_count,
        'forms': f.forms,
        'hash': (f.content_hash or '')[:12],
        'target': naming.target_dir(f, cfg).replace('\\', '/') if f.new_name else '',
        'members': [{'uuid': o.file_uuid, 'batch': o.form_name,
                     'field': o.field_label, 'name': o.name}
                    for o in f.obs],
    }


def _merged_view(files):
    """只列被合并过的（上传次数 > 1），这是去重的证据。"""
    out = []
    for f in files:
        if f.upload_count < 2:
            continue
        names = {o.name for o in f.obs}
        out.append({
            'name': f.name,
            'uploads': f.upload_count,
            'batches': sorted({o.form_name for o in f.obs}),
            'renamed': len(names) > 1,
            'names': sorted(names),
            'hash': (f.content_hash or '')[:12],
        })
    out.sort(key=lambda r: -r['uploads'])
    return out


# ---------- 导出 ----------
def export_zip():
    result = STATE['last_result'] or analyze()
    if not result.get('ok'):
        return None, result.get('message', '没有可导出的内容')

    cfg = _config()
    cfg.validate_save()
    files, findings = _prepare(cfg)

    buf = io.BytesIO()
    tmp = tempfile.mkdtemp()
    try:
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            used = set()
            for f, destination in archive.plan(files, cfg):
                data = _bytes_of(f)
                if data is None:
                    continue
                rel = os.path.relpath(destination, cfg.out_dir)
                rel = _unique_in_zip(rel.replace('\\', '/'), used)
                zf.writestr(rel, data)

            csv_path = report.write_csv(files, cfg,
                                        os.path.join(tmp, '清单.csv'))
            md_path = report.write_markdown(files, findings, cfg,
                                            classify.describe(files, cfg),
                                            os.path.join(tmp, '结果.md'))
            for p in (csv_path, md_path):
                zf.write(p, os.path.basename(p))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return buf.getvalue(), None


def save_result(progress=None):
    """先完整生成，再替换该项目的结果；失败时保留原目录。"""
    os.makedirs(OUTPUT_ROOT, exist_ok=True)
    root = os.path.realpath(OUTPUT_ROOT)
    cfg = _config()
    cfg.validate_save()
    files, findings = _prepare(cfg, log=progress)
    project_root = numbering.project_dir(cfg).resolve()
    for _, destination in archive.plan(files, cfg):
        if project_root not in Path(destination).resolve().parents:
            raise ValueError('网页保存的目录结构必须位于采购单位/项目号目录内')
    project = os.path.join(naming.sanitize(cfg.purchaser), naming.sanitize(cfg.project_no))
    target = os.path.join(root, project)
    if os.path.realpath(target) != target or os.path.commonpath([root, target]) != root:
        raise ValueError('项目输出目录不能是链接或位于归档结果之外')
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.saving-', dir=root) as stage:
        result = _save_to(stage, prepared=(cfg, files, findings))
        if result.get('error'):
            return result
        result['path'] = target
        if not result['ok']:
            result['saved'] = 0
            result['message'] = '本次保存失败，原有项目结果保持不变'
            return result
        fresh = os.path.join(stage, project)
        # 备份放在暂存目录之外：若系统连回滚也拒绝，仍保留可恢复的旧结果。
        backup = stage + '-previous'
        # 所有移动、清理都限制在当前输出根目录内。
        for path in (stage, fresh, backup, target):
            if os.path.commonpath([root, os.path.realpath(path)]) != root:
                raise ValueError('保存路径超出归档结果目录')
        had_previous = os.path.exists(target)
        if had_previous:
            os.replace(target, backup)
        try:
            os.replace(fresh, target)
        except Exception:
            if had_previous:
                try:
                    os.replace(backup, target)
                except OSError as exc:
                    raise RuntimeError('替换和回滚失败，原结果保留在：' + backup) from exc
            raise
        if had_previous:
            try:
                shutil.rmtree(backup)
            except OSError:
                result['message'] = '项目已更新，旧结果临时备份未能清理：' + backup
        return result


def _save_to(output, prepared=None):
    """在暂存目录生成和 ZIP 一致的完整归档方案。"""
    if prepared is None:
        cfg = _config()
        files, findings = _prepare(cfg)
    else:
        cfg, files, findings = prepared
    if not files:
        return {'error': '还没有上传文件'}
    previous = numbering.project_dir(cfg)
    cfg.raw['storage']['out_dir'] = output
    if previous.is_dir():
        for item in previous.rglob('*'):
            if item.is_symlink() or os.path.commonpath([os.path.realpath(previous), os.path.realpath(item)]) != os.path.realpath(previous):
                raise ValueError('归档目录包含外部链接，停止保存')
        def link_or_copy(source, target):
            rel = os.path.relpath(source, previous).split(os.sep)
            # 清单等会被原地重写，不能与旧目录共用硬链接。
            if rel[0] not in (cfg.public_root, cfg.encrypted_root):
                return shutil.copy2(source, target)
            try:
                os.link(source, target)
            except OSError:
                shutil.copy2(source, target)
        shutil.copytree(previous, numbering.project_dir(cfg), copy_function=link_or_copy)
    archive.create_folders(cfg)
    class MemoryClient:
        def download(self, key, dest):
            data = STATE['blobs'].get(key)
            if data is None:
                raise ValueError('没有实际文件内容，无法保存附件')
            with open(dest, 'wb') as fh:
                fh.write(data)
    # 历史附件已随旧目录保留，本轮不校验、重写或提交其编号记录。
    current_files = [f for f in files if not getattr(f, '_from_archive', False)
                     or Path(f.cache_path) != Path(OUTPUT_ROOT) / naming.target_dir(f, cfg) / f.new_name]
    for f in current_files:
        f.url = next((o.file_uuid for o in f.obs if o.file_uuid in STATE['blobs']), '')
        if f.url:
            # 同内容重传也必须使用本轮上传内容，不能读取历史归档副本。
            f.cache_path = ''
    done, failed = archive.write(current_files, cfg, MemoryClient(), output)
    base = str(numbering.project_dir(cfg))
    report.write_csv(files, cfg, os.path.join(base, '清单.csv'))
    report.write_markdown(files, findings, cfg, classify.describe(files, cfg), os.path.join(base, '结果.md'))
    archive.write_manifest(done, failed, cfg, os.path.join(base, '落盘清单.tsv'))
    with open(os.path.join(base, '人工确认.json'), 'w', encoding='utf-8') as fh:
        json.dump(cfg.raw['file_reviews'], fh, ensure_ascii=False, indent=2)
    if not failed:
        numbering.finalize(cfg, current_files, done, cleanup=True, base=base,
                           preserve_modified=True)
        # Historical public/encrypted directory shells are no longer part of the application.
        for old_name in ('公共', '加密', '待归类'):
            old_root = Path(base) / old_name
            for item in sorted(old_root.rglob('*'), key=lambda p: len(p.parts), reverse=True) + [old_root]:
                if item.is_dir() and not item.is_symlink():
                    try:
                        item.rmdir()
                    except OSError:
                        pass
    return {'ok': not failed, 'path': base, 'saved': len(done),
            'failed': [{'name': f.name, 'error': err} for f, err in failed],
            'skipped': sum(not f.new_name for f in files)}


def _unique_in_zip(rel, used):
    if rel.lower() not in used:
        used.add(rel.lower())
        return rel
    stem, ext = os.path.splitext(rel)
    n = 2
    while ('%s_%d%s' % (stem, n, ext)).lower() in used:
        n += 1
    rel = '%s_%d%s' % (stem, n, ext)
    used.add(rel.lower())
    return rel


def _bytes_of(f):
    for o in f.obs:
        if o.file_uuid and o.file_uuid in STATE['blobs']:
            return STATE['blobs'][o.file_uuid]
    if f.cache_path:
        with open(f.cache_path, 'rb') as fh:
            data = fh.read()
        if hashlib.sha256(data).hexdigest() != f.content_hash:
            raise ValueError('历史归档内容已被修改：' + f.name)
        return data
    return None


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    server_version = 'project-archive/2.0'

    def log_message(self, fmt, *args):
        sys.stderr.write('  %s\n' % (fmt % args))

    # ---- 工具 ----
    def _json(self, obj, code=200):
        if self.command == 'POST' and code < 400 and not obj.get('error'):
            try:
                save_session()
            except OSError as exc:
                obj = {'error': '测试进度保存失败：%s，请勿关闭应用' % exc}
                code = 500
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode('utf-8'))
        except ValueError:
            return {}

    def _file(self, path):
        full = os.path.join(STATIC, path.lstrip('/') or 'index.html')
        if not os.path.abspath(full).startswith(os.path.abspath(STATIC)):
            return self._json({'error': '非法路径'}, 400)
        if not os.path.exists(full):
            return self._json({'error': '文件不存在'}, 404)
        ctype = ('text/html; charset=utf-8' if full.endswith('.html')
                 else 'text/javascript; charset=utf-8' if full.endswith('.js')
                 else 'text/plain; charset=utf-8')
        with open(full, 'rb') as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        # 改完页面刷新就能看到，不用去清浏览器缓存
        self.send_header('Cache-Control', 'no-store, must-revalidate')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---- 路由 ----
    def do_GET(self):
        if self.path.split('?')[0] == '/api/source/classify-status':
            from web import source_classify
            return source_classify.dispatch(self)
        if self.path.split('?')[0] in ('/api/sources', '/api/source/tree', '/api/source/file'):
            from web import source_browser
            return source_browser.dispatch(self)
        if self.path.startswith('/api/projects') or self.path.startswith('/api/project/'):
            from web import projects
            with _lock:
                return projects.dispatch(self)
        if self.path.startswith('/api/state'):
            return self._json(self._state())
        if self.path == '/api/export':
            return self._export()
        return self._file(self.path.split('?')[0])

    def do_POST(self):
        if self.path.split('?')[0] == '/api/source/classify':
            from web import source_classify
            return source_classify.dispatch(self, self._body())
        with _lock:
            return self._dispatch_post()

    def _dispatch_post(self):
        path = self.path.split('?')[0]
        body = self._body()
        if path == '/api/projects' or path.startswith('/api/project/'):
            from web import projects
            return projects.dispatch(self, body)
        if path in ('/api/save', '/api/review'):
            with _lock:
                try:
                    if path == '/api/review':
                        result = self._review(body)
                    else:
                        _update_settings(body)
                        result = save_result()
                    return self._json(result)
                except (ValueError, OSError) as exc:
                    return self._json({'error': str(exc)}, 400)
        if path == '/api/settings':
            _update_settings(body)
            STATE['last_result'] = None
            return self._json({'ok': True})
        if path == '/api/batch':
            return self._json(self._new_batch(body))
        if path == '/api/upload':
            return self._json(self._upload(body))
        if path == '/api/clone':
            return self._json(self._clone(body))
        if path == '/api/patch':
            return self._json(self._patch(body))
        if path == '/api/seed':
            return self._json(self._seed(body))
        if path == '/api/remove':
            return self._json(self._remove(body))
        if path == '/api/analyze':
            return self._json(self._analyze(body))
        if path == '/api/export':
            return self._export()
        if path == '/api/reset':
            with _lock:
                STATE['batches'] = []
                STATE['blobs'] = {}
                STATE['last_result'] = None
                STATE['file_reviews'] = {}
            return self._json({'ok': True})
        return self._json({'error': '未知接口'}, 404)

    # ---- 接口实现 ----
    def _review(self, body):
        files, _ = _prepare(_config())
        fid = body.get('id')
        if fid not in {f.ident for f in files}:
            raise ValueError('文件不存在，请重新分析')
        previous = dict(STATE['file_reviews'])
        item = dict(previous.get(fid, {}))
        if 'type' in body:
            if body['type'] and body['type'] not in {r['type'] for r in _config().rules}:
                raise ValueError('类型不在当前分类规则内')
            item['type'] = body['type']
        for key in ('version_group', 'version_order'):
            if key in body:
                item[key] = body[key]
        STATE['file_reviews'][fid] = item
        try:
            return analyze()
        except (ValueError, TypeError):
            STATE['file_reviews'] = previous
            raise ValueError('版本组必须属于同一类型，且每份文件的版本号为不同正整数')

    def _state(self):
        with _lock:
            return {
                'project_no': STATE['project_no'],
                'purchaser': STATE['purchaser'],
                'unmatched_policy': STATE['unmatched_policy'],
                'project_secret': STATE.get('project_secret', ''),
                'batches': [{
                    'id': b['id'],
                    'name': b['name'],
                    'field': b.get('field') or '附件',
                    'created': b['created'],
                    'uploaded_at': b.get('uploaded_at', b['created']),
                    'files': [{'id': f['id'], 'name': f['name'],
                               'size': f['size'], 'field': f.get('field'),
                               'sha256': f['sha256'][:12]}
                              for f in b['files']],
                } for b in STATE['batches']],
                'result': STATE['last_result'],
            }

    def _new_batch(self, body):
        with _lock:
            n = len(STATE['batches']) + 1
            b = {'id': uuid.uuid4().hex[:8],
                 'name': (body.get('name') or '第 %d 次上传' % n),
                 'field': '附件',
                 'created': int(time.time() * 1000),
                 'files': []}
            STATE['batches'].append(b)
            STATE['last_result'] = None
            return {'ok': True, 'batch': {'id': b['id'], 'name': b['name'],
                                          'field': b['field'], 'files': []}}

    def _seed(self, body):
        """把离线快照里的真实数据载进来，一上来就能看效果。

        只带元数据，没有文件内容，因此仅按 UUID 分组。
        基线为 95 次上传 -> 72 个未验证内容的分组。
        """
        rel = body.get('path') or os.path.join('fixtures', 'B22026LD001.json')
        path = rel if os.path.isabs(rel) else os.path.join(REPO, rel)
        if not os.path.exists(path):
            return {'error': '找不到快照文件：%s' % rel}
        try:
            instances = fetch_json.load(path)
        except Exception as exc:
            return {'error': '读快照失败：%s' % exc}
        if not instances:
            return {'error': '快照里没有单据。'}

        with _lock:
            STATE['batches'] = []
            STATE['blobs'] = {}
            STATE['last_result'] = None
            STATE['file_reviews'] = {}
            for k, inst in enumerate(instances):
                batch = {
                    'id': uuid.uuid4().hex[:8],
                    'name': inst.title or ('%s 第 %d 次' % (inst.form_name, k + 1)),
                    'field': '',
                    'created': inst.submitted_at or int(time.time() * 1000),
                    'files': [],
                }
                for att in inst.attachments:
                    batch['files'].append({
                        'id': uuid.uuid4().hex[:8],
                        'name': att.name,
                        'size': att.size,
                        'field': att.field_label or '附件',
                        'uuid': att.file_uuid,
                        # 快照没有文件内容，哈希留空：走不了哈希那层
                        'sha256': '',
                    })
                    if not batch['field']:
                        batch['field'] = att.field_label or '附件'
                STATE['batches'].append(batch)
            return {'ok': True,
                    'batches': len(STATE['batches']),
                    'files': sum(len(b['files']) for b in STATE['batches']),
                    'note': '快照没有文件内容，下载 zip 只能拿到清单，'
                            '拿不到归档文件本身。'}

    def _patch(self, body):
        """改一次上传的名字或附件字段名。

        字段名会参与关键词匹配（比如「上传发票」里的「发票」），
        所以改字段名要连同这次上传的所有文件一起改，结果才和线上一致。
        """
        with _lock:
            for b in STATE['batches']:
                if b['id'] != body.get('batch_id'):
                    continue
                if body.get('name'):
                    b['name'] = body['name']
                if body.get('field'):
                    b['field'] = body['field']
                    for f in b['files']:
                        f['field'] = body['field']
                STATE['last_result'] = None
                return {'ok': True}
        return {'error': '批次不存在'}

    def _upload(self, body):
        batch_id = body.get('batch_id')
        items = body.get('files') or []
        added, skipped = [], []
        with _lock:
            batch = None
            for b in STATE['batches']:
                if b['id'] == batch_id:
                    batch = b
                    break
            if batch is None:
                return {'error': '批次不存在'}
            # 字段名以这次上传自己的设置为准
            field = body.get('field') or batch.get('field') or '附件'

            total = sum(len(v) for v in STATE['blobs'].values())
            for it in items:
                name = os.path.basename(it.get('name') or '') or 'unnamed'
                b64 = it.get('data') or ''
                try:
                    raw = base64.b64decode(b64, validate=True)
                except Exception:
                    skipped.append({'name': name, 'why': '内容解析失败'})
                    continue
                if not raw:
                    skipped.append({'name': name, 'why': '文件为空'})
                    continue
                if len(raw) > MAX_FILE:
                    skipped.append({'name': name, 'why': '单个文件超过 64MB'})
                    continue
                if total + len(raw) > MAX_TOTAL:
                    skipped.append({'name': name, 'why': '总量超过 512MB'})
                    continue
                total += len(raw)
                digest = hashlib.sha256(raw).hexdigest()
                fid = uuid.uuid4().hex[:8]
                uid = 'web-%s' % uuid.uuid4().hex[:16]
                STATE['blobs'][uid] = raw
                batch['files'].append({
                    'id': fid, 'name': name, 'size': len(raw),
                    'field': field, 'uuid': uid, 'sha256': digest})
                added.append({'id': fid, 'name': name, 'size': len(raw),
                              'sha256': digest[:12]})
            if added:
                batch['uploaded_at'] = int(time.time() * 1000)
            STATE['last_result'] = None
        return {'ok': True, 'added': added, 'skipped': skipped}

    def _clone(self, body):
        """把一次上传复制成新的一次，内容一样但 uuid 全新。

        这正好模拟「换个单据把同一批文件又传了一遍」——能验证去重是不是真的
        在看内容，而不是靠 uuid 蒙对。
        """
        src_id = body.get('batch_id')
        with _lock:
            src = None
            for b in STATE['batches']:
                if b['id'] == src_id:
                    src = b
                    break
            if src is None:
                return {'error': '批次不存在'}

            name = body.get('name') or (src['name'] + '（重传）')
            new = {'id': uuid.uuid4().hex[:8], 'name': name,
                   'field': src.get('field') or '附件',
                   'created': int(time.time() * 1000), 'files': []}
            for f in src['files']:
                raw = STATE['blobs'].get(f['uuid'])
                if raw is None:
                    continue
                uid = 'web-%s' % uuid.uuid4().hex[:16]
                STATE['blobs'][uid] = raw
                new['files'].append({
                    'id': uuid.uuid4().hex[:8], 'name': f['name'],
                    'size': f['size'], 'field': f.get('field'),
                    'uuid': uid, 'sha256': f['sha256']})
            STATE['batches'].append(new)
            STATE['last_result'] = None
            return {'ok': True, 'batch_id': new['id'], 'count': len(new['files'])}

    def _remove(self, body):
        with _lock:
            kind = body.get('kind')
            if kind == 'batch':
                bid = body.get('batch_id')
                for b in list(STATE['batches']):
                    if b['id'] == bid:
                        for f in b['files']:
                            STATE['blobs'].pop(f['uuid'], None)
                        STATE['batches'].remove(b)
                STATE['last_result'] = None
                return {'ok': True}
            if kind == 'file':
                bid, fid = body.get('batch_id'), body.get('file_id')
                for b in STATE['batches']:
                    if b['id'] != bid:
                        continue
                    for f in list(b['files']):
                        if f['id'] == fid:
                            b['files'].remove(f)
                            STATE['blobs'].pop(f['uuid'], None)
                STATE['last_result'] = None
                return {'ok': True}
        return {'error': '未知类型'}

    def _analyze(self, body):
        with _lock:
            _update_settings(body)
            try:
                return analyze(body)
            except (ValueError, OSError) as exc:
                return {'ok': False, 'message': str(exc), 'error': str(exc)}

    def _export(self):
        with _lock:
            try:
                data, err = export_zip()
            except (ValueError, OSError) as exc:
                data, err = None, str(exc)
        if err:
            return self._json({'error': err}, 400)
        self.send_response(200)
        self.send_header('Content-Type', 'application/zip')
        self.send_header(
            'Content-Disposition',
            'attachment; filename="archive-%s.zip"' % STATE['project_no'])
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Server(ThreadingHTTPServer):
    # http.server 默认开 SO_REUSEADDR；在 Windows 上这会让第二个实例
    # 也能绑上同一个端口而不报错。那样两个进程各有一份内存状态，
    # 请求轮流落到两边，看起来就像「刚传的文件有时在有时不在」。
    # 关掉它，重复启动直接失败，比默默出错强。
    allow_reuse_address = False
    daemon_threads = True


def main():
    global SERVER_CONFIG
    parser = argparse.ArgumentParser(description='附件归档本地网页')
    parser.add_argument('--port', type=int, default=PORT)
    parser.add_argument('--config', default=os.path.join(REPO, 'config.yaml'),
                        help='分类、命名及AI配置文件，默认项目config.yaml')
    args = parser.parse_args()
    if args.config:
        config = load_config(args.config)
        SERVER_CONFIG = config.raw
        STATE.update(project_no=config.project_no or STATE['project_no'],
                     purchaser=config.purchaser, unmatched_policy=config.unmatched_policy,
                     project_secret=('' if config.project_secret is None else
                                     '是' if config.project_secret else '否'))
    try:
        if load_session():
            print('  已恢复上次本地测试进度')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print('  无法恢复本地测试进度：%s；已保留原缓存，请先检查后再启动。' % exc)
        return 1
    if os.name == 'nt':
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for name in ('stdout', 'stderr'):
        s = getattr(sys, name, None)
        if s is not None and hasattr(s, 'reconfigure'):
            try:
                s.reconfigure(encoding='utf-8')
            except Exception:
                pass

    try:
        srv = Server((HOST, args.port), Handler)
    except OSError as exc:
        print('')
        print('  端口 %d 已经被占用了，起不来：%s' % (args.port, exc))
        print('  多半是已经有一个在本机跑着。先关掉它，或者改 PORT 再起。')
        print('  找占用： netstat -ano | findstr :%d' % PORT)
        return 1

    print('')
    print('  项目档案 · 文件归档管理')
    print('  浏览器打开：http://%s:%d' % (HOST, args.port))
    print('  Ctrl+C 停止')
    print('')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n  已停止。')
    finally:
        srv.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
