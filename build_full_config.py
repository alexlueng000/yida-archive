# -*- coding: utf-8 -*-
"""从缓存的摸底结果生成「全流程」配置。

数据源（都来自 2026-09-22/23 那轮全应用分析，非本次猜测）：
  upload-points.json    39 个附件上传点：表名 + formUuid + 附件字段 id
  zuqh-page-source.js   「项目流程查询」页面源码，里面的 PROCESS_FORMS 数组
                        给出 27 张表的 name/formUuid/projectField
  forms-summary.json    244 张表的字段摘要（其中 136 张有字段），用来补
                        PROCESS_FORMS 里没有的那 17 个上传点的关联键

为什么不手写配置：39 个 formUuid 加 39 个字段 id，手抄必错。这里全部按
formUuid 从证据里取，取不到就明确跳过并说明原因，不猜。

用法：
    py build_full_config.py                 # 写到默认路径（PROJECT_NO）
    py build_full_config.py --project 25XX-01   # 换项目号，表单清单不变
    py build_full_config.py --out X.yaml
"""
import io
import json
import os
import re
import sys

CACHE = r'D:\work\.cache\openyida\business-system'
APP_TYPE = 'APP_R55Z1QDKMB0VILUQRNJA'
PROJECT_NO = '24JZBTPFB157-401'

# 从 forms-summary.json 的字段摘要里人工认定的关联键。
# 每条都是「表名 → (字段 id, 字段标签, 说明)」，标签是摘要里原样抄的。
# 标了「合同号」的那些，用项目号去搜大概率 0 命中——留着是为了跑一次看实况。
FROM_SCHEMA = {
    '项目备案': ('textField_0nab92f', '项目信息编号', '唯一带「项目」的文本字段'),
    '合同签约跟踪': ('textField_mcvqg7sn', '项目号', '标签就是项目号'),
    '放置单位外': ('textField_tnui0zr', '合同号', '键是合同号'),
    '科研设备流动': ('textField_dmwst8f', '合同号', '键是合同号'),
    '补充合同': ('textField_l0g7orif', '合同号', '键是合同号（另一个是关联字段）'),
    '采购合同': ('textField_ky9zhf07', '合同号', '键是合同号；该 id 在合同审批里叫「项目号」'),
    '免表延期': ('textField_m24bw8rr', '合同协议号', '键是合同协议号'),
    '月结单付款': ('textField_mqt9q6xh', '合同号', '键是合同号'),
    '开立收据申请': ('textField_ky8b3veg', '合同号', '键是合同号；该 id 在开立发票申请里叫「项目号」'),
    '发票/收据签收': ('textField_ky8b3veg', '合同号', '键是合同号（有两个同名字段，取 id 相同那个）'),
    '付款登记': ('textField_l0tb7041', '合同号', '键是合同号'),
}

# 明确跳过的表，值是原因。这些表进了配置也查不到，或者会把整轮跑挂。
SKIP = {
    '新增地址备案': '表里没有项目号/合同号这类字段，只有单位名称和地址',
    '海关备案（前置）申请': '表里只有主题/流水号/流程状态，没有可做键的字段',
    '关务免表资料制作': '关联键是关联表单字段(associationFormField_ls75quse)，'
                        '不是文本字段，searchFieldJson 的写法不同，先不塞进来',
    '总代理协议及重大项目合作协议': '缓存里这张表的字段摘要为空，需要重新 get-schema',
    '总合作协议（非常规）': '缓存里这张表的字段摘要为空，需要重新 get-schema',
    '南科大重大平台项目合同审批': '缓存里这张表的字段摘要为空，需要重新 get-schema',
}


