# -*- coding: utf-8 -*-
"""探字段：把某个表单里所有附件字段的真实 id 和出现次数列出来。

部署到新项目时先跑这个，避免凭猜写 field_labels / field_hints。
"""

from . import extract


def scan(instances):
    """返回 {form_name: {field_id: {'label':..., 'count':..., 'samples':[...]}}}"""
    out = {}
    for inst in instances:
        bucket = out.setdefault(inst.form_name, {})
        for att in inst.attachments:
            slot = bucket.setdefault(att.field_id, {
                'label': att.field_label, 'count': 0, 'samples': []})
            slot['count'] += 1
            if att.name not in slot['samples'] and len(slot['samples']) < 3:
                slot['samples'].append(att.name)
    return out


def render(instances):
    """给人看的文本。"""
    lines = []
    for form in sorted(scan(instances)):
        lines.append('表单：%s' % form)
        for fid, info in sorted(scan(instances)[form].items()):
            lines.append('  %s  出现 %d 次  例：%s'
                         % (fid, info['count'], '、'.join(info['samples'])))
        lines.append('')
    return '\n'.join(lines) or '没有扫描到附件字段。'
