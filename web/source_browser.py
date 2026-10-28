"""Read-only browsing of original project upload directories."""
import hashlib
import json
import mimetypes
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, parse_qs, quote

from web import server as web

SOURCE_ROOT = Path(web.REPO)
PREVIEW_TYPES = {'application/pdf', 'image/png', 'image/jpeg', 'image/gif',
                 'image/webp', 'text/plain'}


def safe_path(base, relative=''):
    relative = str(relative)
    parts = PurePosixPath(relative).parts
    if '\\' in relative or ':' in relative or relative.startswith('/') or '..' in parts:
        raise ValueError('目录路径无效')
    target = base.joinpath(*parts)
    if target.resolve() != base and base not in target.resolve().parents:
        raise ValueError('目录路径超出原始附件范围')
    for ancestor in [target, *target.parents]:
        if ancestor == base.parent:
            break
        if ancestor.is_symlink() or (hasattr(ancestor, 'is_junction') and ancestor.is_junction()):
            raise ValueError('不支持访问链接目录或文件')
    return target


def roots():
    base = Path(SOURCE_ROOT).resolve()
    out = []
    for project in sorted(base.glob('archive-*')):
        if not project.is_dir() or project.is_symlink() or project.resolve().parent != base:
            continue
        folder = project / '每次上传'
        try:
            safe_path(base, project.name + '/每次上传')
        except ValueError:
            continue
        if not folder.is_dir():
            continue
        out.append(dict(id=hashlib.sha256(project.name.encode()).hexdigest()[:24],
                        name=project.name.removeprefix('archive-'),
                        path=str(folder), modified=int(folder.stat().st_mtime * 1000)))
    return out


def list_directory(source, relative=''):
    base = Path(source['path']).resolve()
    folder = safe_path(base, relative)
    if not folder.is_dir():
        raise FileNotFoundError('原目录不存在或已被移动')
    from web import source_classify
    history = source_classify.directory_history(source['id'])
    entries = []
    for path in folder.iterdir():
        rel = path.relative_to(base).as_posix()
        try:
            safe_path(base, rel)
        except ValueError:
            continue
        if not path.is_dir() and not path.is_file():
            continue
        stat = path.stat()
        directory = path.is_dir()
        children = []
        if directory:
            for child in path.iterdir():
                try:
                    safe_path(base, child.relative_to(base).as_posix())
                    children.append(child)
                except ValueError:
                    continue
        content_type = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        entries.append(dict(name=path.name, path=rel, kind='directory' if directory else 'file',
                            size=0 if directory else stat.st_size,
                            modified=int(stat.st_mtime * 1000),
                            folders=sum(p.is_dir() for p in children),
                            files=sum(p.is_file() for p in children),
                            preview=not directory and content_type in PREVIEW_TYPES,
                            classification=history.get(rel) if directory else None))
    return dict(ok=True, source=source, path='' if folder == base else folder.relative_to(base).as_posix(),
                classification=history.get(relative),
                entries=sorted(entries, key=lambda e: (e['kind'] != 'directory', e['name'].casefold())))


def dispatch(handler):
    query = parse_qs(urlsplit(handler.path).query)
    route = urlsplit(handler.path).path
    def respond(value, code=200):
        data = json.dumps(value, ensure_ascii=False).encode('utf-8')
        handler.send_response(code)
        handler.send_header('Content-Type', 'application/json; charset=utf-8')
        handler.send_header('Content-Length', str(len(data)))
        handler.send_header('Cache-Control', 'no-store')
        handler.end_headers()
        handler.wfile.write(data)
    try:
        sources = roots()
        if route == '/api/sources':
            return respond(dict(ok=True, sources=sources))
        source = next((s for s in sources if s['id'] == query.get('id', [''])[0]), None)
        if source is None:
            return respond(dict(error='原目录不存在，请重新选择'), 404)
        relative = query.get('path', [''])[0]
        if route == '/api/source/tree':
            return respond(list_directory(source, relative))
        path = safe_path(Path(source['path']).resolve(), relative)
        if not path.is_file():
            return respond(dict(error='文件不存在'), 404)
        content_type = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        inline = query.get('view', [''])[0] == '1' and content_type in PREVIEW_TYPES
        with path.open('rb') as stream:
            handler.send_response(200)
            handler.send_header('Content-Type', content_type if inline else 'application/octet-stream')
            handler.send_header('Content-Disposition', ('inline' if inline else 'attachment') +
                                "; filename*=UTF-8''" + quote(path.name))
            handler.send_header('Content-Length', str(path.stat().st_size))
            handler.send_header('X-Content-Type-Options', 'nosniff')
            handler.send_header('Cache-Control', 'no-store')
            handler.send_header('Content-Security-Policy', "sandbox; default-src 'none'")
            handler.end_headers()
            while chunk := stream.read(1024 * 1024):
                handler.wfile.write(chunk)
    except FileNotFoundError as exc:
        return respond(dict(error=str(exc)), 404)
    except (ValueError, OSError) as exc:
        return respond(dict(error=str(exc)), 400)
