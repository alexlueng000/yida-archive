# -*- coding: utf-8 -*-
"""配置加载。所有业务参数都从配置文件来，程序里不硬编码。"""

import io
import os

try:
    import yaml
except ImportError:                     # 依赖没装时给一句人话，别甩 traceback
    raise SystemExit(
        '缺少依赖 PyYAML。先执行：pip install -r requirements.txt')

DEFAULT_RULES = [
    ('合同', '中标合同（内贸）', ['中标合同', '内贸合同', '内贸补充合同']),
    ('合同', '外贸报关合同', ['外贸合同', '四方外贸', '外贸报关', '外贸补充合同']),
    ('合同', '采购合同', ['采购合同', '两方合同', '二方合同', '产品合同']),
    ('合同', '协议', ['协议']),
    ('合同', '承诺函/书', ['承诺函', '承诺书']),

    ('办证', '彩页说明书', ['彩页', '说明书']),
    ('办证', '减免税进口货物说明表',
     ['减免税进口货物说明', '减免税进口仪器说明', '进口货物说明表']),
    ('办证', '征免税确认通知书', ['征免税确认', '征免税通知']),
    ('办证', '税款担保通知书', ['税款担保']),
    ('办证', '机电证', ['机电证']),
    ('办证', '3C证书', ['3C证书', 'CCC证书']),
    ('办证', '最终用户声明', ['最终用户声明']),
    ('办证', '中检证', ['中检证', '中检证书']),

    ('清关送货', '发票（进口）', ['进口发票', '发票(进口)']),
    ('清关送货', '箱单', ['箱单', '装箱单']),
    ('清关送货', '运单/提单', ['运单', '提单']),
    ('清关送货', '司机资料/舱单回执', ['舱单', '司机资料']),
    ('清关送货', '进口符合性声明', ['符合性声明']),
    ('清关送货', '报关单', ['报关单']),
    ('清关送货', '发货单/送货单/货物签收单', ['发货单', '送货单', '签收单', '签收']),

    ('收付款', '收款凭证', ['收款凭证', '收款记录', '收款回单']),
    ('收付款', '离岸账户证明', ['离岸账户证明']),
    ('收付款', '付款凭证', ['付款凭证', '付款记录', '付款回单', '付款水单', '信用证', 'LC', 'TT']),

    ('结算移交', '发票（国内货物类）',
     ['进项票', '进项发票', '增值税发票', '国内货物']),
    ('结算移交', '发票（国内服务类）', ['国内服务', '服务类发票', '服务费发票']),
    ('结算移交', '结算表', ['结算表', '结算单']),
    ('结算移交', '退款凭证', ['退款凭证']),
    ('结算移交', '移交资料收件回执', ['移交资料', '收件回执', '移交回执']),
]

DEFAULT_FOLDERS = ['合同', '办证', '清关送货', '收付款', '结算移交']

# 序号来自完整业务字典，不能从采购单位的过滤结果或上传顺序推导。
TYPE_NUMBERS = {kind: i for i, (_, kind, _) in enumerate(DEFAULT_RULES, 1)}
TYPE_ALIASES = {'发货单/送货单': '发货单/送货单/货物签收单',
                '货物签收单': '发货单/送货单/货物签收单'}
TYPE_LABELS = {kind: kind for kind in TYPE_NUMBERS}
TYPE_LABELS.update({kind: kind + '（关务部）' for kind in ('彩页说明书', '减免税进口货物说明表')})
TYPE_LABELS.update({kind: kind + '（财务部）' for kind in ('收款凭证', '付款凭证', '退款凭证')})
TYPE_ALIASES.update({label: kind for kind, label in TYPE_LABELS.items()})


def canonical_type(value):
    return TYPE_ALIASES.get(value, value)

DEFAULT_ENCRYPTED = ['收款凭证', '付款凭证', '退款凭证', '结算表',
                     '发票（国内货物类）', '发票（国内服务类）']


def clean_cookie(raw: str) -> str:
    """把从浏览器抄来的 cookie 串收拾干净。

    手工粘贴常见的三种情况，都当正常输入接受：
      - 连 `cookie:` 这个头名一起复制了
      - 从 DevTools 复制时带了换行（cookie 头本身不允许有换行）
      - 首尾有多余空白
    """
    if not raw:
        return ''
    s = raw.strip()
    if s[:7].lower() == 'cookie:':
        s = s[7:]
    return ' '.join(s.split())


