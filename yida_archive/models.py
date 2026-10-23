# -*- coding: utf-8 -*-
"""数据结构定义。"""

from dataclasses import dataclass, field
from typing import List, Optional


def as_millis(value) -> int:
    """把 gmtCreate 之类的时间戳转成整数毫秒，转不了就返回 0。

    不能直接 int()：接口偶尔会给出带小数、带单位或者干脆是格式化过的
    字符串，那会抛 ValueError。而取数循环里这个异常会一路冒到 main()，
    把整轮作业干掉——2026-09-28 用户在 33 个表单的全流程配置上就是这么
    崩的，同一时刻重跑又是好的。归档少一条时间戳可以接受，整轮跑不完
    不可以。
    """
    if value is None or value is True or value is False:
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


@dataclass
class AttachmentObs:
    """一次附件出现。

    同一条单据的同一个字段里出现一次，就算一次 observation。
    同一个文件被上传多次，会产生多条 observation。
    """

    form_name: str            # 来源流程名，如 合同审批
    form_uuid: str
    instance_id: str          # processInstanceId / formInstId
    instance_title: str
    submitted_at: int         # 提交时间，毫秒时间戳
    field_id: str
    field_label: str          # 字段中文名，如 上传发票
    name: str                 # 用户可见文件名
    size: Optional[int]       # 字节数
    file_uuid: str            # 平台物理文件名
    url: str                  # 下载地址（绝对地址）
    content_hash: str = ''    # 下载后才会有
    source_order: int = 0     # 同一时间的附件按输入列表顺序排序
    document_id: str = ''     # 上游明确提供的逻辑文件ID（不是物理 fileUuid）
    revision: int = 0         # 上游明确提供的修订号

    @property
    def ext(self) -> str:
        if '.' not in self.name:
            return ''
        return '.' + self.name.rsplit('.', 1)[1]


@dataclass
class Instance:
    """一条流程实例（或普通表单记录）。"""

    form_name: str
    form_uuid: str
    instance_id: str
    title: str
    submitted_at: int
    originator: str = ''
    status: str = ''
    attachments: List[AttachmentObs] = field(default_factory=list)


@dataclass
class LogicalFile:
    """去重后的一个逻辑文件。"""

    ident: str                 # 稳定标识：有哈希用哈希，否则 name|size
    name: str
    size: Optional[int]
    file_uuid: str
    url: str
    content_hash: str
    obs: List[AttachmentObs] = field(default_factory=list)

    # 分类与命名结果
    folder: str = ''
    type_name: str = ''
    confidence: str = ''       # confirmed / suggested / manual
    new_name: str = ''
    secret: bool = False
    cache_path: str = ''
    download_error: str = ''
    version_group: str = ''
    version_order: int = 0
    superseded: bool = False
    review_note: str = ''
    numbering_id: str = ''
    ai_result: dict = field(default_factory=dict)  # 正文识别状态、证据及调用量

    @property
    def upload_count(self) -> int:
        return len(self.obs)

    @property
    def forms(self) -> List[str]:
        return sorted({o.form_name for o in self.obs})

    @property
    def first_seen(self) -> int:
        return min(o.submitted_at for o in self.obs)

    @property
    def ext(self) -> str:
        if '.' not in self.name:
            return ''
        return '.' + self.name.rsplit('.', 1)[1]


@dataclass
class DedupeFindings:
    """去重过程中发现的特殊情况，都要输出给人看。"""

    version_conflicts: dict = field(default_factory=dict)   # 文件名 -> [大小...]
    rename_candidates: dict = field(default_factory=dict)   # 大小 -> [文件名...]
