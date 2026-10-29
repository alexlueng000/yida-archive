"""Project catalogue and request-scoped archive sessions for the local application."""
import hashlib
import io
import json
import mimetypes
import os
import shutil
import time
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, quote

from web import server as web
from yida_archive import naming, numbering


def catalogue_path():
    return web.SESSION_DIR.parent / 'web-projects' / 'projects.json'


def key_for(purchaser, project_no):
    return hashlib.sha256((purchaser + '\0' + project_no).encode()).hexdigest()[:24]


def save_catalogue(items):
    path = catalogue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def catalogue():
    path = catalogue_path()
    items = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    # Existing archives become projects automatically; their original files stay in place.
    for ledger in Path(web.OUTPUT_ROOT).glob('*/*/编号记录.json'):
        if ledger.is_symlink() or ledger.parent.is_symlink() or ledger.parent.parent.is_symlink():
            continue
        data = json.loads(ledger.read_text(encoding='utf-8'))
        purchaser, no = data.get('purchaser'), data.get('project')
        if not purchaser or not no:
            continue
        ident = key_for(purchaser, no)
        items.setdefault(ident, dict(id=ident, purchaser=purchaser, project_no=no,
                                   name=no, created=int(ledger.stat().st_mtime * 1000)))
    # Preserve the old test session as an independent project, including pending uploads.
    legacy = web.SESSION_DIR / 'session.json'
    if legacy.exists():
        data = json.loads(legacy.read_text(encoding='utf-8'))
        if data.get('batches'):
            purchaser, no = data['purchaser'], data['project_no']
            ident = key_for(purchaser, no)
            item = items.setdefault(ident, dict(id=ident, purchaser=purchaser, project_no=no,
                                              name=no, created=data.get('saved_at', 0)))
            if not item.get('legacy_imported'):
                target = path.parent / ident
                if not target.exists():
                    import shutil
                    shutil.copytree(web.SESSION_DIR, target)
                item['legacy_imported'] = True
    save_catalogue(items)
    return items


@contextmanager
def session(item):
    original = dict(web.STATE)
    original_dir = web.SESSION_DIR
    try:
        web.SESSION_DIR = catalogue_path().parent / item['id']
        web.STATE.clear()
        web.STATE.update(project_no=item['project_no'], purchaser=item['purchaser'],
                         project_secret='否', unmatched_policy='manual',
                         batches=[], blobs={}, last_result=None, file_reviews={})
        web.load_session()
        web.STATE.update(project_secret='否', unmatched_policy='manual')
        yield
    finally:
        web.SESSION_DIR = original_dir
        web.STATE.clear()
        web.STATE.update(original)


def root_for(item):
    root = Path(web.OUTPUT_ROOT).resolve()
    base = root / naming.sanitize(item['purchaser']) / naming.sanitize(item['project_no'])
    if root not in base.resolve().parents or base.is_symlink() or base.parent.is_symlink():
        raise ValueError('项目目录包含外部链接')
    return base


def inventory(item):
    base = root_for(item)
    ledger = base / numbering.FILENAME
    units = json.loads(ledger.read_text(encoding='utf-8')).get('units', {}) if ledger.exists() else {}
    metadata = {u.get('path'): u for u in units.values()}
    output = Path(web.OUTPUT_ROOT).resolve()
    files = []
    for path in sorted(base.rglob('*')) if base.exists() else []:
        if (path.is_symlink() or any(p.is_symlink() for p in path.parents if p != base.parent)
                or not path.is_file() or base.resolve() not in path.resolve().parents):
            continue
        rel = path.relative_to(base)
        if len(rel.parts) == 1:  # application reports and ledger are not uploaded documents
            continue
        unit = metadata.get(path.resolve().relative_to(output).as_posix(), {})
        raw = unit.get('file') or {}
        obs = raw.get('obs') or []
        # 同一份文件可能来自多条单据，取最晚的那次审批通过时间；网页用它核对来源。
        approved = max([o.get('approved_at') or 0 for o in obs] or [0])
        folder = rel.parts[-2]
        if folder == '待归类':
            folder = '未归类'
        files.append(dict(path=rel.as_posix(), id=raw.get('ident', ''), name=path.name,
                          origin=raw.get('name', path.name), folder=folder,
                          reason=raw.get('review_note') or (raw.get('ai_result') or {}).get('reason')
                          or '历史归档未记录归类理由',
                          type=unit.get('type', ''), needs_review=not bool(unit.get('type')), size=path.stat().st_size,
                          modified=int(path.stat().st_mtime * 1000), approved=approved,
                          uploads=len(obs) or 1))
    return files


