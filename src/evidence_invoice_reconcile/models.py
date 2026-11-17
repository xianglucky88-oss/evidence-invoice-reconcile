"""领域模型：证据级发票核验的不可变结果契约。

所有 dataclass 均为 frozen；金额在边界处解析为整数分（cents），
对象内部不再出现 float。时间戳统一使用 ISO-8601 字符串，由输入提供，
系统时钟仅用于 run_id/事件落账时间，不参与业务判定。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Tuple


class Decision(str, Enum):
    VERIFIED = "verified"        # 已核验，可入账
    MANUAL_REVIEW = "manual"     # 待人工处理，挂账不提交
    REJECTED = "rejected"        # 拒绝入账


class AnomalyLevel(str, Enum):
    HARD = "hard"                # 直接拒绝
    HIGH = "high"                # 强制人工
    MEDIUM = "medium"            # 人工可确认


# 异常代码 -> 默认级别。集中定义，报告里保留规则版本与字段路径。
ANOMALY_CATALOG: Dict[str, AnomalyLevel] = {
    # 硬错误：守恒/结构性破坏
    "STRUCTURE_INVALID": AnomalyLevel.HARD,
    "NEGATIVE_UNEXPLAINED_BALANCE": AnomalyLevel.HARD,
    "RED_EXCEEDS_BLUE": AnomalyLevel.HARD,
    "PAYMENT_TARGET_MISSING": AnomalyLevel.HARD,
    # 高优先级人工
    "EVIDENCE_HASH_DRIFT": AnomalyLevel.HIGH,
    "DUPLICATE_NUMBER_CONFLICT": AnomalyLevel.HIGH,
    "TAX_MISMATCH": AnomalyLevel.HIGH,
    "TOTALS_MISMATCH": AnomalyLevel.HIGH,
    "RED_UNALLOCATED": AnomalyLevel.HIGH,
    "PARTIAL_MATCH": AnomalyLevel.HIGH,
    "CROSS_PERIOD_PAYMENT": AnomalyLevel.HIGH,
    "EVIDENCE_INCONSISTENT": AnomalyLevel.HIGH,
    # 中优先级（可人工确认后放行）
    "DUPLICATE_EXPORT": AnomalyLevel.MEDIUM,
    "PAYMENT_OVERMATCH": AnomalyLevel.MEDIUM,
    "PAYMENT_UNMATCHED": AnomalyLevel.MEDIUM,
    "MANUAL_CORRECTION": AnomalyLevel.MEDIUM,
    "TIMESTAMP_ORDER": AnomalyLevel.MEDIUM,
}


@dataclass(frozen=True)
class EvidenceRef:
    """原始文件证据引用：来源、哈希、时间戳、字段定位。"""
    source: str                       # ocr / api / booking / payment / manual
    file_id: str
    hash: Optional[str]
    observed_at: Optional[str]
    locator: Optional[str] = None     # 例如 OCR 页码/坐标或接口 JSON 路径


@dataclass(frozen=True)
class LineItem:
    """发票明细行（一票多行）。"""
    line_no: int
    description: str
    rate_code: str
    net_cents: int
    tax_cents: int                    # 票面申报税额
    expected_tax_cents: int           # 按冻结规则版本计算的预期税额
    gross_cents: int
    field_path: str                   # invoices[i].lines[j]
    evidence: Tuple[EvidenceRef, ...] = ()


@dataclass(frozen=True)
class Invoice:
    number: str
    kind: str                         # normal(蓝字) / red(红字)
    counterparty: Optional[str]       # 购销对手方名称/税号（候选匹配信号）
    fiscal_period: Optional[str]
    rule_version: str
    lines: Tuple[LineItem, ...]
    net_cents: int
    tax_cents: int
    gross_cents: int
    status: str                       # 票面状态字段
    certified_at: Optional[str]       # 认证时间
    booked_at: Optional[str]          # 采购入账时间（外部账系统回传）
    paid_at: Optional[str]            # 付款时间（从付款凭证链接）
    evidence: Tuple[EvidenceRef, ...]
    field_path: str
    # 红字声明的冲销目标（可能为空，由匹配器补候选）
    red_links: Tuple[str, ...] = ()
    export_id: Optional[str] = None   # 导出批次号（重复导出检测）
    source_hash: Optional[str] = None
    manual_correction_of: Optional[str] = None  # 人工更正所针对的历史结论 id


@dataclass(frozen=True)
class Payment:
    payment_id: str
    invoice_number: Optional[str]     # 可为空表示付款凭证未指明目标
    amount_cents: int
    paid_at: Optional[str]
    fiscal_period: Optional[str]
    evidence: Tuple[EvidenceRef, ...]
    field_path: str


@dataclass(frozen=True)
class Anomaly:
    code: str
    level: str
    message: str
    field_path: str
    evidence: Tuple[EvidenceRef, ...]
    rule_version: str
    calc: Dict[str, Any]              # 金额计算过程（分 + 十进制文本）
    candidate_refs: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Allocation:
    """一条红字 -> 蓝字的冲销分配。"""
    red_number: str
    blue_number: str
    amount_cents: int                 # 分配的含税金额（分），正数
    rule_version: str
    method: str                       # explicit / global / greedy
    calc: Dict[str, Any]


@dataclass(frozen=True)
class Conclusion:
    """单张发票的核验结论（证据报告的核心记录）。"""
    conclusion_id: str
    invoice_number: str
    decision: str                     # Decision 的值
    rule_version: str
    run_id: str
    sequence: int                     # 同一发票的结论版本序号，从 1 开始
    supersedes: Optional[str]         # 被取代的旧 conclusion_id
    reason_codes: Tuple[str, ...]
    anomalies: Tuple[Anomaly, ...]
    allocations: Tuple[Allocation, ...]
    payment_ids: Tuple[str, ...]
    amounts: Dict[str, Any]           # 净额/税额/含税/余额的计算快照
    evidence: Tuple[EvidenceRef, ...]
    input_hash: str
    created_at: str
    booked: bool                      # 是否已提交账务（ledger 中不可静默改写）


@dataclass(frozen=True)
class MatchCandidate:
    red_number: str
    blue_number: str
    score: int
    reasons: Tuple[str, ...]
    link_declared: bool


@dataclass(frozen=True)
class AllocationPlan:
    candidates: Tuple[MatchCandidate, ...]
    allocations: Tuple[Allocation, ...]
    method: str
    cost: Dict[str, int]
    conservation: Dict[str, Any]
    unallocated_red_cents: int
    unmatched_blue_cents: int
