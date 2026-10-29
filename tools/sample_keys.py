# -*- coding: utf-8 -*-
"""抽样看各流程的项目标识字段长什么样，确认合同号能不能直接当项目号用。

取最近若干天已审批通过的单据，把项目号字段和合同号字段的值都打印出来，
再统计有多少值长得像项目号（如 26JZBTPB06-003）。

    python tools/sample_keys.py --days 30 --per-form 15
"""

import argparse
import datetime as dt
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from yida_archive import fetch_openapi as api       # noqa: E402
from yida_archive import nightly                     # noqa: E402

PROJECT_PATTERN = re.compile(r'^\d{2}[A-Z]{2,8}[A-Z0-9]*-\d+')


def looks_like_project(value):
    return bool(PROJECT_PATTERN.match((value or '').strip()))


def main():
    parser = argparse.ArgumentParser(description='抽样核对项目标识字段')
    parser.add_argument('--days', type=int, default=30)
    parser.add_argument('--per-form', type=int, default=12)
    args = parser.parse_args()

    client = api.OpenApiClient()
    today = dt.date.today()
    start, _ = api.day_window(today - dt.timedelta(days=args.days))
    _, end = api.day_window(today)

    for form in nightly.FORMS:
        records = []
        for record in client.iter_window(
                form['form_uuid'], start, end,
                instanceStatus='COMPLETED', approvedResult='agree'):
            records.append(record)
            if len(records) >= args.per_form:
                break
        if not records:
            print()
            print('%s：近 %d 天没有已通过的单据' % (form['name'], args.days))
            continue

        project_field = form.get('project_field') or ''
        contract_field = form.get('contract_field') or ''
        print()
        print('%s  项目号字段=%s  合同号字段=%s' % (
            form['name'], project_field or '(无)', contract_field or '(无)'))

        for field_id, label in ((project_field, '项目号'), (contract_field, '合同号')):
            if not field_id:
                continue
            values = []
            for record in records:
                data = api.form_data_of(record)
                values.append(nightly.text_of(data, field_id))
            filled = [v for v in values if v]
            hit = sum(1 for v in filled if looks_like_project(v))
            print('  %s：%d/%d 条有值，其中像项目号的 %d 条' % (
                label, len(filled), len(values), hit))
            for value in filled[:6]:
                print('    %s%s' % (value, '' if looks_like_project(value) else '  <-- 不像项目号'))


if __name__ == '__main__':
    main()
