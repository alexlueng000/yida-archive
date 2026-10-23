"""只按平台 UUID 或已验证的 SHA-256 合并，名称和大小仅用于提示。"""
import hashlib
from collections import defaultdict

from .models import DedupeFindings, LogicalFile


def flatten(instances):
    return [o for inst in instances for o in inst.attachments]


def _order(o):
    return (o.submitted_at, o.source_order, o.instance_id, o.field_id)


def group(instances):
    observations = flatten(instances)
    for index, observation in enumerate(observations):
        observation.source_order = index
    uuid_hashes = defaultdict(set)
    for o in observations:
        if o.file_uuid and o.content_hash:
            uuid_hashes[o.file_uuid].add(o.content_hash)
    buckets = {}
    for i, o in enumerate(observations):
        hashes = uuid_hashes[o.file_uuid]
        digest = o.content_hash or (next(iter(hashes)) if len(hashes) == 1 else '')
        if digest:
            key = ('hash', digest)
        elif o.file_uuid and len(hashes) <= 1:
            key = ('uuid', o.file_uuid)
        else:
            key = ('observation', i)
        buckets.setdefault(key, []).append(o)
    files = []
    for key, items in buckets.items():
        f = _make_file(sorted(items, key=_order))
        if key[0] == 'observation':
            f.ident += '-%s' % key[1]
        files.append(f)
    files.sort(key=lambda f: (_order(f.obs[0]), f.ident))
    return files, findings_for(files)


def _make_file(items):
    head = items[0]
    hashes = {o.content_hash for o in items if o.content_hash}
    digest = next(iter(hashes)) if len(hashes) == 1 else ''
    identity = '|'.join([head.instance_id, head.field_id, head.name,
                         str(head.size), str(head.submitted_at), head.url])
    return LogicalFile(
        ident=digest or head.file_uuid or hashlib.sha256(identity.encode()).hexdigest(),
        name=head.name, size=head.size, file_uuid=head.file_uuid,
        url=head.url, content_hash=digest, obs=items)


def refine(files):
    merged, out = {}, []
    for f in files:
        if f.content_hash and f.content_hash in merged:
            target = merged[f.content_hash]
            target.obs.extend(f.obs)
            target.obs.sort(key=_order)
            if not target.cache_path:
                target.cache_path = f.cache_path
        else:
            if f.content_hash:
                f.ident = f.content_hash
                merged[f.content_hash] = f
            out.append(f)
    return out


def findings_for(files):
    findings = DedupeFindings()
    by_name, by_size = defaultdict(list), defaultdict(list)
    for f in files:
        for name in {o.name for o in f.obs}:
            by_name[name].append(f)
        if f.size is not None:
            by_size[f.size].append(f)
    for name, items in by_name.items():
        if len(items) > 1:
            findings.version_conflicts[name] = sorted({f.size for f in items if f.size is not None})
    for size, items in by_size.items():
        names = sorted({o.name for f in items for o in f.obs})
        if len(items) > 1 and len(names) > 1 and any(not f.content_hash for f in items):
            findings.rename_candidates[size] = names
    return findings


def find_findings(observations):
    return findings_for([_make_file([o]) for o in observations])