class Config:
    def __init__(self, raw: dict = None):
        self.raw = raw or {}

    # ---- 宜搭连接 ----
    @property
    def base_url(self) -> str:
        return (self.raw.get('yida') or {}).get(
            'base_url', 'https://www.aliwork.com').rstrip('/')

    @property
    def access_token(self) -> str:
        env = (self.raw.get('yida') or {}).get('access_token_env',
                                               'YIDA_ACCESS_TOKEN')
        return os.environ.get(env, '')

    @property
    def csrf_token(self) -> str:
        env = (self.raw.get('yida') or {}).get('csrf_token_env',
                                               'YIDA_CSRF_TOKEN')
        return os.environ.get(env, '')

    @property
    def cookie(self) -> str:
        """从浏览器 F12 抄出来的整条 Cookie 头。

        实测（2026-09-23）本机浏览器发往 front.aliwork.com 的请求里
        **没有 Authorization 头**，靠的是 cookie。附件地址和页面同域，
        所以这条路能下到文件。
        """
        env = (self.raw.get('yida') or {}).get('cookie_env', 'YIDA_COOKIE')
        return clean_cookie(os.environ.get(env, ''))

    @property
    def app_type(self) -> str:
        return (self.raw.get('yida') or {}).get('app_type', '')

    @property
    def referer(self) -> str:
        """请求头 Referer。

        实测（2026-09-24）：这个接口前面的 WAF **拒收没有 Referer 的请求**，
        直接返回 HTTP 200 + 空响应——不报错、不跳转，只有 0 字节。
        带着 cookie 和 csrf 也一样，少这一个头就什么都取不到，
        非常难查。值本身无所谓（实测连 https://example.com/ 都能过），
        但头必须在。

        默认拿应用工作台地址，配置里 yida.referer 可以覆盖。
        """
        raw = (self.raw.get('yida') or {}).get('referer')
        if raw:
            return raw
        if self.app_type:
            return '%s/%s/workbench' % (self.base_url, self.app_type)
        return self.base_url + '/'

    @property
    def auth_mode(self) -> str:
        """token=直连接口（凭证取 token 或 cookie 环境变量）；
        cli=复用 openyida 的登录态取数。"""
        return (self.raw.get('yida') or {}).get('auth_mode', 'token')

    @property
    def timeout(self) -> int:
        return int((self.raw.get('yida') or {}).get('timeout', 60))

    @property
    def page_size(self) -> int:
        return int((self.raw.get('yida') or {}).get('page_size', 100))

    # ---- 业务范围 ----
    @property
    def project_no(self) -> str:
        return (self.raw.get('scope') or {}).get('project_no', '')

    @property
    def purchaser(self) -> str:
        return (self.raw.get('scope') or {}).get('purchaser', '未指定采购单位')

    @property
    def forms(self) -> list:
        return (self.raw.get('scope') or {}).get('forms') or []

    @property
    def project_secret(self):
        value = (self.raw.get('scope') or {}).get('project_secret')
        if value is None or value == '':
            return None
        if value is True or value == '是':
            return True
        if value is False or value == '否':
            return False
        raise ValueError('是否南科大企业项目必须为是或否')

    def validate_save(self):
        if not self.project_no.strip() or not self.purchaser.strip():
            raise ValueError('正式保存需要项目号和采购单位')
        if self.project_secret is None:
            raise ValueError('请先确认合同审批的「是否南科大企业项目」，缺失时不能正式保存')

    # ---- 分类 ----
    @property
    def folders(self) -> list:
        return ((self.raw.get('classification') or {}).get('folders')
                or DEFAULT_FOLDERS)

    @property
    def rules(self) -> list:
        profile = (self.raw.get('purchasers') or {}).get(self.purchaser, {})
        classification = profile.get('classification') or {}
        raw = classification.get('rules') or (self.raw.get('classification') or {}).get('rules')
        if not raw:
            raw = [{'folder': f, 'type': t, 'keywords': k} for f, t, k in DEFAULT_RULES]
        allowed = classification.get('allowed_types')
        allowed = None if allowed is None else {canonical_type(t) for t in allowed}
        result = []
        for r in raw:
            kind = canonical_type(r['type'])
            if kind not in TYPE_NUMBERS:
                raise ValueError('文件类型未在1～28号字典中定义：' + kind)
            folder = DEFAULT_RULES[TYPE_NUMBERS[kind] - 1][0]
            if r['folder'] != folder:
                raise ValueError('文件类型与业务阶段不匹配：' + kind)
            if allowed is None or kind in allowed:
                result.append(dict(r, type=kind))
        return result

    @property
    def field_hints(self) -> list:
        return (self.raw.get('classification') or {}).get('field_hints') or []

    @property
    def manual_keywords(self) -> list:
        return list(dict.fromkeys(['旧版', '草稿', '作废'] +
                    ((self.raw.get('classification') or {}).get('manual_keywords') or [])))

    @property
    def encrypted_types(self) -> list:
        raw = (self.raw.get('classification') or {}).get('encrypted_types')
        return raw if raw is not None else DEFAULT_ENCRYPTED

    @property
    def unmatched_policy(self) -> str:
        """unmatched 的文件怎么处理：skip 不存储 / manual 待人工。"""
        return (self.raw.get('classification') or {}).get(
            'unmatched_policy', 'manual')

    # ---- 命名 ----
    def _naming(self, key, default):
        profile = (self.raw.get('purchasers') or {}).get(self.purchaser, {})
        return (profile.get('naming') or {}).get(key, (self.raw.get('naming') or {}).get(key, default))

    def type_label(self, type_name):
        labels = dict(TYPE_LABELS)
        labels.update(self._naming('type_labels', {}))
        return labels.get(type_name, type_name)

    @property
    def pattern(self) -> str:
        return self._naming('pattern', '{seq}-{type}-{project}{ext}')

    @property
    def multi_pattern(self) -> str:
        return self._naming('multi_pattern',
                            '{seq}-{sub}-{type}-{project}{ext}')

    # ---- 存储 ----
    @property
    def out_dir(self) -> str:
        return (self.raw.get('storage') or {}).get('out_dir', './archive')

    @property
    def public_root(self) -> str:
        return (self.raw.get('storage') or {}).get('public_root', '公共')

    @property
    def encrypted_root(self) -> str:
        return (self.raw.get('storage') or {}).get('encrypted_root', '加密')

    @property
    def structure(self) -> str:
        return (self.raw.get('storage') or {}).get(
            'structure', '{purchaser}/{project}/{root}/{folder}')

    @property
    def work_dir(self) -> str:
        return (self.raw.get('storage') or {}).get('work_dir', './.work')


def load(path: str) -> Config:
    if not path:
        return Config({})
    with io.open(path, encoding='utf-8') as fh:
        raw = yaml.safe_load(fh) or {}
    ai = raw.get('ai') or {}
    if ai.get('credentials_file'):
        ai['credentials_file'] = os.path.abspath(os.path.join(
            os.path.dirname(os.path.abspath(path)), ai['credentials_file']))
    return Config(raw)
