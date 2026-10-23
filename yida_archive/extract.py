# -*- coding: utf-8 -*-
"""从宜搭返回的 formData 里抽出附件项。

formData 是 {fieldId: value} 的平铺字典，附件字段的值是 JSON 字符串，
子表字段的值是行列表、行里还可能再嵌附件字段。所以这里递归遍历，
不依赖字段类型表，避免漏掉子表里的附件。
"""

import json


def _walk(value, path, out):
    if isinstance(value, str):
        if 'fileUuid' not in value:
            return
        try:
            arr = json.loads(value)
        except (ValueError, TypeError):
            return
        if not isinstance(arr, list):
            return
        for item in arr:
            if isinstance(item, dict) and item.get('fileUuid'):
                out.append((path, item))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _walk(item, '%s[%d]' % (path, i), out)
    elif isinstance(value, dict):
        # 有的接口把附件数组解析好了直接给（不是 JSON 字符串），
        # 这时字典自己就带 fileUuid，认到就收，不再往下钻。
        if value.get('fileUuid'):
            out.append((path, value))
            return
        for key, sub in value.items():
            _walk(sub, (path + '.' + key) if path else key, out)


def find_attachments(form_data):
    """返回 [(字段路径, 附件项字典), ...]。"""
    out = []
    _walk(form_data or {}, '', out)
    return out


def field_id_of(path):
    """从 'a.b[0].c' 取出 'c'。

    真实响应里少数字段的键带 _value / _text 后缀（同一批数据里有的带有的不带），
    去掉后缀才能对上配置里的字段 id。
    """
    name = path.split('.')[-1].split('[')[0]
    for suffix in ('_value', '_text'):
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return name


def get_field(form_data, field_id):
    """按字段 id 取值，兼容带 _value / _text 后缀的键。"""
    if field_id in form_data:
        return form_data[field_id]
    for suffix in ('_value', '_text'):
        if field_id + suffix in form_data:
            return form_data[field_id + suffix]
    return None


def matches_project(form_data, project_field, project_no):
    """精确核对项目号，子表字段用 '父.子' 写法。

    平台搜索是模糊匹配，必须再核对一次，否则会混进别的单据。
    """
    if not project_field:
        return True
    if '.' in project_field:
        table, col = project_field.split('.', 1)
        rows = get_field(form_data, table) or []
        if isinstance(rows, str):
            try:
                rows = json.loads(rows)
            except (ValueError, TypeError):
                return False
        for row in rows:
            if isinstance(row, dict) and get_field(row, col) == project_no:
                return True
        return False
    return get_field(form_data, project_field) == project_no
