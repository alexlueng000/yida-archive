"""离线审查现有附件的自动分类；只写审查清单，不写归档文件。"""
import argparse
import csv
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from yida_archive import classify, dedupe, fetch_local
from yida_archive.config import Config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--before')
    args = parser.parse_args()
    before = json.loads(Path(args.before).read_text(encoding='utf-8')) if args.before else []
    old = {(r['project'], r['hash']): r for r in before}
    rows = []
    for project, source in [('B22026LD001', 'archive-pay'),
                            ('24JZBTPFB157-401', 'archive-24JZBTPFB157-401-全流程')]:
        instances = fetch_local.load(ROOT / source / '每次上传')
        files = dedupe.refine(dedupe.group(instances)[0])
        classify.classify(files, Config())
        classify.mark_secret(files, Config())
        for f in files:
            previous = old.get((project, f.content_hash), {})
            rows.append(dict(project=project, hash=f.content_hash, name=f.name,
                             aliases=' / '.join(sorted({o.name for o in f.obs})),
                             type=f.type_name, folder=f.folder, secret=f.secret,
                             action='归档' if f.folder else '跳过', reason=f.review_note,
                             before=previous.get('type', ''),
                             changed=bool(previous) and previous['type'] != f.type_name))
        print(project, '出现', sum(len(i.attachments) for i in instances), '去重后', len(files),
              '归档', sum(bool(f.folder) for f in files), '跳过', sum(not f.folder for f in files))
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix('.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    with out.with_suffix('.csv').open('w', encoding='utf-8-sig', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    for row in rows:
        if row['changed']:
            print(row['before'] or '跳过', '->', row['type'] or '跳过', '|', row['name'])


if __name__ == '__main__':
    main()
