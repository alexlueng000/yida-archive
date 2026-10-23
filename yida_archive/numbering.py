"""项目编号账本：预览只读，实际保存成功后提交。"""
import json
import os
from pathlib import Path
from dataclasses import asdict

FILENAME = '编号记录.json'


def project_dir(cfg):
    from .naming import sanitize
    return Path(cfg.out_dir) / sanitize(cfg.purchaser) / sanitize(cfg.project_no)


def restore_reviews(cfg):
    path = project_dir(cfg) / '人工确认.json'
    if path.is_file():
        stored = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(stored, dict):
            raise ValueError('人工确认文件格式不正确')
        cfg.raw['file_reviews'] = dict(stored, **(cfg.raw.get('file_reviews') or {}))


def load(cfg):
    base = project_dir(cfg)
    path = base / FILENAME
    legacy = Path(cfg.out_dir) / (cfg.project_no + '_文件分类')
    if not path.exists() and legacy.is_dir():
        raise ValueError('检测到旧归档，请先运行 tools/migration_preview.py 生成迁移预演；原目录未修改')
    if path.exists():
        data = json.loads(path.read_text(encoding='utf-8'))
        if (data.get('schema') != 2 or data.get('project') != cfg.project_no
                or data.get('purchaser') != cfg.purchaser):
            raise ValueError('编号记录版本或项目号不匹配，已停止以避免重新编号')
        return data
    data = {'schema': 2, 'project': cfg.project_no, 'purchaser': cfg.purchaser,
            'types': {}, 'units': {}}
    if (any((base / name).exists() for name in ('清单.csv', 'archive-清单.csv'))
            and not (base / '归档规范.json').exists()):
        raise ValueError('旧清单需要先迁移至固定编号，原文件未修改')
    return data


def write(cfg, base=None):
    data = getattr(cfg, '_numbering_plan', None)
    if data is None:
        return
    directory = Path(base) if base is not None else project_dir(cfg)
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / FILENAME
    temp = directory / (FILENAME + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, dest)


def finalize(cfg, files, done, cleanup=False, base=None, preserve_modified=False):
    """只清理账本追踪且内容未被外部修改的旧输出，绝不扫描删除源附件。"""
    from .archive import _sha256
    state = cfg._numbering_plan
    root = Path(cfg.out_dir).resolve()
    active_keys = {f.numbering_id for f in files if f.numbering_id}
    active_hashes = {h for k in active_keys for h in state['units'][k]['hashes']}
    for f, path in done:
        if f.numbering_id:
            unit = state['units'][f.numbering_id]
            unit['hashes'][f.content_hash] = f.version_order or 1
            unit['path'] = Path(path).resolve().relative_to(root).as_posix()
            unit['hash'] = f.content_hash
            saved = asdict(f)
            saved['cache_path'] = ''
            saved['url'] = ''
            for obs in saved['obs']:
                obs['url'] = ''
            unit['file'] = saved
    if cleanup:
        destinations = {Path(p).resolve() for _, p in done}
        stale = []
        for unit in cfg._numbering_before['units'].values():
            if not unit.get('path') or not active_hashes.intersection(unit['hashes']):
                continue
            old = (root / unit['path']).resolve()
            if root not in old.parents:
                raise ValueError('旧文件路径超出输出目录')
            if old in destinations or not old.exists():
                continue
            if not old.is_file() or _sha256(old) != unit.get('hash'):
                if preserve_modified:
                    # 本轮附件已另存成功，外部修改的旧副本原样留下。
                    continue
                raise ValueError('旧归档文件已被外部修改，未删除：' + str(old))
            stale.append(old)
        for old in stale:
            old.unlink()
    write(cfg, base)


def include_history(files, cfg):
    """读已归档的最新文件与来源记录；无需重新上传旧附件。"""
    from .models import AttachmentObs, LogicalFile
    from .dedupe import refine
    state = load(cfg)
    root = Path(cfg.out_dir).resolve()
    current_hashes = {f.content_hash for f in files if f.content_hash}
    historical = []
    for key, unit in state['units'].items():
        if not unit.get('path') or not unit.get('hash'):
            continue
        path = (root / unit['path']).resolve()
        if root not in path.parents:
            raise ValueError('历史附件路径超出归档目录')
        if not path.is_file():
            # 旧版整目录覆盖可能已删除该文件；占号记录不能冒充真实文件。
            continue
        raw = unit.get('file')
        if raw:
            raw = dict(raw)
            raw['obs'] = [AttachmentObs(**o) for o in raw['obs']]
            f = LogicalFile(**raw)
        else:
            name = unit.get('base', '') + path.suffix
            obs = AttachmentObs('历史归档', 'ARCHIVE', key, '', 0, '', '附件',
                                name, path.stat().st_size, '', '', unit['hash'])
            f = LogicalFile(unit['hash'], name, obs.size, '', '', unit['hash'], [obs])
            f.type_name = unit['type']
            rule = next((r for r in cfg.rules if r['type'] == unit['type']), None)
            if rule:
                f.folder, f.confidence = rule['folder'], 'confirmed'
        f.cache_path = str(path)
        f._from_archive = True
        if f.folder and not any(r['type'] == f.type_name for r in cfg.rules):
            f.folder = f.type_name = ''
            f.confidence = 'manual'
            f.review_note = '历史类型不在当前采购单位定义内，进入待归类'
        if unit['hash'] in current_hashes:
            current = next(x for x in files if x.content_hash == unit['hash'])
            def identity(o):
                return (o.form_uuid, o.instance_id, o.field_id, o.file_uuid, o.name, o.submitted_at)
            seen = {identity(o) for o in current.obs}
            current.obs.extend(o for o in f.obs if identity(o) not in seen)
            current.cache_path = str(path)
            if ((cfg.raw.get('ai') or {}).get('enabled') and f.folder
                    and 'type' not in (cfg.raw.get('file_reviews') or {}).get(current.ident, {}) and (
                    current.type_name != f.type_name or not current.folder)):
                # 同内容的旧归档不可因模型失败或改判而在整目录保存时丢失。
                current.folder, current.type_name, current.confidence = f.folder, f.type_name, f.confidence
                current.review_note += '；保留历史已归档分类：' + f.type_name
                current.ai_result = dict(current.ai_result, status='history_preserved')
            continue
        historical.append(f)
    return refine(files + historical)