def load_cache():
    """读三份证据。"""
    up = json.load(io.open(os.path.join(CACHE, 'upload-points.json'),
                           encoding='utf-8'))
    src = io.open(os.path.join(CACHE, 'zuqh-page-source.js'),
                  encoding='utf-8').read()
    i = src.index('PROCESS_FORMS')
    blk = src[i:src.index('];', i)]
    page = {}
    for m in re.finditer(r'\{([^{}]*)\}', blk):
        body = m.group(1)
        row = {}
        for key in ('name', 'formUuid', 'projectField', 'appType'):
            hit = re.search(key + r":\s*'([^']*)'", body)
            if hit:
                row[key] = hit.group(1)
        if row.get('formUuid'):
            page[row['formUuid']] = row
    summary = json.load(io.open(os.path.join(CACHE, 'forms-summary.json'),
                                encoding='utf-8'))
    return up, page, summary


def build():
    up, page, summary = load_cache()
    # 字段摘要按「去掉结尾横线」的表名索引（缓存里表名有的带尾巴横线）
    idx = {}
    for form in summary['forms']:
        idx.setdefault(form['formName'].rstrip('-'), form)

    forms, skipped, unresolved = [], [], []
    for point in up:
        name = point['formName'].rstrip('-')
        uuid = point['formUuid']
        if name in SKIP:
            skipped.append((name, SKIP[name]))
            continue

        field, label, note = None, '', ''
        if page.get(uuid, {}).get('projectField'):
            field = page[uuid]['projectField']
            label, note = '项目号', '来自「项目流程查询」页面源码'
        elif name in FROM_SCHEMA:
            field, label, note = FROM_SCHEMA[name]
        else:
            unresolved.append(name)
            continue

        labels = {}
        for f in point.get('fields') or []:
            if f.get('fieldId') and f.get('label'):
                labels[f['fieldId']] = f['label']

        forms.append({
            'name': name,
            'form_uuid': uuid,
            'kind': 'process',
            'project_field': field,
            'field_labels': labels,
            '_key_label': label,
            '_key_note': note,
        })
    return forms, skipped, unresolved


YIDA = {
    'base_url': 'https://www.aliwork.com',
    'app_type': APP_TYPE,
    'auth_mode': 'cli',
    'access_token_env': 'YIDA_ACCESS_TOKEN',
    'cookie_env': 'YIDA_COOKIE',
    'csrf_token_env': 'YIDA_CSRF_TOKEN',
    'timeout': 60,
    'page_size': 100,
}
STORAGE = {
    'out_dir': './archive-%s-全流程' % PROJECT_NO,
    'structure': '{project}_文件分类/{root}/{folder}',
    'public_root': '公共',
    'encrypted_root': '加密',
    'work_dir': './.work',
}
# 分类规则先照搬单流程的配置：这轮是为了把文件取全，不是调分类。
CLASSIFICATION = {
    'folders': ['合同', '办证', '清关送货', '收付款', '结算移交'],
    'field_hints': [],
    'manual_keywords': ['旧版', '草稿', '作废'],
    'encrypted_types': ['收款凭证', '付款凭证', '退款凭证', '结算表',
                        '发票（国内货物类）', '发票（国内服务类）'],
    'unmatched_policy': 'skip',
}
NAMING = {
    'pattern': '{seq}-{type}-{project}{ext}',
    'multi_pattern': '{seq}-{sub}-{type}-{project}{ext}',
}


