# -*- coding: utf-8 -*-
"""宜搭开放平台接口，走企业内部应用凭证，供夜间无人值守任务使用。

与 fetch_yida.py 的分工：
  fetch_yida.py  网页接口 + 浏览器登录态，适合有人值守的临时拉取，登录态会过期
  本模块          api.dingtalk.com + AppKey/AppSecret，token 两小时自动续期

凭证来源优先级：环境变量 > .env 文件。任何日志、异常、报告都不打印凭证原文，
核对凭证是否抄错用 mask()。

接口清单（都用 systemToken 指定读哪个宜搭应用）：
  POST /v1.0/oauth2/accessToken             AppKey/AppSecret 换 access_token
  POST /v1.0/yida/processes/instances       按时间、状态、审批结果筛流程实例
  GET  /v1.0/yida/apps/temporaryUrls/{app}  附件换临时免登地址，地址会过期
"""

import datetime as dt
import json
import os
import time

import requests

from . import extract

API_ROOT = 'https://api.dingtalk.com'
TOKEN_PATH = '/v1.0/oauth2/accessToken'
INSTANCES_PATH = '/v1.0/yida/processes/instances'
OPERATION_PATH = '/v1.0/yida/processes/operationRecords'
TEMP_URL_PATH = '/v1.0/yida/apps/temporaryUrls/{app_type}'

# 资源 ID 取自宜搭「应用设置-部署运维」，可用环境变量覆盖。
DEFAULT_APP_TYPE = 'APP_R55Z1QDKMB0VILUQRNJA'
DEFAULT_CORP_ID = 'ding92dcc27e50e36d1a'

# 同一个值在不同人手里叫法不同，全部认，谁先有值用谁。
ALIASES = {
    'app_key': ('YIDA_APP_KEY', 'APPKEY', 'APP_KEY'),
    'app_secret': ('YIDA_APP_SECRET', 'APPSECRET', 'APP_SECRET'),
    'system_token': ('YIDA_SYSTEM_TOKEN', 'SYSTEMTOKEN', 'SYSTEM_TOKEN'),
    'user_id': ('YIDA_USER_ID', 'USERID', 'USER_ID'),
    'app_type': ('YIDA_APP_TYPE', 'APPTYPE', 'APP_TYPE'),
}

REQUIRED = ('app_key', 'app_secret', 'system_token', 'user_id')

# 北京时间。国内没有夏令时，固定 +8 就够，不依赖服务器时区。
CST = dt.timezone(dt.timedelta(hours=8))
UTC = dt.timezone.utc

# 这个接口不接受分页：pageNumber/pageSize 写了也被忽略，每次固定回 10 条。
# 好在 totalCount 是准的，所以按总数二分时间窗，切到 10 条以内一次取回。
PAGE_LIMIT = 10

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------- 凭证 ----------

def _env_paths():
    """按优先级列出存在的 .env 文件。YIDA_ENV_FILE 用于临时指向别的文件。"""
    candidates = (
        os.environ.get('YIDA_ENV_FILE'),
        os.path.join(PROJECT_ROOT, '.env'),
        os.path.join(PACKAGE_DIR, '.env'),
    )
    return [p for p in candidates if p and os.path.isfile(p)]


