# -*- coding: utf-8 -*-
"""Reclassify only ledger-tracked pending files. Stop all archive writers first."""
import argparse
import hashlib
import os
import shutil
import sys
import tempfile
import json
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from web import server as web
from yida_archive import ai_classify, classify, dedupe, naming, numbering, zip_members
from yida_archive.config import load
from yida_archive.fetch_openapi import load_env


def result_snapshot(files):
    """Compare persisted business results, ignoring local cache paths and cache-hit flags."""
    rows = []
    for f in files:
        row = asdict(f)
        row.pop('cache_path', None)
        row['ai_result'].pop('cached', None)
        rows.append(row)
    return json.dumps(sorted(rows, key=lambda row: row['ident']), ensure_ascii=False, sort_keys=True)


def inherit_archive_owner(staged, archive_root):
    """Root-run maintenance must leave private files readable by the archive service."""
    if not hasattr(os, 'chown'):
        return
    owner = archive_root.stat()
    for directory, dirs, names in os.walk(staged, followlinks=False):
        for path in [Path(directory)] + [Path(directory) / name for name in names]:
            if path.is_symlink():
                raise ValueError('暂存归档中不允许符号链接')
            current = path.stat()
            if (current.st_uid, current.st_gid) != (owner.st_uid, owner.st_gid):
                os.chown(path, owner.st_uid, owner.st_gid)
        if any((Path(directory) / name).is_symlink() for name in dirs):
            raise ValueError('暂存归档中不允许符号链接')


def run_project(ledger, write):
    import json
    data = json.loads(ledger.read_text(encoding='utf-8'))
    purchaser, project = data['purchaser'], data['project']
    web.STATE.update(project_no=project, purchaser=purchaser, file_reviews={}, batches=[], blobs={})
    cfg = web._config()
    if numbering.project_dir(cfg).resolve() != ledger.parent.resolve():
        raise ValueError('账本与项目路径不一致')
    files = numbering.include_history([], cfg, deduplicate=False)
    before = result_snapshot(files)
    zip_members.expand(files, cfg, lambda f: Path(f.cache_path).read_bytes(), log=print)
    from yida_archive.document_dedupe import merge
    original_count = len(files)
    files = merge(files, cfg.raw.get('file_reviews'))
    merged_count = original_count - len(files)
    pending = [f for f in files if not f.type_name and 'type' not in cfg.raw['file_reviews'].get(f.ident, {})]
    if not pending and not merged_count:
        return 0
    for f in pending:
        if hashlib.sha256(Path(f.cache_path).read_bytes()).hexdigest() != f.content_hash:
            raise ValueError('文件内容与账本哈希不一致：' + f.name)
    classify.classify(pending, cfg)
    ai_classify.apply(pending, cfg, lambda f: Path(f.cache_path).read_bytes(), log=print)
    changed = [f for f in pending if f.type_name and f.folder and f.confidence != classify.MANUAL]
    print(f'项目 {purchaser} / {project}：待归类 {len(pending)}，可归类 {len(changed)}', flush=True)
    if merged_count:
        print(f'  DOCX内容相同，合并重复副本 {merged_count} 份；上传来源全部保留', flush=True)
    if before == result_snapshot(files):
        print('  分类结果未变化，跳过保存和备份', flush=True)
        return 0
    if not changed and not merged_count and not write:
        return 0
    classify.mark_secret(changed, cfg)
    naming.assign(files, cfg, project)
    for f in changed:
        print(f'  {f.name} -> {f.type_name} / {f.new_name}', flush=True)
    if not write:
        return len(changed) + merged_count
    root = Path(web.OUTPUT_ROOT).resolve()
    target = ledger.parent.resolve()
    # Stage through the same writer used by the web application; preserve a complete backup.
    with tempfile.TemporaryDirectory(prefix='.reclassify-', dir=root) as stage:
        result = web._save_to(stage, prepared=(cfg, files, dedupe.findings_for(files)))
        if not result.get('ok'):
            raise RuntimeError('保存失败，原目录未改动：' + str(result.get('failed') or result))
        fresh = Path(stage) / target.relative_to(root)
        # 修复属主后才替换线上目录；失败时原目录保持原样。
        inherit_archive_owner(fresh, root)
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
    return len(changed) + merged_count


def main():
    parser = argparse.ArgumentParser(description='重新归类账本中未归类的文件；默认预览')
    parser.add_argument('--write', action='store_true', help='保存结果；默认只预览')
    parser.add_argument('--project', help='只处理指定项目号')
    parser.add_argument('--config', default=str(ROOT / 'config.yaml'))
    parser.add_argument('--workers', type=int, choices=range(1, 9), help='AI并发数，默认3，最大8')
    args = parser.parse_args()
    load_env()
    web.SERVER_CONFIG = load(args.config).raw
    if args.workers is not None:
        web.SERVER_CONFIG.setdefault('ai', {})['workers'] = args.workers
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
