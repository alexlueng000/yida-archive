# -*- coding: utf-8 -*-
"""不需要联网、不需要 token 的单元测试。

跑法： python -m pytest tests -q
或者： python tests/test_pipeline.py
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from yida_archive import (by_instance, classify, dedupe, extract, fetch_cli,
                          naming, survey)
from yida_archive.config import Config
from yida_archive import models
from yida_archive.models import AttachmentObs, Instance


def obs(name, size, uuid, form='合同审批', ts=1000, field='附件'):
    return AttachmentObs(
        form_name=form, form_uuid='F1', instance_id='I1',
        instance_title='t', submitted_at=ts, field_id='a1',
        field_label=field, name=name, size=size, file_uuid=uuid,
        url='http://x/' + uuid)


def make(attachments):
    inst = Instance(form_name='合同审批', form_uuid='F1', instance_id='I1',
                    title='t', submitted_at=1000)
    inst.attachments = attachments
    return inst


class TestDedupe(unittest.TestCase):
    def test_same_uuid_merges(self):
        files, _ = dedupe.group([make([
            obs('a.pdf', 10, 'u1', ts=1),
            obs('a.pdf', 10, 'u1', ts=2),
        ])])
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].upload_count, 2)

    def test_reupload_same_name_size_requires_hash(self):
        """不同UUID且未验证内容，即使同名同大小也不能合并。"""
        files, _ = dedupe.group([make([
            obs('a.pdf', 10, 'u1', ts=1),
            obs('a.pdf', 10, 'u2', ts=2),
        ])])
        self.assertEqual(len(files), 2)

    def test_same_name_diff_size_not_merged(self):
        files, findings = dedupe.group([make([
            obs('a.pdf', 10, 'u1', ts=1),
            obs('a.pdf', 20, 'u2', ts=2),
        ])])
        self.assertEqual(len(files), 2)
        self.assertEqual(findings.version_conflicts['a.pdf'], [10, 20])

    def test_diff_name_same_size_flagged(self):
        _, findings = dedupe.group([make([
            obs('a.pdf', 10, 'u1', ts=1),
            obs('b.pdf', 10, 'u2', ts=2),
        ])])
        self.assertEqual(findings.rename_candidates[10], ['a.pdf', 'b.pdf'])

    def test_refine_merges_by_hash(self):
        files, _ = dedupe.group([make([
            obs('a.pdf', 10, 'u1', ts=1),
            obs('b.pdf', 10, 'u2', ts=2),
        ])])
        for f in files:
            f.content_hash = 'HASH'
        files = dedupe.refine(files)
        self.assertEqual(len(files), 1)


class TestCliClient(unittest.TestCase):
    """CLI 取数路径：解析和错误提示。"""

    def test_recover_json_behind_logs(self):
        txt = '\x1b[36m[1] 读取登录态\x1b[0m\nok\n{"content": {"data": [1, 2]}}\n'
        d = fetch_cli._recover_json(fetch_cli.ANSI.sub('', txt))
        self.assertEqual(d['content']['data'], [1, 2])

    def test_recover_json_returns_empty_on_garbage(self):
        self.assertEqual(fetch_cli._recover_json('完全不是 JSON'), {})

    def test_gmtcreate_shapes_do_not_raise(self):
        """时间戳什么样的值都得能过。

        int(row['gmtCreate']) 裸转，遇到非数字就抛 ValueError，而取数循环
        只兜 CliError，于是一个表单的异常把整轮几十个表单带走。
        """
        self.assertEqual(models.as_millis(1713484800000), 1713484800000)
        self.assertEqual(models.as_millis('1713484800000'), 1713484800000)
        self.assertEqual(models.as_millis('1713484800000.0'), 1713484800000)
        for bad in (None, '', '2024-04-19', '  ', [], {}, True, False, 'abc'):
            self.assertEqual(models.as_millis(bad), 0,
                             'as_millis(%r) 该返回 0' % (bad,))

    def test_iter_instances_survives_a_weird_timestamp(self):
        """整条链路：记录里时间戳是格式化的字符串，也要能出单据。"""
        cfg = Config({})
        client = fetch_cli.CliClient(cfg, app_type='APP_X')
        rows = [{'formData': {}, 'processInstanceId': 'I1', 'title': 'T',
                 'gmtCreate': '2024-04-19 16:19', 'instanceStatus': 'COMPLETED'}]
        client.iter_records = lambda form_cfg, project_no=None: iter(rows)
        got = list(client.iter_instances({'name': 'X', 'form_uuid': 'F1'}))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].submitted_at, 0)

    def test_download_without_token_says_what_to_do(self):
        cfg = Config({})
        client = fetch_cli.CliClient(cfg, app_type='APP_X')
        with self.assertRaises(fetch_cli.CliError) as ctx:
            client.download('http://x/a.pdf', 'a.pdf')
        self.assertIn('YIDA_ACCESS_TOKEN', str(ctx.exception))

    def test_person_name_shapes(self):
        self.assertEqual(fetch_cli._person(None), '')
        self.assertEqual(fetch_cli._person({'name': '张三'}), '张三')
        self.assertEqual(fetch_cli._person({'name': {'zh_CN': '李四'}}), '李四')


class TestByInstance(unittest.TestCase):
    """按每次上传列文件：不去重、不归类，跨单据重复要各算各的。"""

    def _instances(self):
        a = Instance(form_name='合同审批', form_uuid='F1', instance_id='I1',
                     title='A项目合同审批', submitted_at=2000)
        a.attachments = [obs('合同.pdf', 10, 'u1', ts=2000),
                         obs('发票.pdf', 20, 'u2', ts=2000)]
        b = Instance(form_name='货款支付', form_uuid='F2', instance_id='I2',
                     title='A项目货款支付', submitted_at=1000)
        b.attachments = [obs('合同.pdf', 10, 'u1', form='货款支付', ts=1000)]
        return [a, b]

    def test_sorted_by_time(self):
        g = by_instance.groups(self._instances())
        self.assertEqual([i.instance_id for i in g], ['I2', 'I1'])

    def test_empty_instance_dropped(self):
        c = Instance(form_name='货款支付', form_uuid='F2', instance_id='I3',
                     title='没附件', submitted_at=3000)
        g = by_instance.groups(self._instances() + [c])
        self.assertEqual(len(g), 2)

    def test_duplicates_kept_per_submission(self):
        """跨单据重复的文件，在两个单据下各出现一次。"""
        cfg = Config({'scope': {'project_no': 'P1', 'project_secret': False}, 'storage': {'out_dir': './o'}})
        planned = by_instance.plan(self._instances(), cfg)
        names = [[a.name for a, _ in rows] for _, rows in planned]
        self.assertEqual(names, [['合同.pdf'], ['合同.pdf', '发票.pdf']])

    def test_paths_never_collide(self):
        cfg = Config({'scope': {'project_no': 'P1', 'project_secret': False}, 'storage': {'out_dir': './o'}})
        planned = by_instance.plan(self._instances(), cfg)
        paths = [p.lower() for _, rows in planned for _, p in rows]
        self.assertEqual(len(paths), len(set(paths)))

    def test_same_name_twice_in_one_submission(self):
        """同一单据里传了两个同名文件，不能互相覆盖。"""
        a = Instance(form_name='合同审批', form_uuid='F1', instance_id='I1',
                     title='T', submitted_at=2000)
        a.attachments = [obs('x.pdf', 10, 'u1', ts=2000),
                         obs('x.pdf', 20, 'u2', ts=2000)]
        cfg = Config({'scope': {'project_no': 'P1', 'project_secret': False}, 'storage': {'out_dir': './o'}})
        planned = by_instance.plan([a], cfg)
        paths = [p for _, p in planned[0][1]]
        self.assertEqual(len(paths), 2)
        self.assertNotEqual(paths[0].lower(), paths[1].lower())
        self.assertEqual(os.path.basename(os.path.dirname(os.path.dirname(paths[0]))), '每次上传')

    def test_folder_name_has_index_time_and_count(self):
        inst = self._instances()[0]
        name = by_instance.folder_name(inst, 3)
        self.assertTrue(name.startswith('03-'))
        self.assertIn('合同审批', name)
        self.assertIn('-2件-', name)
        self.assertTrue(name.endswith('A项目合同审批'))

    def test_same_title_same_time_differ_by_count(self):
        """标题和时间都相同、只有件数不同的两条，目录名必须能分开。

        实测 24JZBTPFB157-401 的「合同审批」有 7 次提交，标题全是同一个
        模板生成的「袁剑峰发起的24JZBTPFB157-401合同审批」，其中一条 3 个
        附件、一条 29 个。用户就是把这两条当成同一条，去比对线上的附件，
        以为本地漏下了 26 个。件数是最后一根能靠的差别。
        """
        small = Instance(form_name='合同审批', form_uuid='F1', instance_id='I1',
                         title='袁剑峰发起的X合同审批', submitted_at=1000)
        small.attachments = [obs('a.pdf', 10, 'u1')]
        big = Instance(form_name='合同审批', form_uuid='F1', instance_id='I2',
                       title='袁剑峰发起的X合同审批', submitted_at=1000)
        big.attachments = [obs('a.pdf', 10, 'u1'), obs('b.pdf', 20, 'u2'),
                           obs('c.pdf', 30, 'u3')]
        n1 = by_instance.folder_name(small, 1)
        n2 = by_instance.folder_name(big, 1)
        self.assertNotEqual(n1, n2)
        self.assertIn('-1件-', n1)
        self.assertIn('-3件-', n2)

    def _tmp_cfg(self):
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out, True)
        return Config({'scope': {'project_no': 'P1', 'project_secret': False},
                       'storage': {'out_dir': out}})

    def test_write_stamps_dates_to_submission_time(self):
        """落盘后文件时间要是提交时间，不是下载时间。

        资源管理器「修改日期」那一列显示的就是文件自己的 mtime，也就是
        下载那一刻。26 个单据目录全是同一天，用户点那一列表头想按时间找
        某一次提交，等于没排——他问「你不能按照提交的时间排序吗」问的
        就是这个。所以下完必须盖成提交时间。
        """
        ts = 1600000000000            # 2020-09-13 20:26:40 UTC
        a = Instance(form_name='合同审批', form_uuid='F1', instance_id='I1',
                     title='T', submitted_at=ts)
        a.attachments = [obs('x.pdf', 3, 'u1', ts=ts)]
        planned = by_instance.plan([a], self._tmp_cfg())
        done, failed = by_instance.write(planned, _FakeDownloader())
        self.assertEqual(failed, [])
        self.assertEqual(len(done), 1)

        dest = planned[0][1][0][1]
        want = ts / 1000.0
        for label, path in (('文件', dest),
                            ('单据目录', os.path.dirname(dest))):
            self.assertAlmostEqual(os.path.getmtime(path), want, delta=1,
                                   msg='%s 的时间不是提交时间' % label)

    def test_no_submission_time_leaves_the_clock_alone(self):
        """没有提交时间的单据宁可留着下载时间，也不要盖成 1970。"""
        path = os.path.join(tempfile.mkdtemp(), 'x.pdf')
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)
        with open(path, 'wb') as fh:
            fh.write(b'x')
        before = os.path.getmtime(path)
        self.assertFalse(by_instance.stamp_time(path, 0))
        self.assertAlmostEqual(os.path.getmtime(path), before, delta=1)


class TestExtract(unittest.TestCase):
    """附件值有两种形态，两种都要认出来。"""

    def test_json_string_form(self):
        fd = {'attachmentField_a': json.dumps(
            [{'name': 'x.pdf', 'size': 1, 'fileUuid': 'u1'}])}
        self.assertEqual(len(extract.find_attachments(fd)), 1)

    def test_parsed_list_form(self):
        """有的接口直接给解析好的数组，早期版本会一个都认不出来。"""
        fd = {'attachmentField_a': [
            {'name': 'x.pdf', 'size': 1, 'fileUuid': 'u1'},
            {'name': 'y.pdf', 'size': 2, 'fileUuid': 'u2'}]}
        self.assertEqual(len(extract.find_attachments(fd)), 2)

    def test_nested_subtable(self):
        fd = {'tableField_t': [
            {'attachmentField_b': json.dumps(
                [{'name': 'z.pdf', 'size': 3, 'fileUuid': 'u3'}])}]}
        found = extract.find_attachments(fd)
        self.assertEqual(len(found), 1)
        self.assertEqual(extract.field_id_of(found[0][0]), 'attachmentField_b')

    def test_no_double_count(self):
        """解析好的数组不能既算数组元素、又算整个数组。"""
        fd = {'a': [{'name': 'x', 'size': 1, 'fileUuid': 'u1'}]}
        self.assertEqual(len(extract.find_attachments(fd)), 1)

    def test_value_suffix_stripped(self):
        """真实数据里少数字段键带 _value 后缀，字段 id 要能对上。"""
        self.assertEqual(extract.field_id_of('a.b[0].textField_x_value'),
                         'textField_x')
        self.assertEqual(extract.field_id_of('attachmentField_y'),
                         'attachmentField_y')

    def test_matches_project_with_value_suffix(self):
        self.assertTrue(extract.matches_project(
            {'textField_p_value': 'B22026LD001'}, 'textField_p',
            'B22026LD001'))
        self.assertFalse(extract.matches_project(
            {'textField_p': 'B22026LD002'}, 'textField_p', 'B22026LD001'))


class TestClassify(unittest.TestCase):
    def test_keyword_match(self):
        cfg = Config({})
        files, _ = dedupe.group([make([obs('四方外贸合同.docx', 5, 'u1')])])
        classify.classify(files, cfg)
        self.assertEqual(files[0].type_name, '外贸报关合同')
        self.assertEqual(files[0].folder, '合同')
        self.assertEqual(files[0].confidence, 'confirmed')

    def test_unknown_is_manual(self):
        cfg = Config({})
        files, _ = dedupe.group([make([obs('随手拍.png', 5, 'u1')])])
        classify.classify(files, cfg)
        self.assertEqual(files[0].confidence, 'manual')
        self.assertEqual(files[0].folder, '')

    def test_field_hint_wins(self):
        cfg = Config({'classification': {'field_hints': [
            {'form_name': '合同审批', 'field_label': '附件',
             'folder': '合同', 'type': '协议'}]}})
        files, _ = dedupe.group([make([obs('随手拍.png', 5, 'u1')])])
        classify.classify(files, cfg)
        self.assertEqual(files[0].type_name, '协议')

    def test_encrypted_flag(self):
        cfg = Config({})
        files, _ = dedupe.group([make([obs('付款记录.png', 5, 'u1',
                                           form='货款支付')])])
        classify.classify(files, cfg)
        classify.mark_secret(files, cfg)
        self.assertTrue(files[0].secret)


class TestNaming(unittest.TestCase):
    def _run(self, names, cfg=None):
        cfg = cfg or Config({})
        atts = [obs(n, s, 'u%d' % i, ts=i)
                for i, (n, s) in enumerate(names)]
        files, _ = dedupe.group([make(atts)])
        classify.classify(files, cfg)
        classify.mark_secret(files, cfg)
        naming.assign(files, cfg, 'B22026LD001')
        return sorted(f.new_name for f in files if f.new_name)

    def test_single_file_no_sub(self):
        out = self._run([('四方外贸合同.docx', 5)])
        self.assertEqual(out, ['2-外贸报关合同-B22026LD001.docx'])

    def test_multi_gets_sub(self):
        out = self._run([('四方外贸合同.docx', 5), ('四方外贸合同.docx', 6)])
        self.assertEqual(out, ['2-1-外贸报关合同-B22026LD001.docx',
                               '2-2-外贸报关合同-B22026LD001.docx'])

    def test_distinct_names_get_incrementing_sub(self):
        out = self._run([('两方合同（进口）a.doc', 5),
                         ('两方合同（进口）b.doc', 6)])
        self.assertEqual(out, ['3-1-采购合同-B22026LD001.doc',
                               '3-2-采购合同-B22026LD001.doc'])

    def test_illegal_chars_sanitized(self):
        self.assertEqual(naming.sanitize('a/b:c*d?.pdf'), 'a_b_c_d_.pdf')
        self.assertEqual(naming.sanitize('   '), '未命名')

    def test_target_dir_layout(self):
        cfg = Config({'scope': {'project_no': 'B22026LD001'},
                      'storage': {'structure': '{root}/{purchaser}/{project}/{folder}'}})
        files, _ = dedupe.group([make([obs('付款记录.png', 5, 'u1',
                                           form='货款支付')])])
        classify.classify(files, cfg)
        classify.mark_secret(files, cfg)
        naming.assign(files, cfg, 'B22026LD001')
        self.assertEqual(naming.target_dir(files[0], cfg),
                         '加密/未指定采购单位/B22026LD001/收付款')


class _FakeDownloader:
    """写一个非空文件就算下载成功。"""

    def __init__(self, content=b'x'):
        self.content = content
        self.calls = []

    def download(self, url, dest):
        self.calls.append(url)
        with open(dest, 'wb') as fh:
            fh.write(self.content)


class _FakeClient:
    """只记下被怎么调用、按表单吐预置行的假取数端。"""

    def __init__(self, rows_by_form):
        self.rows_by_form = rows_by_form
        self.calls = []

    def iter_records(self, form_cfg, project_no=None):
        self.calls.append((form_cfg.get('name'), project_no))
        for row in self.rows_by_form.get(form_cfg.get('name'), []):
            yield row


def _row(field, project, atts=(), status='COMPLETED', ts=0, key='formData'):
    """造一条取数结果。project=None 表示这个字段没填。"""
    fd = {}
    if project is not None:
        fd[field] = project
    if atts:
        fd['attachmentField_x'] = [
            {'name': n, 'fileUuid': u, 'size': s} for n, s, u in atts]
    return {key: fd, 'instanceStatus': status, 'gmtCreate': ts}


class TestSurvey(unittest.TestCase):
    """盘点：不预设项目号，把每个项目有多少单据多少附件数出来。"""

    A = [('a.pdf', 10, 'u1'), ('b.pdf', 20, 'u2'), ('c.pdf', 30, 'u3'),
         ('d.pdf', 40, 'u4'), ('e.pdf', 50, 'u5')]

    def _cfg(self):
        return Config({'scope': {'project_no': 'P1', 'forms': [
            {'name': '合同审批', 'form_uuid': 'F-A',
             'project_field': 'textField_a'},
            {'name': '货款支付', 'form_uuid': 'F-B',
             'project_field': 'textField_b'},
        ]}})

    def _client(self, pay_status='COMPLETED'):
        return _FakeClient({
            '合同审批': [
                _row('textField_a', 'P1', self.A[:2], ts=2000),
                _row('textField_a', 'P1', self.A[2:3], ts=1000),
                _row('textField_a', 'P2', self.A, status='RUNNING', ts=3000),
                _row('textField_a', None, []),
            ],
            '货款支付': [
                _row('textField_b', 'P1', self.A[:3], ts=4000),
                # 这条只有 data 键、没有 formData，实测接口就是这样给的
                _row('textField_b', 'P3', self.A[:4], ts=5000, key='data'),
            ],
        })

    def test_aggregates_by_project_and_counts_attachments(self):
        stats = survey.collect(self._client(), self._cfg())
        p1 = stats['P1']
        self.assertEqual(p1['instances'], 3)
        self.assertEqual(p1['attachments'], 6)
        self.assertEqual(p1['forms'], {'合同审批': 2, '货款支付': 1})
        self.assertEqual(p1['statuses'], {'COMPLETED': 3})

    def test_each_form_is_read_without_a_project_filter(self):
        client = self._client()
        survey.collect(client, self._cfg())
        self.assertEqual(client.calls,
                         [('合同审批', ''), ('货款支付', '')])

    def test_data_key_records_are_counted_too(self):
        stats = survey.collect(self._client(), self._cfg())
        self.assertEqual(stats['P3']['attachments'], 4)

    def test_blank_project_goes_into_its_own_bucket(self):
        stats = survey.collect(self._client(), self._cfg())
        self.assertEqual(stats['(空)']['instances'], 1)
        self.assertEqual(stats['(空)']['attachments'], 0)

    def test_time_range_is_the_span_of_its_records(self):
        stats = survey.collect(self._client(), self._cfg())
        self.assertEqual(stats['P1']['first'], 1000)
        self.assertEqual(stats['P1']['last'], 4000)

    def test_project_value_may_be_a_dict(self):
        self.assertEqual(survey._project_of({'f': {'value': 'P9'}}, 'f'), 'P9')
        self.assertEqual(survey._project_of({'f': '  P9  '}, 'f'), 'P9')
        self.assertEqual(survey._project_of({}, 'f'), '(空)')

    def test_on_form_reports_how_many_were_read(self):
        seen = []
        survey.collect(self._client(), self._cfg(),
                       on_form=lambda n, c: seen.append((n, c)))
        self.assertEqual(seen, [('合同审批', 4), ('货款支付', 2)])

    def test_only_projects_that_finished_every_flow_are_recommended(self):
        cfg = self._cfg()
        stats = survey.collect(self._client(), cfg)
        self.assertEqual([p for p, _ in survey.finished(stats, cfg)], ['P1'])

    def test_a_project_still_running_is_not_recommended(self):
        cfg = self._cfg()
        stats = survey.collect(self._client(pay_status='RUNNING'), cfg)
        # P1 两个流程都走过，但有一条还在审批中
        stats['P1']['statuses'] = {'COMPLETED': 2, 'RUNNING': 1}
        self.assertEqual(survey.finished(stats, cfg), [])

    def test_render_orders_by_attachments_and_names_the_best_one(self):
        cfg = self._cfg()
        text = survey.render(survey.collect(self._client(), cfg), cfg)
        body = [ln.split()[0] for ln in text.splitlines()
                if ln.startswith('P')]
        self.assertEqual(body, ['P1', 'P2', 'P3'])
        rec = text.split('全流程走完的')[1]
        self.assertIn('P1', rec)
        self.assertNotIn('P2', rec)     # 只走了一个流程
        self.assertIn('已完成', text)

    def test_render_says_so_when_nothing_finished(self):
        cfg = self._cfg()
        stats = survey.collect(self._client(), cfg)
        del stats['P1']
        self.assertIn('没有哪个项目号同时满足', survey.render(stats, cfg))

    def test_render_explains_empty_result(self):
        text = survey.render({}, self._cfg())
        self.assertIn('一个项目号都没读到', text)
        self.assertIn('project_field', text)

    def test_markdown_lists_the_recommended_projects_with_a_flow_breakdown(self):
        cfg = self._cfg()
        md = survey.render_markdown(survey.collect(self._client(), cfg), cfg)
        self.assertTrue(md.startswith('# '))
        # 推荐表按流程拆开列，能直接看出「都走过」是走过哪几条
        self.assertIn('| # | 项目号 | 合同审批 | 货款支付 |', md)
        self.assertIn('| 1 | `P1` | 2 | 1 | 3 | 6 |', md)
        # 只有 P1 够格
        self.assertIn('共 1 个', md)

    def test_markdown_says_the_top_table_is_not_the_recommendation(self):
        cfg = self._cfg()
        md = survey.render_markdown(survey.collect(self._client(), cfg), cfg)
        self.assertIn('这一节不等于推荐名单', md)
        # P2 附件位（5）比 P3（4）多，但两个都不推荐
        self.assertIn('| `P2` | 1 | 5 | 1/2 |', md)
        self.assertIn('| 否 |', md)

    def test_markdown_states_its_own_limits(self):
        cfg = self._cfg()
        md = survey.render_markdown(survey.collect(self._client(), cfg), cfg)
        self.assertIn('盘点只覆盖配置里写了的表单', md)
        self.assertIn('不是文件数', md)
        self.assertIn('已终止', md)

    def test_markdown_can_be_capped(self):
        cfg = self._cfg()
        stats = survey.collect(self._client(), cfg)
        md = survey.render_markdown(stats, cfg, top=2)
        self.assertIn('附件最多的 2 个项目', md)
        self.assertNotIn('| `P3` |', md)

    def test_json_roundtrips_the_stats(self):
        cfg = self._cfg()
        stats = survey.collect(self._client(), cfg)
        blob = survey.to_json(stats, cfg, app_type='APP_X')
        self.assertEqual(blob['app_type'], 'APP_X')
        self.assertEqual(blob['forms'], ['合同审批', '货款支付'])
        self.assertEqual(blob['instance_count'], 6)
        self.assertEqual(blob['attachment_count'], 15)
        # 存下来再读回去，数字不能变（不然离线重算就没意义）
        back = json.loads(json.dumps(blob, ensure_ascii=False))
        self.assertEqual(back['projects']['P1']['attachments'], 6)

    def test_timestamps_carry_the_year(self):
        """只显示月日的话，跨年的区间看起来像倒着走。"""
        oct_2025 = 1761408000000
        jul_2026 = 1784000000000
        a, b = survey._fmt_ts(oct_2025), survey._fmt_ts(jul_2026)
        self.assertLess(a[:2], b[:2])        # 年份在前，跨年也排得对
        self.assertEqual(len(a), 8)
        self.assertEqual(survey._fmt_ts(0), '--')


if __name__ == '__main__':
    unittest.main(verbosity=2)
