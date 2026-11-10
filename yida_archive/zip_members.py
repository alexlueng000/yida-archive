"""Read ZIP members into a content-addressed cache, retaining the original archive."""
import hashlib
import io
import stat
import zipfile
from dataclasses import replace
from pathlib import Path, PurePosixPath

from .models import LogicalFile


def expand(files, cfg, loader, log=None):
    known = {f.content_hash: f for f in files if f.content_hash}
    added = []
    total = count = 0

    def visit(parent, depth):
        nonlocal total, count
        if parent.ext.lower() != '.zip':
            return
        try:
            if depth >= 3:
                raise ValueError('嵌套ZIP超过3层')
            data = loader(parent) if not parent.cache_path else Path(parent.cache_path).read_bytes()
            if data is None:
                return
            if len(data) > 256 * 1024 * 1024:
                raise ValueError('ZIP超过256MB')
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    name = info.filename
                    if not info.flag_bits & 0x800:
                        try:
                            name = name.encode('cp437').decode('gb18030')
                        except (UnicodeError, LookupError):
                            pass
                    path = PurePosixPath(name.replace('\\', '/'))
                    if path.is_absolute() or '..' in path.parts or ':' in name or stat.S_ISLNK(info.external_attr >> 16):
                        if log:
                            log('ZIP跳过不安全成员：' + name)
                        continue
                    if '__MACOSX' in path.parts or path.name == '.DS_Store':
                        continue
                    count += 1
                    if count > 1000 or info.file_size > 64 * 1024 * 1024 or total + info.file_size > 256 * 1024 * 1024:
                        raise ValueError('ZIP解压数量或体积超过限制')
                    try:
                        with archive.open(info) as stream:
                            body = stream.read(64 * 1024 * 1024 + 1)
                        if len(body) > 64 * 1024 * 1024:
                            raise ValueError('ZIP成员超过64MB')
                    except (RuntimeError, zipfile.BadZipFile, NotImplementedError) as exc:
                        if log:
                            log('ZIP成员无法读取：%s（%s）' % (name, type(exc).__name__))
                        continue
                    total += len(body)
                    digest = hashlib.sha256(body).hexdigest()
                    obs = [replace(o, name=path.name, size=len(body), file_uuid='', url='',
                                   content_hash=digest, document_id='', revision=0,
                                   field_label=o.field_label + ' / ZIP:' + parent.name + '/' + name)
                           for o in parent.obs]
                    if digest in known:
                        known[digest].obs.extend(o for o in obs if o not in known[digest].obs)
                        continue
                    cache = Path(cfg.work_dir) / 'zip-members' / digest
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_bytes(body)
                    child = LogicalFile(digest, path.name, len(body), '', '', digest, obs,
                                        cache_path=str(cache))
                    known[digest] = child
                    added.append(child)
                    visit(child, depth + 1)
            parent.review_note = 'ZIP原包保留，包内文件已单独处理'
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
            parent.review_note = 'ZIP解压未完成：' + str(exc)
        if log:
            log(parent.name + '：' + parent.review_note)

    for f in list(files):
        visit(f, 0)
    files.extend(added)
    return added