def read_env_file(path):
    """解析 KEY=VALUE。只认最基本的写法，不做变量展开，避免把值搅坏。"""
    values = {}
    with open(path, encoding='utf-8-sig') as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[len('export '):].lstrip()
            if '=' not in line:
                continue
            key, value = line.split('=', 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
                value = value[1:-1]
            if key:
                values[key] = value
    return values


def load_env():
    """把 .env 补进环境变量。已有的环境变量优先，方便临时覆盖。返回读到的文件。"""
    used = []
    for path in _env_paths():
        used.append(path)
        for key, value in read_env_file(path).items():
            os.environ.setdefault(key, value)
    return used


def credentials():
    """返回 (值字典, 缺失项列表)。缺项只报名字，不报值。"""
    load_env()
    values = {}
    for name, keys in ALIASES.items():
        found = ''
        for key in keys:
            candidate = os.environ.get(key, '').strip()
            if candidate:
                found = candidate
                break
        values[name] = found
    values['app_type'] = values['app_type'] or DEFAULT_APP_TYPE
    missing = [name for name in REQUIRED if not values[name]]
    return values, missing


def mask(value):
    """日志里显示凭证只露头尾，够核对抄得对不对，又不足以还原。"""
    if not value:
        return '(空)'
    if len(value) <= 6:
        return '*' * len(value)
    return value[:2] + '*' * (len(value) - 4) + value[-2:]


# ---------- 错误 ----------

class OpenApiError(RuntimeError):
    """开放平台报错。带上是哪一步、HTTP 状态、平台错误码和原文消息。"""

    def __init__(self, step, status=None, code='', message='', payload=None):
        self.step = step
        self.status = status
        self.code = code or ''
        self.message = message or ''
        self.payload = payload
        parts = [step]
        if status is not None:
            parts.append('HTTP %s' % status)
        if self.code:
            parts.append('错误码 %s' % self.code)
        text = '，'.join(parts)
        if self.message:
            text += '：' + self.message
        super().__init__(text)


def _json_or_none(resp):
    try:
        return resp.json()
    except ValueError:
        return None


def _code(payload):
    if isinstance(payload, dict):
        for key in ('code', 'errcode', 'errorCode'):
            value = payload.get(key)
            if value not in (None, '', 0, '0'):
                return str(value)
    return ''


def _message(payload, resp=None):
    if isinstance(payload, dict):
        for key in ('message', 'errmsg', 'errorMsg'):
            if payload.get(key):
                return str(payload[key])
    if resp is not None:
        return (resp.text or '')[:300]
    return ''


# ---------- token ----------

_TOKEN = {'value': '', 'expires_at': 0.0}


def access_token(force=False):
    """进程内缓存 access_token，默认提前 5 分钟换，避免正好在边界上失败。"""
    now = time.time()
    if not force and _TOKEN['value'] and now < _TOKEN['expires_at']:
        return _TOKEN['value']

    values, missing = credentials()
    if missing:
        raise OpenApiError('换 token', code='credentials_missing',
                           message='缺少凭证：' + '、'.join(missing))
    body = {'appKey': values['app_key'], 'appSecret': values['app_secret']}
    try:
        resp = requests.post(API_ROOT + TOKEN_PATH, json=body, timeout=20)
    except requests.RequestException as exc:
        raise OpenApiError('换 token', code=type(exc).__name__,
                           message='连不上 api.dingtalk.com：%s' % exc)
    payload = _json_or_none(resp) or {}
    token = payload.get('accessToken') or ''
    if resp.status_code != 200 or not token:
        raise OpenApiError('换 token', resp.status_code, _code(payload),
                           _message(payload, resp), payload)
    expires = int(payload.get('expireIn') or 7200)
    _TOKEN['value'] = token
    _TOKEN['expires_at'] = now + max(60, expires - 300)
    return token


# ---------- 客户端 ----------

class OpenApiClient:
    """只管发请求、解错误信封；业务语义交给调用方。

    宜搭各接口的返回信封不统一（content.data / result.data / 直接 data 都有），
    这里不假设形状，统一用 find_records() 取记录。
    """

    def __init__(self, app_type='', system_token='', user_id='', timeout=30, session=None):
        values, missing = credentials()
        if missing:
            raise OpenApiError('读凭证', code='credentials_missing',
                               message='缺少凭证：' + '、'.join(missing))
        self.app_type = app_type or values['app_type']
        self.system_token = system_token or values['system_token']
        self.user_id = user_id or values['user_id']
        self.timeout = timeout
        self.session = session or requests.Session()

    def call(self, path, body=None, need_token=True, method='POST', params=None):
        headers = {'Content-Type': 'application/json'}
        if need_token:
            headers['x-acs-dingtalk-access-token'] = access_token()
        try:
            resp = self.session.request(
                method, API_ROOT + path, params=params,
                json=body if method != 'GET' else None,
                headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise OpenApiError('请求 ' + path, code=type(exc).__name__, message=str(exc))
        payload = _json_or_none(resp)
        code = _code(payload)
        if resp.status_code >= 400 or code:
            raise OpenApiError('请求 ' + path, resp.status_code,
                               code or 'http_%d' % resp.status_code,
                               _message(payload, resp), payload)
        if payload is None:
            return {'raw_text': (resp.text or '')[:300]}
        return payload

    def process_instances(self, form_uuid, page=1, size=10, **filters):
        """筛流程实例。filters 直接用平台字段名，如 instanceStatus、createFrom。"""
        body = {
            'appType': self.app_type,
            'systemToken': self.system_token,
            'userId': self.user_id,
            'formUuid': form_uuid,
            'pageNumber': page,
            'pageSize': size,
            'language': 'zh_CN',
        }
        body.update({k: v for k, v in filters.items() if v not in (None, '')})
        return self.call(INSTANCES_PATH, body)

    def operation_records(self, process_instance_id):
        """一条流程的操作记录（提交、同意、抄送…），用来取真实审批时间。"""
        params = {
            'appType': self.app_type,
            'systemToken': self.system_token,
            'userId': self.user_id,
            'language': 'zh_CN',
            'processInstanceId': process_instance_id,
        }
        payload = self.call(OPERATION_PATH, method='GET', params=params)
        records = payload.get('result') if isinstance(payload, dict) else None
        return records if isinstance(records, list) else []

    def temporary_url(self, file_url, timeout_ms=None):
        """附件换临时免登地址。

        file_url 是附件项里的 url（front.aliwork.com/ossFileHandle?...），不是 fileUuid；
        接口是 GET + query 参数，按 POST + body 调会 404。
        响应形如 {"result": "<地址>"}，默认有效期两小时。
        """
        params = {
            'systemToken': self.system_token,
            'userId': self.user_id,
            'language': 'zh_CN',
            'fileUrl': file_url or '',
        }
        if timeout_ms:
            params['timeout'] = str(int(timeout_ms))
        payload = self.call(TEMP_URL_PATH.format(app_type=self.app_type),
                            method='GET', params=params)
        result = payload.get('result') if isinstance(payload, dict) else None
        if not result:
            raise OpenApiError('换临时地址', code='empty_result',
                               message='响应里没有 result 字段', payload=payload)
        return result

    # ---------- 按时间窗取全量 ----------

    def _window_body(self, form_uuid, start, end, filters):
        body = {
            'appType': self.app_type,
            'systemToken': self.system_token,
            'userId': self.user_id,
            'formUuid': form_uuid,
            'language': 'zh_CN',
            'modifiedFromTimeGMT': utc_text(start),
            'modifiedToTimeGMT': utc_text(end),
            'pageNumber': 1,
        }
        body.update({k: v for k, v in filters.items() if v not in (None, '')})
        return body

    def count_window(self, form_uuid, start, end, **filters):
        """窗口内有多少条。totalCount 可信，用来判断要不要继续切。"""
        payload = self.call(INSTANCES_PATH, self._window_body(form_uuid, start, end, filters))
        try:
            total = int(payload.get('totalCount') or 0)
        except (TypeError, ValueError):
            total = 0
        return total, payload

    def iter_window(self, form_uuid, start, end, min_seconds=60, seen=None, **filters):
        """把窗口内的实例全部取回，按时间二分，保证不漏也不重。

        接口每次最多回 10 条且不认分页，所以总数超过 10 就把窗口对半切，
        切到不足 10 条再取。窗口细到一分钟还超 10 条时直接报错，让人知道
        这一分钟需要手工处理，而不是悄悄少拿。
        """
        seen = set() if seen is None else seen
        total, payload = self.count_window(form_uuid, start, end, **filters)
        if total == 0:
            return
        records = find_records(payload)
        if total <= PAGE_LIMIT and len(records) >= total:
            for record in records:
                key = instance_id_of(record) or id(record)
                if key in seen:
                    continue
                seen.add(key)
                yield record
            return
        if (end - start).total_seconds() <= min_seconds:
            raise OpenApiError(
                '取实例', code='window_too_dense',
                message='%s 到 %s 内还有 %d 条，超过单次 %d 条上限，需人工处理'
                        % (utc_text(start), utc_text(end), total, PAGE_LIMIT))
        mid = start + (end - start) / 2
        yield from self.iter_window(form_uuid, start, mid, min_seconds, seen, **filters)
        yield from self.iter_window(form_uuid, mid, end, min_seconds, seen, **filters)


# ---------- 时间 ----------

def utc_text(moment):
    """接口的 *TimeGMT 参数只认不带时区的 yyyy-MM-dd HH:mm:ss。

    名字里带 GMT，实际按北京时间比较（见 parse_gmt 的说明），所以这里按北京
    时间格式化，**不要**先换算成 UTC，否则整个时间窗会提前 8 小时。
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=CST)
    return moment.astimezone(CST).strftime('%Y-%m-%d %H:%M:%S')


def day_window(day, tz=CST):
    """北京时间的某一天 00:00 到次日 00:00。"""
    start = dt.datetime.combine(day, dt.time(0, 0), tz)
    return start, start + dt.timedelta(days=1)


def parse_gmt(text):
    """把记录里的 '2026-10-28T12:49Z' 转成毫秒时间戳，转不了返回 0。

    **这个值名义上是 GMT，实测是北京时间**（2026-10-29 10:06 +08:00 时，
    接口里最新一条事件的 modifiedTimeGMT 是 09:57Z；按真 UTC 解释会跑到未来）。
    按 UTC 解析的话，页面上所有时间都会比真实时间晚 8 小时。
    """
    if not text:
        return 0
    try:
        moment = dt.datetime.fromisoformat(str(text).replace('Z', ''))
    except ValueError:
        return 0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=CST)
    return int(moment.timestamp() * 1000)


def final_agree_time(records):
    """审批记录里最后一条「同意」的时间，毫秒；没有就返回 0。

    modifiedTimeGMT 是「最后一次修改」，审批通过之后还可能被抄送、重定向改掉，
    拿它当审批时间会偏。这里按操作记录取最后一条 agree。
    """
    latest = 0
    for record in records or []:
        if str(record.get('actionExit') or '').lower() != 'agree':
            continue
        latest = max(latest, parse_gmt(record.get('operateTimeGMT')))
    return latest

def find_records(payload, depth=0):
    """取响应里的记录列表。信封套娃不固定，逐层找第一个列表，最多下探四层。"""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict) or depth > 4:
        return []
    for key in ('data', 'result', 'content', 'records', 'list'):
        found = find_records(payload.get(key), depth + 1)
        if found:
            return found
    return []


def form_data_of(record):
    """实例记录里的表单值。有的接口给 dict，有的给 JSON 字符串。"""
    if not isinstance(record, dict):
        return {}
    for key in ('formData', 'data', 'formValue'):
        value = record.get(key)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                continue
        if isinstance(value, dict):
            return value
    return {}


def instance_id_of(record):
    if not isinstance(record, dict):
        return ''
    for key in ('processInstanceId', 'formInstId', 'id'):
        if record.get(key):
            return str(record[key])
    return ''


def attachments_of(record):
    """返回 [(字段id, 附件项), ...]，与 fetch_cli 用同一套解析。"""
    return [(extract.field_id_of(path), item)
            for path, item in extract.find_attachments(form_data_of(record))]


# ---------- 下载 ----------

def download(url, dest_path, timeout=180):
    """下载临时地址，返回实际字节数。临时地址不需要登录态，但会过期。"""
    with requests.get(url, stream=True, timeout=timeout) as resp:
        if resp.status_code != 200:
            raise OpenApiError('下载附件', resp.status_code, 'download_failed',
                               'HTTP %d' % resp.status_code)
        folder = os.path.dirname(os.path.abspath(dest_path))
        os.makedirs(folder, exist_ok=True)
        size = 0
        with open(dest_path, 'wb') as fh:
            for chunk in resp.iter_content(1 << 16):
                if not chunk:
                    continue
                fh.write(chunk)
                size += len(chunk)
    return size
