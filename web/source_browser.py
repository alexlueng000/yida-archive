"""Read-only browsing of original project upload directories."""
import hashlib
import json
import mimetypes
import os
import re
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, parse_qs, quote

from web import server as web

SOURCE_ROOT = Path(web.REPO)
PREVIEW_TYPES = {'application/pdf', 'image/png', 'image/jpeg', 'image/gif',
                 'image/webp', 'text/plain'}

# 项目号有两代写法：26JZBTPB06-003、26NKD-FW010 这类两位年份开头带横杠的，
# 以及 B22026LD001 这类老格式（字母开头、无横杠）。认不出来就不猜，
# 归到「未识别项目」，不硬塞进别的项目。
PROJECT_KEY = re.compile(r'^(?:\d{2}[A-Za-z]{2,8}[A-Za-z0-9]*-\d+|[A-Za-z]\d{5}[A-Za-z]{2,6}\d+)')
UNKNOWN_PROJECT = '未识别项目'
# 夜间任务里没有项目号、认领不了的单据，落在 {out_dir}/_未归组项目/{提交日期}/...
UNATTRIBUTED_DIR = '_未归组项目'
UNATTRIBUTED_PROJECT = '未归组项目'


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


def project_of(name):
    """从目录名里取项目号。取不到返回空串，由调用方归入「未识别项目」。"""
    match = PROJECT_KEY.match(str(name or '').strip())
    return match.group(0).upper() if match else ''


def _source_id(label):
    return hashlib.sha256(label.encode('utf-8')).hexdigest()[:24]


def _measure(folder):
    """统计目录下的文件数和最近修改时间。不跟链接，避免数到别处去。"""
    files, newest = 0, 0
    for root, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames
                       if not os.path.islink(os.path.join(root, d))]
        for name in filenames:
            path = os.path.join(root, name)
            if os.path.islink(path):
                continue
            files += 1
            try:
                newest = max(newest, os.path.getmtime(path))
            except OSError:
                continue
    return files, int(newest * 1000)


def _archive_root():
    """夜间任务落盘的位置：{out_dir}/{采购单位}/{项目号}/公共/{阶段}/..."""
    storage = (web.SERVER_CONFIG or {}).get('storage') or {}
    out_dir = storage.get('out_dir') or './归档结果'
    return (SOURCE_ROOT / out_dir).resolve()


def _batch_sources(base):
    """CLI 按单据下载的批次目录：archive-<项目号>[-全流程]/每次上传。"""
    out = []
    for project in sorted(base.glob('archive-*')):
        if not project.is_dir() or project.is_symlink() or project.resolve().parent != base:
            continue
        try:
            safe_path(base, project.name + '/每次上传')
        except ValueError:
            continue
        folder = project / '每次上传'
        if not folder.is_dir():
            continue
        name = project.name.removeprefix('archive-')
        key = project_of(name)
        marker = project / '.source-project.json'
        if marker.is_file() and not marker.is_symlink():
            try:
                marked = json.loads(marker.read_text(encoding='utf-8')).get('project')
                if isinstance(marked, str) and marked:
                    key = marked
            except (OSError, ValueError, AttributeError):
                pass
        files, newest = _measure(folder)
        out.append(dict(id=_source_id(project.name), name=name, path=str(folder),
                        modified=newest or int(folder.stat().st_mtime * 1000),
                        kind='batch', project=key, purchaser='', files=files))
    return out


def _archive_sources(root, only_unattributed=False):
    """夜间归档的项目目录：{采购单位}/{项目号}，目录名就是项目号。"""
    if not root.is_dir() or root.is_symlink():
        return []
    out = []
    for purchaser in sorted(root.iterdir()):
        if not purchaser.is_dir() or purchaser.is_symlink():
            continue
        if purchaser.name == UNATTRIBUTED_DIR:
            # 这一支是 {日期}/{流程}/{单据标题}，按日期列出来，文件不被悄悄藏起来
            for day in sorted(purchaser.iterdir()):
                if not day.is_dir() or day.is_symlink():
                    continue
                files, newest = _measure(day)
                out.append(dict(id=_source_id('unattributed:' + str(day)), name=day.name,
                                path=str(day), modified=newest or int(day.stat().st_mtime * 1000),
                                kind='unattributed', project=UNATTRIBUTED_PROJECT,
                                purchaser='', files=files))
            continue
        if only_unattributed:
            continue
        for project in sorted(purchaser.iterdir()):
            if not project.is_dir() or project.is_symlink():
                continue
            key = project_of(project.name)
            if not key:
                continue
            files, newest = _measure(project)
            out.append(dict(id=_source_id('archive:' + str(project)), name=project.name,
                            path=str(project), modified=newest or int(project.stat().st_mtime * 1000),
                            kind='archive', project=key, purchaser=purchaser.name, files=files))
    return out


def roots():
    """原始附件来源；归类结果不作为原目录展示。"""
    base = Path(SOURCE_ROOT).resolve()
    return _batch_sources(base) + _archive_sources(_archive_root(), only_unattributed=True)


def purchase_units():
    """项目号到采购单位的对照，用夜间任务已经回查好的缓存，不再重复问接口。

    批次目录名里没有采购单位，没有这份缓存就只能显示「—」。
    """
    path = Path(web.REPO) / '.cache' / 'openapi' / 'project-meta.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key).upper(): (value or {}).get('customer') or ''
            for key, value in data.items() if isinstance(value, dict)}


def projects(sources):
    """按项目号汇总。同一项目的多个下载批次自动合并成一条。"""
    units = purchase_units()
    groups = {}
    for source in sources:
        key = source['project'] or UNKNOWN_PROJECT
        group = groups.setdefault(key, dict(project=key, purchaser=units.get(key, ''), files=0,
                                            modified=0, batches=0, archived=False,
                                            sources=[]))
        group['sources'].append(source)
        group['files'] += source['files']
        group['modified'] = max(group['modified'], source['modified'])
        if source['purchaser']:
            group['purchaser'] = source['purchaser']
        if source['kind'] == 'archive':
            group['archived'] = True
        elif source['kind'] == 'batch':
            group['batches'] += 1
    for group in groups.values():
        group['sources'].sort(key=lambda s: (s['kind'] != 'archive', s['name'].casefold()))
    return sorted(groups.values(), key=lambda g: (g['project'] == UNKNOWN_PROJECT,
                                                  -g['modified'], g['project']))


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
            return respond(dict(ok=True, sources=sources, projects=projects(sources)))
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
