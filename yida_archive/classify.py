# -*- coding: utf-8 -*-
"""按规则给文件归类。

判定顺序（先强后弱）：
  1. 字段线索：某些表单的某些字段，上传的必定是某一类，直接确认
  2. 关键词：在文件名里找规则关键词，命中多个关键词的规则优先
  3. 人工关键词：命中就标成待人工，不自动归档
  4. 都没命中：按 unmatched_policy 处理（skip 不存储 / manual 待人工）

每个结论都带 confidence：
  confirmed  有字段线索或有把握的关键词
  suggested  只有文件名里的弱关键词，建议人工扫一眼
  manual     需要人工判断
"""

import re
import unicodedata
from .config import canonical_type, TYPE_NUMBERS

CONFIRMED = 'confirmed'
SUGGESTED = 'suggested'
MANUAL = 'manual'

# 只识别完整英文词；下划线、连字符和空格都可作分隔符。
# PO 后面允许直接接订单号，但不会命中 report / support / sample。
ENGLISH_RULES = (
    ('箱单', r'(?<![a-z])(?:packing|pascking)[\s_-]*list(?![a-z])|(?<![a-z])pl(?=[\s_-]+[a-z0-9])', CONFIRMED),
    ('运单/提单', r'(?<![a-z])(?:awb|air[\s_-]*waybill|bill[\s_-]*of[\s_-]*lading)(?![a-z])', CONFIRMED),
    ('采购合同', r'(?<![a-z])(?:purchase[\s_-]*order(?![a-z])|p\.?o\.?(?![a-z]))', SUGGESTED),
    ('发票（进口）', r'(?<![a-z])invoice(?![a-z])', SUGGESTED),
)
ENGLISH_REVIEW = re.compile(
    r'(?<![a-z])(?:sample|draft|template|specimen)(?![a-z])'
    r'|(?<![a-z])pro[\s_-]*forma(?![a-z])|样稿|样本|模板|草稿|形式发票', re.I)


def _contract_supporting_material(f):
    """合同名被引用在沟通材料中，不足以证明附件就是合同正文。"""
    for obs in f.obs:
        name = unicodedata.normalize('NFKC', obs.name)
        if '合同' not in name:
            continue
        if re.search(r'聊天|沟通|邮件|微信|往来记录|条款对比|条款说明|条款讨论', name):
            return True
        if '确认' in name and re.search(r'条款|事宜|垫付|先下单', name):
            return True
    return False


def _exclusion_reason(f, manual_kw):
    """排除证据优先，避免字段名或正文类型关键词吞掉非归档材料。"""
    if _contract_supporting_material(f):
        return '文件名指向合同条款确认或沟通材料，不能据此认定为合同正文'
    for o in f.obs:
        name = unicodedata.normalize('NFKC', o.name).casefold()
        if any(k.casefold() in name for k in manual_kw if k):
            return '旧版、草稿或作废文件不自动归档'
        if ENGLISH_REVIEW.search(name):
            return '样稿、模板或形式发票不作为正式归档文件'
        if re.search(r'聊天记录|沟通记录|邮件往来|(?:发展|同事)确认|确认.*结算[表单]', name):
            return '沟通或业务确认材料，不属于需求定义的正式文件类型'
        if re.search(r'进口增值税|海关.*(?:缴款|税单)|关税.*缴款', name):
            return '进口税费凭证不能按国内货物发票归档，现有分类未定义该类型'
    return ''


def _receipt_types(f):
    """凭证为主体、合同为引用时，按收付款方向归类。"""
    found = set()
    for o in f.obs:
        name = unicodedata.normalize('NFKC', o.name)
        if re.search(r'退款.*(?:凭证|回单|水单)', name):
            found.add('退款凭证')
        elif re.search(r'(?:支付|付款).*凭证|付款(?:回单|水单|记录)', name):
            found.add('付款凭证')
        elif re.search(r'(?:收款|收汇).*(?:凭证|回单|水单)|收款记录', name):
            found.add('收款凭证')
        elif ('信用证' in name or re.search(
                r'(?<![a-z])(?:l[./]?c|t[./]?t)(?![a-z])', name, re.I)):
            found.add('付款凭证')
        elif '水单' in name and o.form_name == '货款支付':
            found.add('付款凭证')
    return found


