# -*- coding: utf-8 -*-
"""从本地快照 JSON 读数据，不连宜搭。

用途：在没有 token 的机器上先把去重和归类跑通，
或者把线上数据固定成一份样本，回归时反复用。

快照格式见 README 的「快照格式」一节。
"""

import io
import json

from .models import AttachmentObs, Instance


def load(path, form_names=None):
    with io.open(path, encoding='utf-8') as fh:
        snap = json.load(fh)
    out = []
    for inst in snap.get('instances') or []:
        if form_names and inst.get('form_name') not in form_names:
            continue
        obj = Instance(
            form_name=inst.get('form_name', ''),
            form_uuid=inst.get('form_uuid', ''),
            instance_id=inst.get('instance_id', ''),
            title=inst.get('title', ''),
            submitted_at=int(inst.get('submitted_at') or 0),
            originator=inst.get('originator', ''),
            status=inst.get('status', ''),
        )
        for att in inst.get('attachments') or []:
            obj.attachments.append(AttachmentObs(
                form_name=obj.form_name,
                form_uuid=obj.form_uuid,
                instance_id=obj.instance_id,
                instance_title=obj.title,
                submitted_at=obj.submitted_at,
                field_id=att.get('field_id', ''),
                field_label=att.get('field_label', ''),
                name=att.get('name', ''),
                size=att.get('size'),
                file_uuid=att.get('file_uuid', ''),
                url=att.get('url', ''),
                content_hash=att.get('content_hash', ''),
                document_id=att.get('document_id', ''),
                revision=int(att.get('revision') or 0),
            ))
        out.append(obj)
    return out


def snapshot_meta(path):
    with io.open(path, encoding='utf-8') as fh:
        snap = json.load(fh)
    return {'project_no': snap.get('project_no', ''),
            'purchaser': snap.get('purchaser', ''),
            'generated_at': snap.get('generated_at', '')}


def dump(instances, project_no, purchaser, path):
    """把拉到的实例写成本地快照，便于离线回归。"""
    data = {
        'project_no': project_no,
        'purchaser': purchaser,
        'instances': [],
    }
    for inst in instances:
        data['instances'].append({
            'form_name': inst.form_name,
            'form_uuid': inst.form_uuid,
            'instance_id': inst.instance_id,
            'title': inst.title,
            'submitted_at': inst.submitted_at,
            'originator': inst.originator,
            'status': inst.status,
            'attachments': [{
                'field_id': a.field_id,
                'field_label': a.field_label,
                'name': a.name,
                'size': a.size,
                'file_uuid': a.file_uuid,
                'url': a.url,
                'content_hash': a.content_hash,
                'document_id': a.document_id,
                'revision': a.revision,
            } for a in inst.attachments],
        })
    with io.open(path, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    return path