def sources(item):
    """项目里的文件分别来自哪些单据，带提交和审批通过时间。

    网页上用这张表核对数据真伪：每条单据都能拿去宜搭里对上。
    编号记录里每条来源记录都存了流程、单据 id、标题和时间，不用重新查接口。
    """
    base = root_for(item)
    ledger = base / numbering.FILENAME
    if not ledger.exists():
        return []
    units = json.loads(ledger.read_text(encoding='utf-8')).get('units', {})
    seen = {}
    for unit in units.values():
        for obs in (unit.get('file') or {}).get('obs') or []:
            instance = obs.get('instance_id') or ''
            if not instance:
                continue
            key = (obs.get('form_uuid') or '', instance)
            row = seen.setdefault(key, dict(form=obs.get('form_name') or '未知流程',
                                            title=obs.get('instance_title') or '',
                                            submitted=obs.get('submitted_at') or 0,
                                            approved=0, files=set()))
            row['approved'] = max(row['approved'], obs.get('approved_at') or 0)
            row['files'].add(unit.get('path') or '')
    rows = [dict(row, files=len(row['files'])) for row in seen.values()]
    return sorted(rows, key=lambda r: (r['submitted'], r['form']))


def detail(item):
    files = inventory(item)
    uploads = sum(len(b['files']) for b in web.STATE['batches'])
    return dict(project=item, files=files, uploads=uploads, sources=sources(item),
                pending=sum(f['needs_review'] for f in files),
                folders=web._config().folders,
                types=[dict(type=r['type'], folder=r['folder']) for r in web._config().rules],
                path=str(root_for(item)), batches=len(web.STATE['batches']))


def send_bytes(handler, data, filename, content_type, inline=False):
    handler.send_response(200)
    handler.send_header('Content-Type', content_type)
    handler.send_header('Content-Disposition', ('inline' if inline else 'attachment') + "; filename*=UTF-8''" + quote(filename))
    handler.send_header('Content-Length', str(len(data)))
    handler.send_header('X-Content-Type-Options', 'nosniff')
    if inline:
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('Content-Security-Policy', "sandbox; default-src 'none'")
    handler.end_headers()
    handler.wfile.write(data)


def change_purchaser(items, item, purchaser):
    purchaser = str(purchaser or '').strip()
    if not purchaser or len(purchaser) > 120 or naming.sanitize(purchaser) != purchaser:
        raise ValueError('请输入有效的采购单位名称')
    if purchaser == item['purchaser']:
        return item
    if any(i['purchaser'].casefold() == purchaser.casefold() and
           i['project_no'].casefold() == item['project_no'].casefold() for i in items.values()):
        raise ValueError('该采购单位下已存在此项目号')
    from web import source_classify
    if source_classify._active:
        raise ValueError('正在进行目录归类，请完成后再修改采购单位')
    old_id = item['id']
    new_id = key_for(purchaser, item['project_no'])
    updated = dict(item, id=new_id, purchaser=purchaser, legacy_imported=True)
    old_base, new_base = root_for(item), root_for(updated)
    old_session = catalogue_path().parent / old_id
    new_session = catalogue_path().parent / new_id
    if new_base.exists() or new_session.exists() or new_id in items:
        raise ValueError('目标目录或项目记录已存在，无法自动迁移')
    backup = catalogue_path().parent / ('migration-' + uuid.uuid4().hex)
    backup.mkdir(parents=True)
    moved_old = False
    try:
        if old_base.exists():
            if any(p.is_symlink() for p in old_base.rglob('*')):
                raise ValueError('归档目录包含链接，无法自动迁移')
            new_base.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(old_base, new_base)
            for filename in (numbering.FILENAME, '归档规范.json'):
                path = new_base / filename
                if path.exists():
                    data = json.loads(path.read_text(encoding='utf-8'))
                    data['purchaser'] = purchaser
                    if filename == numbering.FILENAME:
                        old_prefix = old_base.relative_to(Path(web.OUTPUT_ROOT).resolve()).as_posix() + '/'
                        new_prefix = new_base.relative_to(Path(web.OUTPUT_ROOT).resolve()).as_posix() + '/'
                        for unit in data.get('units', {}).values():
                            path_value = unit.get('path', '')
                            if path_value:
                                if not path_value.startswith(old_prefix):
                                    raise ValueError('编号记录包含非当前项目路径')
                                unit['path'] = new_prefix + path_value[len(old_prefix):]
                    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        if old_session.exists():
            shutil.copytree(old_session, backup / 'session')
            shutil.copytree(old_session, new_session)
            path = new_session / 'session.json'
            if path.exists():
                data = json.loads(path.read_text(encoding='utf-8'))
                data['purchaser'] = purchaser
                path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        if old_base.exists():
            shutil.move(str(old_base), str(backup / 'archive'))
            moved_old = True
        new_items = dict(items)
        del new_items[old_id]
        new_items[new_id] = updated
        save_catalogue(new_items)
    except Exception:
        if moved_old:
            shutil.move(str(backup / 'archive'), str(old_base))
        if new_base.exists(): shutil.rmtree(new_base)
        if new_session.exists(): shutil.rmtree(new_session)
        raise
    # Original session and archive remain in the migration backup for recovery.
    return updated