def _english_hits(f, rules):
    """英文名仅提供类型线索，不声称已核验正文或贸易性质。"""
    allowed = {r['type']: r for r in rules}
    hits = {}
    names = [unicodedata.normalize('NFKC', o.name).casefold() for o in f.obs]
    for type_name, pattern, confidence in ENGLISH_RULES:
        if type_name in allowed and any(re.search(pattern, name) for name in names):
            hits[type_name] = (allowed[type_name], confidence)
    return hits, bool(hits) and any(ENGLISH_REVIEW.search(name) for name in names)


def _matches(text, keyword):
    text = unicodedata.normalize('NFKC', text or '').casefold()
    keyword = unicodedata.normalize('NFKC', keyword or '').casefold()
    if not keyword:
        return False
    if re.fullmatch(r'[a-z0-9 ._-]+', keyword):
        return bool(re.search(r'(?<![a-z])' + re.escape(keyword) + r'(?![a-z])', text))
    return keyword in text


def _haystack(f):
    """参与匹配的文本：文件名 + 各字段名。"""
    parts = [o.name for o in f.obs]
    for o in f.obs:
        parts.append(o.field_label or '')
    return ' '.join(parts)


def _hit_count(text, keywords):
    return sum(1 for k in keywords if _matches(text, k))


def classify(files, cfg):
    """就地写回 folder / type_name / confidence。"""
    rules = cfg.rules
    ordinal = {}
    for i, r in enumerate(rules):
        ordinal.setdefault((r['folder'], r['type']), i + 1)

    hints = cfg.field_hints          # [{form_name, field_label, folder, type}]
    manual_kw = cfg.manual_keywords    # ['旧版', '草稿', ...]

    for f in files:
        f.folder = f.type_name = ''
        f.confidence = MANUAL
        f.review_note = ''
        text = _haystack(f)
        name = f.name or ''

        review = (cfg.raw.get('file_reviews') or {}).get(f.ident, {})
        if 'type' in review:
            chosen = [r for r in rules if r['type'] == canonical_type(review['type'])]
            if chosen:
                f.folder, f.type_name = chosen[0]['folder'], chosen[0]['type']
                f.confidence = CONFIRMED
            f.review_note = '人工指定分类' if chosen else '人工标记不归类'
            continue

        reason = _exclusion_reason(f, manual_kw)
        if reason:
            f.review_note = reason
            continue

        english, needs_review = _english_hits(f, rules)
        if needs_review:
            f.review_note = '英文附件含样稿、模板、草稿或形式发票标记，需要人工确认用途'
            continue

        receipts = _receipt_types(f)
        if receipts:
            if len(receipts) != 1:
                f.review_note = '同一文件的来源名称存在收付款方向冲突，自动跳过'
                continue
            receipt = next(iter(receipts))
            rule = next((r for r in rules if r['type'] == receipt), None)
            if rule:
                f.folder, f.type_name, f.confidence = rule['folder'], receipt, CONFIRMED
                f.review_note = '按凭证用途和收付款方向识别，合同关键词仅为引用'
                continue

        # 1. 字段线索
        hit = _match_hint(f, hints)
        if hit:
            if not any((r['folder'], r['type']) == hit for r in rules):
                f.review_note = '专用字段映射冲突或类型不在允许清单，自动跳过'
                continue
            f.folder, f.type_name, f.confidence = hit[0], hit[1], CONFIRMED
            f.review_note = '命中配置中的专用附件字段'
            continue

        # 2. 人工关键词
        if _hit_count(name, manual_kw):
            f.confidence = MANUAL
            continue

        # 3. 关键词打分
        best = None
        best_score = (0, 0, 0)
        tied = False
        direct_matches = []
        for r in rules:
            direct = [k for k in r.get('keywords') or []
                      if k and any(_matches(o.name, k) for o in f.obs)]
            hits = direct or [k for k in r.get('keywords') or []
                              if _matches(text, k)]
            if direct:
                direct_matches.append((r['type'], direct))
            score = (bool(direct), max(map(len, hits), default=0), len(hits))
            if score > best_score:
                best, best_score = r, score
                tied = False
            elif best and score == best_score and r['type'] != best['type']:
                tied = True
        # 文件名中的英文类型也优先于普通字段名；显式字段映射仍在前面处理。
        if english and not best_score[0]:
            best, tied = None, False
        if best and best_score[0]:
            winning = [keys for kind, keys in direct_matches if kind == best['type']]
            winning = [k for keys in winning for k in keys]
            if any(kind != best['type'] and any(not any(k in w for w in winning) for k in keys)
                   for kind, keys in direct_matches):
                f.review_note = '文件名称命中多个不相容类型，无法确定主体文件，自动跳过'
                continue
        if best and not tied:
            f.folder = best['folder']
            f.type_name = best['type']
            # 关键词足够长、且直接出现在文件名里，才算确认
            strong = any(_matches(o.name, k)
                         for o in f.obs for k in best.get('keywords') or [] if len(k) >= 4)
            f.confidence = CONFIRMED if strong else SUGGESTED
            f.review_note = '文件名规则匹配：' + best['type'] if best_score[0] else '附件字段规则匹配：' + best['type']
            continue

        if not best and len(english) == 1:
            rule, confidence = next(iter(english.values()))
            f.folder, f.type_name = rule['folder'], rule['type']
            f.confidence = confidence
            f.review_note = {
                '箱单': '英文文件名识别：Packing list（箱单）',
                '运单/提单': '英文文件名识别：AWB / Air waybill / Bill of lading',
                '发票（进口）': '英文文件名推定为进口发票，尚未核验正文和贸易性质',
                '采购合同': '英文文件名识别为采购订单，请核对是否为正式订单',
            }[f.type_name]
            continue

        f.confidence = MANUAL
        if tied:
            f.review_note = '多个类型匹配强度相同，需要人工选择'
        elif len(english) > 1:
            f.review_note = '英文文件名同时包含多个文件类型，需要人工选择'
        else:
            f.review_note = '不在已定义类型内，或文件名与字段缺少足够分类依据，自动跳过'

    return files


