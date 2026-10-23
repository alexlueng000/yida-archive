# -*- coding: utf-8 -*-
"""下载、算哈希、落盘。

落盘是「先写临时文件再改名」，避免中途断了留下半个文件。
dry_run 只出方案，不下载不写盘。
"""

import hashlib
import io
import os
import shutil
import tempfile


from . import dedupe, naming


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def hash_all(files, client, work_dir, log=None):
    """把每个逻辑文件的代表副本下载下来算内容哈希。

    同一 fileUuid 只下一次，省带宽也省时间。
    下载失败的记下来，不中断整批。
    """
    os.makedirs(work_dir, exist_ok=True)
    for f in files:
        if f.cache_path and os.path.isfile(f.cache_path) and _sha256(f.cache_path) == f.content_hash:
            continue
        fd, tmp = tempfile.mkstemp(prefix='download-', suffix=naming.sanitize(f.ext), dir=work_dir)
        os.close(fd)
        urls = list(dict.fromkeys(o.url for o in reversed(f.obs) if o.url)) or [f.url]
        for url in urls:
            try:
                client.download(url, tmp)
                if os.path.getsize(tmp) == 0:
                    raise RuntimeError('下载到 0 字节')
                digest = _sha256(tmp)
                if f.content_hash and digest != f.content_hash:
                    raise RuntimeError('下载内容与已有哈希不一致')
                cached = os.path.join(work_dir, digest)
                os.replace(tmp, cached)
                f.content_hash, f.cache_path, f.ident = digest, cached, digest
                f.size = os.path.getsize(cached)
                f.download_error = ''
                for o in f.obs:
                    o.content_hash = digest
                break
            except Exception as exc:
                f.download_error = str(exc)
        _safe_remove(tmp)
        if f.download_error and log:
            log('下载失败 %s: %s' % (f.name, f.download_error))
    return files


def _safe_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def merge_after_hash(files):
    """哈希算完再合一次，同内容不同名的会并到一起。"""
    return dedupe.refine(files)


def plan(files, cfg):
    """生成落盘方案：[(逻辑文件, 目标绝对路径)]，并处理重名。"""
    out_root = os.path.abspath(cfg.out_dir)
    taken = set()
    items = []
    for f in files:
        if not f.new_name or f.superseded:
            continue
        rel = os.path.join(naming.target_dir(f, cfg), f.new_name)
        abs_path = os.path.realpath(os.path.join(out_root, rel))
        if os.path.commonpath([os.path.realpath(out_root), abs_path]) != os.path.realpath(out_root):
            raise ValueError('归档路径超出输出目录：%s' % rel)
        abs_path = _dedupe_path(abs_path, taken)
        taken.add(abs_path.lower())
        items.append((f, abs_path))
    return items


def create_folders(cfg):
    """公共、加密各建五个业务文件夹，空分类也可直接查看。"""
    from types import SimpleNamespace
    root = os.path.realpath(cfg.out_dir)
    for secret in (False, True):
        for folder in list(dict.fromkeys(cfg.folders + [(cfg.raw.get('storage') or {}).get('pending_folder', '待归类')])):
            stub = SimpleNamespace(secret=secret, folder=folder, type_name='')
            target = os.path.realpath(os.path.join(root, naming.target_dir(stub, cfg)))
            if os.path.commonpath([root, target]) != root:
                raise ValueError('分类路径超出输出目录')
            os.makedirs(target, exist_ok=True)


def _dedupe_path(path, taken):
    if path.lower() not in taken:
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while ('%s_%d%s' % (stem, n, ext)).lower() in taken:
        n += 1
    return '%s_%d%s' % (stem, n, ext)


def write(files, cfg, client, work_dir, log=None):
    """真正下载并落盘。返回 (落地清单, 失败清单)。"""
    cfg.validate_save()
    items = plan(files, cfg)
    out_root = os.path.abspath(cfg.out_dir)
    os.makedirs(work_dir, exist_ok=True)
    done, failed = [], []

    for f, dest in items:
        tmp = None
        try:
            if f.download_error:
                raise RuntimeError(f.download_error)
            if f.content_hash and os.path.isfile(dest) and _sha256(dest) == f.content_hash:
                done.append((f, dest))
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            fd, tmp = tempfile.mkstemp(suffix='.part', dir=os.path.dirname(dest))
            os.close(fd)
            if f.cache_path:
                shutil.copyfile(f.cache_path, tmp)
            else:
                client.download(f.url, tmp)
            if os.path.getsize(tmp) == 0:
                raise RuntimeError('下载到 0 字节')
            digest = _sha256(tmp)
            if f.content_hash and f.content_hash != digest:
                raise RuntimeError('落盘前哈希校验失败')
            f.content_hash = digest
            # 不覆盖目录里已有的其他内容；相同内容重复执行不产生副本。
            stem, ext = os.path.splitext(dest)
            n = 2
            while True:
                try:
                    os.link(tmp, dest)
                    break
                except FileExistsError:
                    if _sha256(dest) == digest:
                        break
                    dest = '%s_%d%s' % (stem, n, ext)
                    n += 1
            _safe_remove(tmp)
        except Exception as exc:
            f.download_error = str(exc)
            if tmp:
                _safe_remove(tmp)
            failed.append((f, str(exc)))
            if log:
                log('落盘失败 %s -> %s: %s' % (f.name, dest, exc))
            continue
        done.append((f, dest))

    return done, failed


def write_manifest(done, failed, cfg, path):
    """落盘清单，方便和源文件对账。"""
    lines = ['目标路径\t原始文件名\t大小\t上传次数\t来源流程\t内容哈希']
    for f, dest in done:
        rel = os.path.relpath(dest, os.path.abspath(cfg.out_dir))
        lines.append('\t'.join([
            rel.replace('\\', '/'), f.name, str(f.size or ''),
            str(f.upload_count), '/'.join(f.forms), f.content_hash,
        ]))
    for f, err in failed:
        lines.append('\t'.join(['FAILED', f.name, str(f.size or ''),
                                str(f.upload_count), '/'.join(f.forms), err]))
    with io.open(path, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')
    return path
