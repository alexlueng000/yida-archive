# -*- coding: utf-8 -*-
"""入口。

三种用法：

1) 只要每次上传了哪些文件（不做去重归类）：
    python run.py --config config.yaml --online --files           # 只出清单
    python run.py --config config.yaml --online --files --write   # 顺便下载

2) 完整去重归类：
    python run.py --config config.yaml --online --dump-snapshot snapshots/now.json --dry-run
    python run.py --config config.yaml --online --write

3) 不连网先用快照验证：
    python run.py --config config.yaml --snapshot fixtures/B22026LD001.json --dry-run

--dry-run 只出清单和报告，不下载不写盘；--write 才会真下载落盘。
"""

import argparse
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _fix_console():
    """Windows 控制台默认是 GBK，中文会显示成乱码。切到 UTF-8。

    只影响屏幕显示；写出来的 CSV/Markdown 本来就是 UTF-8，不受影响。
    """
    if os.name != 'nt':
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for name in ('stdout', 'stderr'):
        stream = getattr(sys, name, None)
        if stream is not None and hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(encoding='utf-8')
            except Exception:
                pass


_fix_console()

from yida_archive import by_instance
from yida_archive import config as cfg_mod
from yida_archive import discover, fetch_cli, fetch_json, fetch_yida, pipeline


def _parse_args(argv=None):
    p = argparse.ArgumentParser(
        description='合同审批附件自动去重归类')
    p.add_argument('--config', required=True, help='配置文件路径')
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument('--snapshot', help='本地快照 JSON，离线演练用')
    src.add_argument('--online', action='store_true', help='连宜搭取数')
    src.add_argument('--local-dir', action='append', help='已下载的每次上传目录，可重复指定；按实际内容去重')
    p.add_argument('--output', help='归类结果输出目录')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', default=True,
                      help='只出方案，不下载不落盘（默认）')
    mode.add_argument('--write', action='store_true',
                      help='真下载并落盘')
    p.add_argument('--no-hash', action='store_true',
                   help='跳过下载算哈希（离线无 token 时用）')
    p.add_argument('--dump-snapshot', metavar='PATH',
                   help='把线上取到的数据存成快照，便于以后离线复跑')
    p.add_argument('--prefix', default='archive',
                   help='输出文件名前缀，默认 archive')
    p.add_argument('--discover', action='store_true',
                   help='只列出各表单里实际出现的附件字段 id，不做归类')
    p.add_argument('--files', action='store_true',
                   help='只按「每次上传」列出文件，不去重、不归类')
    p.add_argument('--survey', action='store_true',
                   help='盘点每个项目有多少单据和附件，用来挑归档样本；'
                        '不需要配 scope.project_no')
    p.add_argument('--survey-limit', type=int, default=0, metavar='N',
                   help='盘点最多列 N 行，0（默认）表示全列')
    p.add_argument('--survey-md', metavar='PATH',
                   help='把推荐名单存成 Markdown 文档')
    p.add_argument('--survey-json', metavar='PATH',
                   help='把盘点原始统计存成 JSON，方便以后换口径重算')
    p.add_argument('--survey-top', type=int, default=20, metavar='N',
                   help='Markdown 里对照表列前 N 名，默认 20')
    p.add_argument('--quiet', action='store_true')
    return p.parse_args(argv)


def _files_mode(cfg, instances, client, args):
    """按每次上传列文件。不去重、不归类。"""
    prefix = args.prefix if args.prefix != 'archive' else 'files'
    planned = by_instance.plan(instances, cfg)

    total = sum(len(rows) for _, rows in planned)
    print('')
    print('共 %d 次上传，合计 %d 个附件' % (len(planned), total))
    for i, (inst, rows) in enumerate(planned, start=1):
        print('  %2d. %-38s %2d 个  %s'
              % (i, (inst.title or inst.form_name)[:38], len(rows),
                 # 必须带年份：只打 %m-%d 的话跨年区间看着像倒序
                 # （01-19 后面跟 04-17，其实后者是下一年的）。
                 time.strftime('%Y-%m-%d %H:%M',
                               time.localtime(inst.submitted_at / 1000.0))
                 if inst.submitted_at else ''))

    failed = []
    if args.write:
        if client is None:
            print('下载需要连线上取数，快照模式只能出清单。')
            return 2
        print('')
        print('下载中...')
        done, failed = by_instance.write(
            planned, client,
            log=None if args.quiet else (lambda m: print('  ' + m)))
        print('下载成功 %d，失败 %d' % (len(done), len(failed)))

    os.makedirs(os.path.abspath(cfg.out_dir), exist_ok=True)
    csv_path = by_instance.write_csv(
        planned, cfg, os.path.join(cfg.out_dir, prefix + '-每次上传.csv'))
    md_path = by_instance.write_markdown(
        planned, cfg, os.path.join(cfg.out_dir, prefix + '-每次上传.md'),
        failed=failed)
    print('')
    print('清单：%s' % csv_path)
    print('清单：%s' % md_path)
    if args.write:
        print('文件目录：%s'
              % os.path.join(os.path.abspath(cfg.out_dir), '每次上传'))
    return 0


def _make_client(cfg):
    """建取数客户端。返回 (client, 该捕获的异常类型)。"""
    if cfg.auth_mode == 'cli':
        return (fetch_cli.CliClient(cfg, app_type=cfg.app_type),
                (fetch_cli.CliError,))
    return (fetch_yida.YidaClient(cfg, app_type=cfg.app_type),
            (fetch_yida.YidaError,))


