"""全局固定类型编号，同类型多份加稳定子号，只输出终版。"""
import re
import copy
import unicodedata
from collections import defaultdict
from . import numbering
from .config import TYPE_NUMBERS

_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def sanitize(text):
    text = _BAD.sub('_', str(text or '').strip()).strip(' .') or '未命名'
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', text, re.I):
        text = '_' + text
    return text


def _order(f):
    return min((o.submitted_at, o.source_order) for o in f.obs)


def _version_name(f):
    stem = unicodedata.normalize('NFKC', f.name.rsplit('.', 1)[0]).casefold().strip()
    match = re.search(r'(?:[ _-]*v([1-9]\d*)|[ (（]v([1-9]\d*)[)）])$', stem)
    return ((stem[:match.start()].strip(' _-'), int(match[1] or match[2]), True)
            if match else (stem, 1, False))


def assign(files, cfg, project_no):
    state = copy.deepcopy(numbering.load(cfg))
    cfg._numbering_before = copy.deepcopy(state)
    records = state['units']
    hash_index = {h: key for key, unit in records.items() for h in unit['hashes']}
    reviews = cfg.raw.get('file_reviews') or {}
    active, candidates = [], defaultdict(list)
    for f in files:
        f.new_name, f.superseded, f.numbering_id = '', False, ''
        f.version_group, f.version_order = '', 0
        review = reviews.get(f.ident, {})
        if review.get('version_group') and not f.folder:
            raise ValueError('版本组必须属于允许归档的文件类型')
        if not f.folder or f.confidence == 'manual':
            if cfg.unmatched_policy == 'manual':
                f.new_name = sanitize(f.name)
                previous = hash_index.get(f.content_hash)
                key = previous if previous and not records[previous]['type'] else 'pending:' + f.ident
                records[key] = dict(seq=0, sub=0, multi=False, type='', base='',
                                    hashes={f.content_hash: 1} if f.content_hash else {},
                                    latest=1, path='', hash='')
                f.numbering_id = key
            continue
        active.append(f)
        base, version, explicit = _version_name(f)
        candidates[(f.type_name, base)].append((f, version, explicit))

    auto = {}
    for (kind, base), entries in candidates.items():
        existing = [(key, unit) for key, unit in records.items()
                    if unit['type'] == kind and unit.get('base') == base]
        # 同名的后续提交按内容变化递增；文件名中的数字不决定输出版本号。
        batches = [set((o.form_uuid, o.instance_id) for o in f.obs) for f, _, _ in entries
                   if not getattr(f, '_from_archive', False)]
        ambiguous = any(batches[i] & batches[j] for i in range(len(batches)) for j in range(i))
        if len(existing) > 1 or ambiguous or any(not f.content_hash for f, _, _ in entries):
            for f, _, _ in entries:
                f.review_note += '；同名文件关系有歧义或内容未验证，分别保留'
            continue
        key = existing[0][0] if existing else 'name:' + kind + ':' + base
        prior = existing[0][1] if existing else {}
        known = dict(prior.get('hashes', {}))
        next_version = max(known.values(), default=0)
        for f, _, _ in sorted(entries, key=lambda e: _order(e[0])):
            if f.content_hash not in known:
                next_version += 1
                known[f.content_hash] = next_version
            auto[id(f)] = (key, known[f.content_hash], base)

    groups = defaultdict(list)
    for f in sorted(active, key=_order):
        review = reviews.get(f.ident, {})
        base, _, _ = _version_name(f)
        metadata = {(o.document_id, o.revision) for o in f.obs if o.document_id}
        key = hash_index.get(f.content_hash, 'file:' + f.ident)
        version = records.get(key, {}).get('hashes', {}).get(f.content_hash, 1)
        linked = False
        if review.get('version_group'):
            key, version = 'review:' + str(review['version_group']), int(review.get('version_order') or 0)
            linked = True
        elif metadata:
            if len({doc for doc, _ in metadata}) != 1:
                raise ValueError('同一内容存在冲突的逻辑文件ID')
            key = 'document:' + next(iter(metadata))[0]
            version = max(rev for _, rev in metadata)
            if not version:
                unit = records.get(key, {})
                version = unit.get('hashes', {}).get(f.content_hash, unit.get('latest', 0) + 1)
            linked = True
        elif id(f) in auto:
            key, version, base = auto[id(f)]
            linked = True
        if linked and (version < 1 or not f.content_hash):
            raise ValueError('版本号必须为正整数，版本内容必须通过哈希验证')
        # 若后来补入明确的版本关系，继承已保存文件的序号。
        previous_key = hash_index.get(f.content_hash)
        if key not in records and previous_key:
            records[key] = records.pop(previous_key)
            hash_index = {h: k for k, u in records.items() for h in u['hashes']}
        if key not in records:
            seq = TYPE_NUMBERS[f.type_name]
            state['types'][f.type_name] = seq
            sub = max([u['sub'] for u in records.values() if u['seq'] == seq] + [0]) + 1
            records[key] = dict(seq=seq, sub=sub, multi=False, type=f.type_name,
                                base=base, hashes={}, latest=version, path='', hash='')
        unit = records[key]
        if unit['type'] != f.type_name:
            seq = TYPE_NUMBERS[f.type_name]
            unit.update(seq=seq, sub=max([u['sub'] for k, u in records.items()
                                         if k != key and u['seq'] == seq] + [0]) + 1,
                        multi=False)
        if any(v == version and digest != f.content_hash for digest, v in unit['hashes'].items()) and linked:
            raise ValueError('同一文件的同一版本号对应不同内容，不能覆盖：' + f.name)
        if f.content_hash:
            unit['hashes'][f.content_hash] = version
        unit['latest'] = max(unit['latest'], version)
        unit['base'] = unit.get('base') or base
        unit['type'] = f.type_name
        state['types'].setdefault(f.type_name, unit['seq'])
        f.numbering_id, f.version_order = key, version
        f.version_group = key if linked or version > 1 else ''
        groups[key].append(f)

    for key, items in groups.items():
        unit = records[key]
        if len({(f.folder, f.type_name) for f in items}) != 1:
            raise ValueError('同一逻辑文件的版本必须归为同一类型')
        current = max(items, key=lambda f: f.version_order)
        if current.version_order < unit['latest']:
            raise ValueError('本次只包含旧版本，不能覆盖已归档最新版：' + current.name)
        if len({f.version_order for f in items}) != len(items):
            raise ValueError('同一版本组的版本号不能重复')
        for f in items:
            f.superseded = f is not current
        unit['multi'] = unit['multi'] or sum(u['seq'] == unit['seq'] for u in records.values()) > 1
        tname = sanitize(cfg.type_label(current.type_name))
        if current.version_order > 1:
            tname += 'v%d' % current.version_order
        pattern = cfg.multi_pattern if unit['multi'] else cfg.pattern
        current.new_name = sanitize(pattern.format(seq=unit['seq'], sub=unit['sub'], type=tname,
                                                   project=sanitize(project_no), ext=current.ext))
    cfg._numbering_plan = state
    return files


def target_dir(f, cfg):
    return cfg.structure.format(
        root=cfg.encrypted_root if f.secret else cfg.public_root,
        purchaser=sanitize(cfg.purchaser), project=sanitize(cfg.project_no),
        folder=sanitize(f.folder or (cfg.raw.get('storage') or {}).get('pending_folder', '待归类')), type=sanitize(f.type_name))
