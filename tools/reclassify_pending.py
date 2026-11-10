# -*- coding: utf-8 -*-
"""Reclassify only ledger-tracked pending files. Stop all archive writers first."""
import argparse
import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from web import server as web
from yida_archive import ai_classify, classify, dedupe, naming, numbering, zip_members
from yida_archive.config import load
from yida_archive.fetch_openapi import load_env


def run_project(ledger, write):
    import json
    data = json.loads(ledger.read_text(encoding='utf-8'))
    purchaser, project = data['purchaser'], data['project']
    web.STATE.update(project_no=project, purchaser=purchaser, file_reviews={}, batches=[], blobs={})
    cfg = web._config()
    if numbering.project_dir(cfg).resolve() != ledger.parent.resolve():
        raise ValueError('账本与项目路径不一致')
    files = numbering.include_history([], cfg)
    zip_members.expand(files, cfg, lambda f: Path(f.cache_path).read_bytes(), log=print)
    pending = [f for f in files if not f.type_name and 'type' not in cfg.raw['file_reviews'].get(f.ident, {})]
    if not pending:
        return 0
    for f in pending:
        if hashlib.sha256(Path(f.cache_path).read_bytes()).hexdigest() != f.content_hash:
            raise ValueError('文件内容与账本哈希不一致：' + f.name)
    classify.classify(pending, cfg)
    ai_classify.apply(pending, cfg, lambda f: Path(f.cache_path).read_bytes(), log=print)
    changed = [f for f in pending if f.type_name and f.folder and f.confidence != classify.MANUAL]
    print(f'项目 {purchaser} / {project}：待归类 {len(pending)}，可归类 {len(changed)}', flush=True)
    if not changed and not write:
        return 0
    classify.mark_secret(changed, cfg)
    naming.assign(files, cfg, project)
    for f in changed:
        print(f'  {f.name} -> {f.type_name} / {f.new_name}', flush=True)
    if not write:
        return len(changed)
    root = Path(web.OUTPUT_ROOT).resolve()
    target = ledger.parent.resolve()
    # Stage through the same writer used by the web application; preserve a complete backup.
    with tempfile.TemporaryDirectory(prefix='.reclassify-', dir=root) as stage:
        result = web._save_to(stage, prepared=(cfg, files, dedupe.findings_for(files)))
        if not result.get('ok'):
            raise RuntimeError('保存失败，原目录未改动：' + str(result.get('failed') or result))
        fresh = Path(stage) / target.relative_to(root)
        backup_root = root / '.reclassify-backups'
        backup_root.mkdir(exist_ok=True)
        import uuid
        backup = backup_root / (uuid.uuid4().hex + '-' + target.name)
        os.replace(target, backup)
        try:
            os.replace(fresh, target)
        except BaseException:
            os.replace(backup, target)
            raise
        print('  备份：' + str(backup), flush=True)
    return len(changed)


def main():
    parser = argparse.ArgumentParser(description='重新归类账本中未归类的文件；默认预览')
    parser.add_argument('--write', action='store_true', help='保存结果；默认只预览')
    parser.add_argument('--project', help='只处理指定项目号')
    parser.add_argument('--config', default=str(ROOT / 'config.yaml'))
    args = parser.parse_args()
    load_env()
    web.SERVER_CONFIG = load(args.config).raw
    root = Path(web.OUTPUT_ROOT).resolve()
    if not root.is_dir():
        raise SystemExit('找不到归档结果目录：' + str(root))
    if not (web.SERVER_CONFIG.get('ai') or {}).get('enabled'):
        raise SystemExit('配置未启用 AI，停止运行；请检查服务器配置')
    count = errors = projects = 0
    for ledger in sorted(root.glob('*/*/编号记录.json')):
        if ledger.parent.parent.name.startswith('.'):
            continue
        if ledger.is_symlink() or ledger.parent.is_symlink() or ledger.parent.parent.is_symlink():
            print('跳过链接：' + str(ledger), file=sys.stderr)
            errors += 1
            continue
        if args.project and ledger.parent.name != args.project:
            continue
        projects += 1
        try:
            count += run_project(ledger, args.write)
        except Exception as exc:
            errors += 1
            print(f'失败 {ledger}: {exc}', file=sys.stderr, flush=True)
    print(f'处理项目 {projects}，可归类文件 {count}，失败项目 {errors}；' + ('已保存' if args.write else '预览，未修改归档'))
    return 1 if errors or (args.project and not projects) else 0


if __name__ == '__main__':
    raise SystemExit(main())