def _survey_mode(cfg, args):
    """盘点全部项目。不预设项目号，所以不受 scope.project_no 约束。"""
    from yida_archive import survey
    if not cfg.app_type:
        print('配置里没写 yida.app_type，无法取数。')
        return 2
    if args.snapshot or args.local_dir:
        print('--survey 要连线上取数，快照里只有一个项目的数据，盘不了。')
        return 2

    client, errors = _make_client(cfg)
    print('取数方式：%s' % ('复用 openyida 登录态' if cfg.auth_mode == 'cli'
                            else '直连宜搭接口'))
    print('不按项目过滤，每个表单都要翻全，单据多会比较慢...')
    try:
        stats = survey.collect(
            client, cfg,
            on_form=lambda n, c: print('  已读 %s：%d 条' % (n, c)))
    except errors as exc:
        print('盘点失败：%s' % exc)
        return 3
    print('')
    print(survey.render(stats, cfg, limit=args.survey_limit))

    if args.survey_json:
        path = os.path.abspath(args.survey_json)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        survey.write_json(stats, cfg, path, app_type=cfg.app_type)
        print('')
        print('原始统计：%s' % path)
    if args.survey_md:
        path = os.path.abspath(args.survey_md)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(survey.render_markdown(
                stats, cfg, top=args.survey_top, app_type=cfg.app_type))
        print('推荐名单：%s' % path)
    return 0


def main(argv=None):
    args = _parse_args(argv)
    cfg = cfg_mod.load(args.config)
    if args.output:
        cfg.raw.setdefault('storage', {})['out_dir'] = args.output
    if not cfg.project_no and not args.survey:
        print('配置里没写 scope.project_no，无法确定要归档哪个项目。')
        return 2
    if args.survey:
        return _survey_mode(cfg, args)
    if args.survey_md or args.survey_json:
        print('--survey-md / --survey-json 要跟 --survey 一起用。')
        return 2

    verbose = not args.quiet
    client = None

    if args.local_dir:
        from yida_archive import fetch_local
        instances = [inst for directory in args.local_dir for inst in fetch_local.load(directory)]
        client = fetch_local.LocalClient(args.local_dir)
        do_hash = True
        print('本地附件：%d 个目录，%d 条上传记录' % (len(args.local_dir), len(instances)))
    elif args.snapshot:
        if not os.path.exists(args.snapshot):
            print('快照文件不存在：%s' % args.snapshot)
            return 2
        instances = fetch_json.load(args.snapshot)
        print('离线快照：%s' % args.snapshot)
        if verbose:
            meta = fetch_json.snapshot_meta(args.snapshot)
            if meta.get('generated_at'):
                print('快照生成时间：%s' % meta['generated_at'])
        do_hash = False
    else:
        if not cfg.app_type:
            print('配置里没写 yida.app_type，无法取数。')
            return 2
        client, errors = _make_client(cfg)
        print('取数方式：%s' % ('复用 openyida 登录态' if cfg.auth_mode == 'cli'
                                else '直连宜搭接口'))
        instances = []
        failed_forms = []
        for form_cfg in cfg.forms:
            try:
                got = list(client.iter_instances(form_cfg))
            except errors as exc:
                # 一个表单取不到不该拖垮整轮。全流程配置有几十个表单，
                # 里面混着关联键对不上的，那种会报错而不是返回 0 条。
                failed_forms.append((form_cfg.get('name'), str(exc)))
                print('  %-14s 取数失败：%s'
                      % (form_cfg.get('name', ''), exc))
                continue
            except Exception as exc:      # noqa: BLE001 - 见下
                # 兜住解析类的意外：某条记录的时间戳不是数字、附件字段形态
                # 没见过、CLI 输出格式变了，等等。这类异常以前会一路冒到
                # main()，把剩下几十个表单的活儿全带走（2026-09-28 用户
                # 在 33 个表单的配置上就这么崩的，重跑又是好的）。
                # 宁可少一个表单并在末尾列出来，也不要整轮白跑。
                failed_forms.append((form_cfg.get('name'),
                                     '%s: %s' % (type(exc).__name__, exc)))
                print('  %-14s 取数出错，已跳过：%s: %s'
                      % (form_cfg.get('name', ''), type(exc).__name__, exc))
                continue
            hits = sum(len(i.attachments) for i in got)
            print('  %-14s 单据 %d，附件 %d'
                  % (form_cfg.get('name', ''), len(got), hits))
            instances.extend(got)
        if failed_forms:
            print('')
            print('有 %d 个表单没取到（其余照常处理）：' % len(failed_forms))
            for name, why in failed_forms:
                print('  · %s：%s' % (name, why))
            if not instances:
                print('所有表单都失败了，没什么可处理的。')
                return 3
        if args.dump_snapshot:
            os.makedirs(os.path.dirname(os.path.abspath(args.dump_snapshot)),
                        exist_ok=True)
            fetch_json.dump(instances, cfg.project_no, cfg.purchaser,
                            args.dump_snapshot)
            print('已存快照：%s' % args.dump_snapshot)
        do_hash = not args.no_hash

    if args.discover:
        print('')
        print(discover.render(instances))
        return 0

    if args.files:
        return _files_mode(cfg, instances, client, args)

    res = pipeline.run(cfg, instances, client=client,
                       dry_run=not args.write, do_hash=do_hash,
                       verbose=verbose, out_prefix=args.prefix)

    if verbose and res.findings:
        if res.findings.version_conflicts:
            print('注意：%d 个文件名对应不同文件；版本关系未确认前分别保留。'
                  % len(res.findings.version_conflicts))
        if res.findings.rename_candidates:
            print('注意：%d 组文件大小相同但名字不同，请看报告确认是否同一份。'
                  % len(res.findings.rename_candidates))
    return 4 if res.failed else 0


if __name__ == '__main__':
    sys.exit(main())