def dispatch(handler, body=None):
    url = urlsplit(handler.path)
    query = parse_qs(url.query)
    body = body or {}
    # Handler._json checkpoints the active session for POSTs; catalogue routes have no active session.
    def respond(data, code=200):
        payload = json.dumps(data, ensure_ascii=False).encode('utf-8')
        handler.send_response(code)
        handler.send_header('Content-Type', 'application/json; charset=utf-8')
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('Content-Length', str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)
    try:
        items = catalogue()
        if url.path == '/api/projects':
            if handler.command == 'POST':
                purchaser = str(body.get('purchaser') or '').strip()
                no = str(body.get('project_no') or '').strip()
                name = str(body.get('name') or no).strip()
                if not purchaser or not no:
                    raise ValueError('请填写采购单位和项目号')
                if any(len(v) > 120 or naming.sanitize(v) != v for v in (purchaser, no)):
                    raise ValueError('采购单位和项目号不能包含路径符号、保留名称或超过120个字符')
                ident = key_for(purchaser, no)
                # Windows paths are case-insensitive; prevent two records sharing one directory.
                if any(i['purchaser'].casefold() == purchaser.casefold() and
                       i['project_no'].casefold() == no.casefold() for i in items.values()):
                    raise ValueError('该采购单位下已存在此项目号，请打开已有项目')
                item = dict(id=ident, purchaser=purchaser, project_no=no, name=name[:200],
                            created=int(time.time() * 1000))
                items[ident] = item
                save_catalogue(items)
                return respond(dict(ok=True, project=item))
            rows = []
            for item in items.values():
                files = inventory(item)
                rows.append(dict(item, count=len(files), size=sum(f['size'] for f in files),
                                 pending=sum(f['needs_review'] for f in files),
                                 updated=max([f['modified'] for f in files] + [item['created']])))
            return respond(dict(ok=True, projects=sorted(rows, key=lambda i: -i['updated'])))
        ident = query.get('id', [None])[0]
        if ident not in items:
            return respond(dict(error='项目不存在，请返回首页重新选择'), 404)
        item = items[ident]
        if url.path == '/api/project/purchaser' and handler.command == 'POST':
            updated = change_purchaser(items, item, body.get('purchaser'))
            with session(updated):
                return respond(dict(ok=True, **detail(updated)))
        if url.path == '/api/project/file' and handler.command == 'GET':
            rel = query.get('path', [''])[0]
            if rel not in {f['path'] for f in inventory(item)}:
                return respond(dict(error='文件不存在'), 404)
            path = root_for(item) / rel
            inline = query.get('view', [''])[0] == '1' and path.suffix.lower() == '.pdf'
            return send_bytes(handler, path.read_bytes(), path.name,
                              'application/pdf' if inline else (mimetypes.guess_type(path.name)[0] or 'application/octet-stream'),
                              inline=inline)
        if url.path == '/api/project/export' and handler.command == 'GET':
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as archive:
                for f in inventory(item):
                    archive.write(root_for(item) / f['path'], item['project_no'] + '/' + f['path'])
            return send_bytes(handler, buf.getvalue(), item['project_no'] + '.zip', 'application/zip')
        with session(item):
            if url.path == '/api/project/detail' and handler.command == 'GET':
                return respond(dict(ok=True, **detail(item)))
            if url.path == '/api/project/review' and handler.command == 'POST':
                fid, kind = body.get('id'), body.get('type')
                if not fid or fid not in {f['id'] for f in inventory(item)}:
                    raise ValueError('文件不存在或缺少分类记录')
                if kind not in {r['type'] for r in web._config().rules}:
                    raise ValueError('请选择当前项目允许的文件类型')
                previous = dict(web.STATE['file_reviews'])
                web.STATE['file_reviews'][fid] = dict(previous.get(fid, {}), type=kind)
                try:
                    result = web.save_result()
                    if not result.get('ok'):
                        raise ValueError('分类保存失败，原文件保持不变')
                    web.save_session()
                except Exception:
                    web.STATE['file_reviews'] = previous
                    raise
                return respond(dict(ok=True, **detail(item)))
            if url.path == '/api/project/upload' and handler.command == 'POST':
                if not body.get('files'):
                    raise ValueError('请选择要上传的文件')
                batch = handler._new_batch({'name': body.get('name') or '文件上传'})['batch']
                uploaded = handler._upload(dict(body, batch_id=batch['id']))
                web.save_session()  # keep originals available for retry after archive failure
                if not uploaded.get('added'):
                    return respond(dict(error='没有可保存的文件', skipped=uploaded.get('skipped', [])), 400)
                result = web.analyze()
                saved = web.save_result()
                web.save_session()
                if not saved.get('ok'):
                    return respond(dict(error='自动归档失败，上传原件已保留，可点击重试保存',
                                        saved=saved), 500)
                return respond(dict(ok=True, added=len(uploaded['added']), skipped=uploaded['skipped'],
                                    summary=result['summary'], **detail(item)))
            if url.path == '/api/project/save' and handler.command == 'POST':
                result = web.save_result()
                web.save_session()
                if not result.get('ok'):
                    return respond(dict(error='保存失败，原件已保留', saved=result), 500)
                return respond(dict(ok=True, **detail(item)))
        return respond(dict(error='未知接口'), 404)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return respond(dict(error=str(exc)), 400)