def render(forms, skipped, unresolved, project_no=PROJECT_NO):
    out = []
    w = out.append
    w('# %s 全流程配置 —— 由 build_full_config.py 生成，不要手改。' % project_no)
    w('#')
    w('# 覆盖 %d 个附件上传点（应用内共 39 个）。' % len(forms))
    w('# 关联键来自缓存证据，不是猜的，但分两类：')
    w('#   · 标签「项目号」的，按项目号搜正常命中')
    w('#   · 标签是「合同号/合同协议号」的，字段里存的是合同号，')
    w('#     拿项目号去搜大概率 0 命中。留着是为了跑一次看实况。')
    w('#')
    if skipped:
        w('# 跳过的 %d 个：' % len(skipped))
        for name, why in skipped:
            w('#   · %s —— %s' % (name, why))
    if unresolved:
        w('# 另有 %d 个没找到关联键，也跳过了：%s'
          % (len(unresolved), '、'.join(unresolved)))
    w('')
    w('yida:')
    for key in ('base_url', 'app_type', 'auth_mode', 'access_token_env',
                'cookie_env', 'csrf_token_env', 'timeout', 'page_size'):
        w('  %s: %s' % (key, YIDA[key]))
    w('')
    w('scope:')
    w('  project_no: %s' % project_no)
    w('  purchaser: 未指定采购单位')
    w('  forms:')
    for f in forms:
        w('    # 关联键：%s（%s）' % (f['_key_label'], f['_key_note']))
        w('    - name: %s' % f['name'])
        w('      form_uuid: %s' % f['form_uuid'])
        w('      kind: %s' % f['kind'])
        w('      project_field: %s' % f['project_field'])
        if f['field_labels']:
            w('      field_labels:')
            for k, v in f['field_labels'].items():
                w('        %s: %s' % (k, v))
        else:
            w('      field_labels: {}')
    w('')
    w('classification:')
    w('  folders:')
    for x in CLASSIFICATION['folders']:
        w('    - %s' % x)
    w('  field_hints: []')
    w('  manual_keywords:')
    for x in CLASSIFICATION['manual_keywords']:
        w('    - %s' % x)
    w('  encrypted_types:')
    for x in CLASSIFICATION['encrypted_types']:
        w('    - %s' % x)
    w('  unmatched_policy: %s' % CLASSIFICATION['unmatched_policy'])
    w('')
    w('naming:')
    # 必须带引号：{seq} 在 YAML 里是流式映射的开头，裸写解析不了。
    w("  pattern: '%s'" % NAMING['pattern'])
    w("  multi_pattern: '%s'" % NAMING['multi_pattern'])
    w('')
    w('storage:')
    # out_dir 跟着项目号走，一个项目一个目录，互不覆盖。
    storage = dict(STORAGE, out_dir='./archive-%s-全流程' % project_no)
    # 一律加引号：structure 里也有花括号，裸写会当成流式映射。
    for key in ('out_dir', 'structure', 'public_root', 'encrypted_root',
                'work_dir'):
        w("  %s: '%s'" % (key, storage[key]))
    return '\n'.join(out) + '\n'


def main(argv):
    # 多个项目共用同一份表单清单，只有 scope.project_no 和输出目录不同，
    # 所以 --project 一改，其余全部沿用缓存里的证据。
    project_no = PROJECT_NO
    if '--project' in argv:
        project_no = argv[argv.index('--project') + 1]

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'config.%s.全流程.yaml' % project_no)
    if '--out' in argv:
        out_path = argv[argv.index('--out') + 1]

    forms, skipped, unresolved = build()
    text = render(forms, skipped, unresolved, project_no=project_no)

    # 先自检再落盘：配置解析不了的话后面全是白跑。
    try:
        import yaml
        parsed = yaml.safe_load(text)
    except ImportError:
        print('装 pyyaml 的 python 才能自检，跳过')
        parsed = None
    if parsed is not None:
        got = len((parsed.get('scope') or {}).get('forms') or [])
        assert got == len(forms), '解析回来只有 %d 个表单，写出去的是 %d' % (
            got, len(forms))
        ids = [f['form_uuid'] for f in forms]
        assert len(set(ids)) == len(ids), '有重复的 form_uuid'

    with io.open(out_path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(text)
    print('写入 %s' % out_path)
    print('  覆盖 %d 个上传点，跳过 %d 个' % (len(forms), len(skipped)))
    by_key = {}
    for f in forms:
        by_key[f['_key_label']] = by_key.get(f['_key_label'], 0) + 1
    for k, v in sorted(by_key.items(), key=lambda x: -x[1]):
        print('    关联键是「%s」的：%d 个' % (k, v))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
