"""读取已下载的「每次上传」目录；保持原文件，直接校验内容后归档。"""
import hashlib
import re
import shutil
from datetime import datetime
from pathlib import Path

from .archive import _sha256
from .models import AttachmentObs, Instance


def load(directory):
    root = Path(directory).resolve()
    if not root.is_dir():
        raise ValueError('本地附件目录不存在：%s' % root)
    instances = {}
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.is_symlink() or path.name.startswith('~$') or path.name.endswith('.part'):
            continue
        rel = path.relative_to(root)
        if len(rel.parts) < 3:
            raise ValueError('请指定「每次上传」目录，结构应为 单据/附件字段/文件：%s' % rel)
        batch, field = rel.parts[0], rel.parts[1]
        match = re.match(r'^\d+-(\d{8})-(\d{4})-([^-]+)-(.*)$', batch)
        stamp = int(datetime.strptime(match[1] + match[2], '%Y%m%d%H%M').timestamp() * 1000) if match else 0
        form, title = (match[3], match[4]) if match else ('本地附件', batch)
        iid = str(root / batch)
        if iid not in instances:
            instances[iid] = Instance(form, 'LOCAL', iid, title, stamp)
        uid = hashlib.sha256(str(path).encode('utf-8')).hexdigest()
        instances[iid].attachments.append(AttachmentObs(
            form, 'LOCAL', iid, title, stamp, field, field,
            path.name, path.stat().st_size, uid, str(path), _sha256(path)))
    return list(instances.values())


class LocalClient:
    def __init__(self, directories):
        self.roots = [Path(p).resolve() for p in directories]

    def download(self, url, dest_path):
        source = Path(url).resolve()
        if not any(root in source.parents for root in self.roots):
            raise ValueError('源文件不在指定的本地附件目录内')
        shutil.copyfile(source, dest_path)
        return source.stat().st_size
