"""Pull one project's complete approved history across registered forms."""
import argparse
import datetime as dt
import json
import re

from . import nightly, pipeline
from .config import load


def project_key(value):
    if not isinstance(value, str):
        raise ValueError('请输入项目号')
    value = nightly.normalize_key(value.strip())
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_().-]{1,79}', value):
        raise ValueError('项目号格式无效，请输入完整项目号')
    return value


def scan(client, key, forms=None):
    rows, seen = [], set()
    forms = nightly.FORMS if forms is None else forms
    start = dt.datetime(1970, 1, 1, tzinfo=nightly.api.CST)
    end = dt.datetime.now(nightly.api.CST) + dt.timedelta(seconds=1)
    for n, form in enumerate(forms, 1):
        field = nightly.key_field_of(form)
        if not field:
            print('流程 %d/%d：%s 未配置项目关联字段，无法自动关联' % (n, len(forms), form['name']), flush=True)
            continue
        # 搜索只作候选筛选，返回后归一化精确核对；前缀兼容尾号括号写法。
        query = key.split('-')[0]
        found = []
        for record in client.iter_window(form['form_uuid'], start, end,
                instanceStatus='COMPLETED', approvedResult='agree',
                searchFieldJson=json.dumps({field: query}, ensure_ascii=False)):
            if not nightly.is_finished(record):
                continue
            inst = nightly.build_instance(form, record)
            identity = (inst.form_uuid, inst.instance_id)
            if inst.project_key != key or identity in seen:
                continue
            seen.add(identity)
            # 修改时间不冒充最终审批时间；完整历史拉取不依赖日期过滤。
            inst.approved_at = 0
            for obs in inst.attachments:
                obs.approved_at = 0
            found.append(inst)
        rows.extend(found)
        print('流程 %d/%d：%s，单据 %d，附件 %d' %
              (n, len(forms), form['name'], len(found), sum(len(i.attachments) for i in found)), flush=True)
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--config', default='config.yaml')
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args(argv)
    key = project_key(args.project)
    with nightly.execution_lock():
        client = nightly.api.OpenApiClient()
        rows = scan(client, key)
        if not rows:
            print('未找到项目 %s 的已审批通过单据，请核对项目号及流程字段配置' % key, flush=True)
            return 1
        meta = nightly.project_meta(client, [key], refresh=True).get(key) or {}
        cfg = nightly.project_config(load(args.config).raw, key, meta)
        downloader = nightly.Downloader(client)
        if args.write:
            count, root = nightly.save_originals(rows, key, dt.datetime.now(nightly.api.CST).date(), downloader)
            print('原始附件 %d 份：%s' % (count, root), flush=True)
        result = pipeline.run(cfg, rows, client=downloader, dry_run=not args.write, verbose=True)
        print('归类结果：' + json.dumps(result.summary, ensure_ascii=False), flush=True)
        return 1 if result.failed or any(f.download_error for f in result.files) else 0


if __name__ == '__main__':
    raise SystemExit(main())