def _match_hint(f, hints):
    matches = set()
    for h in hints:
        if not any((not h.get('form_name') or h['form_name'] == o.form_name)
                   and (not h.get('field_label') or h['field_label'] == o.field_label)
                   for o in f.obs):
            continue
        matches.add((h.get('folder'), canonical_type(h.get('type'))))
    return next(iter(matches)) if len(matches) == 1 else (('', '') if matches else None)


def type_ordinal(cfg):
    """类型序号：规则表里的次序，用来拼文件名。"""
    return {(r['folder'], r['type']): TYPE_NUMBERS[r['type']] for r in cfg.rules}


def mark_secret(files, cfg):
    for f in files:
        f.secret = cfg.project_secret is not False
        if cfg.project_secret is None:
            f.review_note += '；项目保密标记未确认，预览按加密区显示，禁止正式保存'
    return files


def describe(files, cfg):
    """给报告用的汇总数字。"""
    kept = [f for f in files if f.folder and f.confidence != MANUAL and not f.superseded]
    manual = [f for f in files if not f.folder]
    return {
        'total_uploads': sum(f.upload_count for f in files),
        'logical_files': len(files),
        'classified': len(kept),
        'manual': len(manual),
        'skipped': len(manual) if cfg.unmatched_policy == 'skip' else 0,
        'secret': len([f for f in kept if f.secret]),
        'verified': sum(bool(f.content_hash) for f in files),
        'unverified': sum(not f.content_hash for f in files),
        'superseded': sum(f.superseded for f in files),
        'ai_classified': sum(f.ai_result.get('status') == 'classified' for f in files),
        'ai_failed': sum(f.ai_result.get('status') == 'failed' for f in files),
        'rule_fallback': sum(f.ai_result.get('status') == 'rule_fallback' for f in files),
        'ai_cached': sum(bool(f.ai_result.get('cached')) for f in files),
    }
