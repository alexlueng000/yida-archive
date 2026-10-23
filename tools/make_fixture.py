# -*- coding: utf-8 -*-
"""把之前跑出来的 archive_final.json 转成程序用的快照格式，做离线样本。"""

import io
import json
import os
import sys

SRC = r'D:\work\.cache\openyida\business-system\archive_final.json'
DST = r'D:\work\yida-archive\fixtures\B22026LD001.json'

forms = {
    '合同审批': ('FORM-HT866U91JVN4V3LWAWXX5AYC8UG63K2N7EM9LQZ5',
                 {'合同附件': 'attachmentField_lxson6vd',
                  '附件': 'attachmentField_l7u2h6rf'}),
    '货款支付': ('FORM-HT866U91JVN4V3LWAWXX5AYC8UG63K2N7EM9LNX5',
                 {'上传发票': 'attachmentField_lfj2zmnt',
                  '附件': 'attachmentField_ky8afins'}),
}

with io.open(SRC, encoding='utf-8') as fh:
    raw = json.load(fh)

# 按 (流程, 提交时间) 分单据。
# 注意：不能用标题分 —— 同一个项目会有多条合同审批，标题一模一样，
# 按标题分会把两条单据并成一条，附件数直接翻倍。
buckets = {}
order = []
for o in raw['obs']:
    key = (o['form'], o['gmt'])
    if key not in buckets:
        buckets[key] = []
        order.append(key)
    buckets[key].append(o)
order.sort(key=lambda k: (k[0], k[1]))

instances = []
for i, key in enumerate(order):
    form, gmt = key
    uuid, field_map = forms.get(form, ('', {}))
    rows = sorted(buckets[key], key=lambda r: (r.get('order', 0), r['name']))
    inst = {
        'form_name': form,
        'form_uuid': uuid,
        'instance_id': 'SNAP-%s-%03d' % (form, i),
        'title': rows[0]['title'],
        'submitted_at': gmt,
        'originator': '',
        'status': 'COMPLETE',
        'attachments': [{
            'field_id': field_map.get(r['field'], r['field']),
            'field_label': r['field'],
            'name': r['name'],
            'size': r['size'],
            'file_uuid': r['uuid'],
            'url': r['url'],
        } for r in rows],
    }
    instances.append(inst)

snap = {
    'project_no': 'B22026LD001',
    'purchaser': '深圳理工',
    'generated_at': '2026-09-23 (from archive_final.json, %d obs)' % len(raw['obs']),
    'instances': instances,
}

os.makedirs(os.path.dirname(DST), exist_ok=True)
with io.open(DST, 'w', encoding='utf-8') as fh:
    json.dump(snap, fh, ensure_ascii=False, indent=2)

n = sum(len(i['attachments']) for i in instances)
sys.stdout.write('instances=%d attachments=%d -> %s\n'
                 % (len(instances), n, DST))
