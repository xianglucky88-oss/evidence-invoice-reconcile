"""领域核验引擎：在已解析对象上生成异常、计算结论与守恒快照。

引擎不做持久化，全部为纯函数，便于单测与重放。每个异常都携带：

- ``field_path``：问题字段的 JSON 路径；
- ``evidence``：来源证据（OCR/接口/付款凭证/人工更正）；
- ``rule_version``：判定所用的冻结规则版本；
- ``calc``：金额计算过程（整数分 + 两位十进制文本）。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import (
    ANOMALY_CATALOG, Anomaly, AnomalyLevel, Decision, EvidenceRef,
    Invoice, Payment,
)
from .money import from_cents
from .taxrules import DEFAULT_RULE_VERSION, get_rule


def _anomaly(code: str, field_path: str, message: str, evidence: Sequence[EvidenceRef],
             rule_version: str, calc: Dict[str, Any],
             candidate_refs: Sequence[str] = ()) -> Anomaly:
    return Anomaly(
        code=code,
        level=ANOMALY_CATALOG[code].value,
        message=message,
        field_path=field_path,
        evidence=tuple(evidence),
        rule_version=rule_version,
        calc=calc,
        candidate_refs=tuple(candidate_refs),
    )


def _money_calc(**cents: int) -> Dict[str, Any]:
    return {k: {"cents": v, "text": from_cents(v)} for k, v in cents.items()}


# ---------------------------------------------------------------- 单票核验

def verify_invoice(invoice: Invoice, line_totals: Dict[str, Tuple[int, int]],
                   duplicates: Dict[str, List[Invoice]],
                   export_seen: Dict[str, List[Invoice]],
                   evidence_index: Dict[str, List[EvidenceRef]],
                   known_numbers: set) -> List[Anomaly]:
    anomalies: List[Anomaly] = []
    rv = invoice.rule_version

    # 1) 明细行合计 vs 发票头（TOTALS_MISMATCH）
    line_net, line_tax = line_totals.get(invoice.number, (0, 0))
    if line_net != invoice.net_cents or line_tax != invoice.tax_cents:
        anomalies.append(_anomaly(
            "TOTALS_MISMATCH", invoice.field_path,
            "明细行合计与发票头金额不一致",
            invoice.evidence, rv,
            {
                "header": _money_calc(net=invoice.net_cents, tax=invoice.tax_cents,
                                      gross=invoice.gross_cents),
                "lines_sum": _money_calc(net=line_net, tax=line_tax,
                                         gross=line_net + line_tax),
                "difference": _money_calc(
                    net=invoice.net_cents - line_net,
                    tax=invoice.tax_cents - line_tax),
            },
        ))

    # 2) 逐行税额 + 整票预期税额（TAX_MISMATCH，计算过程按冻结版本）
    rule = get_rule(rv)
    line_calc = []
    tax_diff_total = 0
    for line in invoice.lines:
        diff = line.tax_cents - line.expected_tax_cents
        tax_diff_total += diff
        line_calc.append({
            "field_path": line.field_path,
            "rate_code": line.rate_code,
            "rate": str(rule.rate_of(line.rate_code)),
            "declared_tax": from_cents(line.tax_cents),
            "expected_tax": from_cents(line.expected_tax_cents),
            "rounding_level": rule.rounding_level,
            "rounding": rule.rounding,
            "difference_cents": diff,
        })
    expected_invoice_tax_cents = sum(line.expected_tax_cents for line in invoice.lines)
    if rule.rounding_level == "invoice":
        # 发票级版本重算整票预期税额（与逐行和可能有 1 分差异）
        from .taxrules import expected_invoice_tax
        expected_invoice_tax_cents = int(expected_invoice_tax(
            rule, [{"net": Decimal(from_cents(l.net_cents)), "rate_code": l.rate_code}
                   for l in invoice.lines]).scaleb(2))
    if invoice.tax_cents != expected_invoice_tax_cents or any(d["difference_cents"] for d in line_calc):
        anomalies.append(_anomaly(
            "TAX_MISMATCH", f"{invoice.field_path}.tax",
            f"票面税额与规则版本 {rv} 的预期税额不符",
            invoice.evidence, rv,
            {
                "rule_version": rv,
                "declared_invoice_tax": from_cents(invoice.tax_cents),
                "expected_invoice_tax": from_cents(expected_invoice_tax_cents),
                "invoice_tax_difference_cents": invoice.tax_cents - expected_invoice_tax_cents,
                "lines": line_calc,
            },
        ))

    # 3) 同号重复：同号 + 同内容哈希 => 重复导出；同号 + 不同哈希 => 冲突
    peers = [p for p in duplicates.get(invoice.number, []) if p is not invoice]
    if peers:
        same_hash = [p for p in peers if p.source_hash and p.source_hash == invoice.source_hash]
        if same_hash:
            anomalies.append(_anomaly(
                "DUPLICATE_EXPORT", f"{invoice.field_path}.number",
                f"发票号 {invoice.number} 重复导出且原始哈希一致，禁止重复入账",
                tuple(invoice.evidence) + tuple(e for p in same_hash for e in p.evidence),
                rv, {"peer_numbers": [p.number for p in peers],
                     "source_hash": invoice.source_hash,
                     "export_ids": [p.export_id for p in [invoice] + peers]},
            ))
        else:
            anomalies.append(_anomaly(
                "DUPLICATE_NUMBER_CONFLICT", f"{invoice.field_path}.number",
                f"发票号 {invoice.number} 重复但原始哈希/内容不一致，需人工甄别",
                tuple(invoice.evidence) + tuple(e for p in peers for e in p.evidence),
                rv, {"peer_field_paths": [p.field_path for p in peers],
                     "peer_hashes": [p.source_hash for p in peers],
                     "this_hash": invoice.source_hash},
            ))

    # 4) 多来源证据一致性（OCR vs 结构化接口的金额/哈希漂移）
    source_amounts: Dict[str, Dict[str, Any]] = {}
    source_hashes: Dict[str, List[str]] = {}
    for ev in invoice.evidence:
        source_hashes.setdefault(ev.source, []).append(ev.hash or "")
        for ref in evidence_index.get(_ev_key(invoice.number, ev.source), []):
            source_amounts.setdefault(ref.source, ref.locator)
    if len({tuple(v) for v in source_hashes.values() if v}) > 1 and len(source_hashes) > 1:
        anomalies.append(_anomaly(
            "EVIDENCE_INCONSISTENT", f"{invoice.field_path}.evidence",
            "OCR 与接口来源的证据哈希不一致，需要核验原始文件",
            invoice.evidence, rv, {"hashes_by_source": source_hashes},
        ))

    # 5) 红字显式关联目标存在性
    if invoice.kind == "red":
        missing = [n for n in invoice.red_links if n not in known_numbers]
        for target in missing:
            anomalies.append(_anomaly(
                "STRUCTURE_INVALID", f"{invoice.field_path}.red_links",
                f"红字发票声明冲销的蓝票 {target} 不存在",
                invoice.evidence, rv,
                {"red_number": invoice.number, "missing_target": target,
                 **_money_calc(red_gross=-invoice.gross_cents)},
                candidate_refs=[target],
            ))

    # 6) 人工更正标记（新结论而非覆盖）
    if invoice.manual_correction_of:
        anomalies.append(_anomaly(
            "MANUAL_CORRECTION", f"{invoice.field_path}.manual_correction_of",
            f"该发票为人工更正，针对历史结论 {invoice.manual_correction_of}",
            invoice.evidence, rv,
            {"supersedes_hint": invoice.manual_correction_of},
        ))

    # 7) 认证→入账时间链
    if invoice.certified_at and invoice.booked_at and invoice.booked_at < invoice.certified_at:
        anomalies.append(_anomaly(
            "TIMESTAMP_ORDER", f"{invoice.field_path}.booked_at",
            "入账时间早于认证时间，时间链不成立",
            invoice.evidence, rv,
            {"certified_at": invoice.certified_at, "booked_at": invoice.booked_at},
        ))

    return anomalies


def _ev_key(invoice_number: str, source: str) -> str:
    return f"{invoice_number}|{source}"


# ---------------------------------------------------------------- 红字分配异常

def verify_allocations(invoice: Invoice, plan, known_numbers: set) -> List[Anomaly]:
    anomalies: List[Anomaly] = []
    rv = invoice.rule_version
    if invoice.kind != "red":
        return anomalies
    red_amt = -invoice.gross_cents
    mine = [a for a in plan.allocations if a.red_number == invoice.number]
    allocated = sum(a.amount_cents for a in mine)

    # 显式关联但蓝票不存在 -> HARD（在 verify_invoice 已加 STRUCTURE_INVALID）
    missing = [n for n in invoice.red_links if n not in known_numbers]

    # 声明关联金额超过被关联蓝票总额
    if invoice.red_links and not missing:
        blue_cap = {a.blue_number: 0 for a in mine}
        for a in mine:
            blue_cap[a.blue_number] = blue_cap.get(a.blue_number, 0) + a.amount_cents
    if red_amt > 0 and allocated == 0 and not missing:
        # 完全无法冲销：红字全额成为不可解释负余额 => HARD 拒绝
        anomalies.append(_anomaly(
            "NEGATIVE_UNEXPLAINED_BALANCE", invoice.field_path,
            f"红字发票 {invoice.number} 找不到可冲销的蓝字候选，"
            f"全额 {from_cents(red_amt)} 成为不可解释负余额，拒绝入账",
            invoice.evidence, rv,
            {"red_gross": from_cents(red_amt), "candidate_count": len(plan.candidates),
             **_money_calc(unallocated=red_amt)},
            candidate_refs=[c.blue_number for c in plan.candidates
                            if c.red_number == invoice.number],
        ))
    elif allocated < red_amt:
        # 未完全分配：红字超过可冲销蓝字额度，会制造不可解释负余额 => HARD 拒绝
        residual = red_amt - allocated
        anomalies.append(_anomaly(
            "NEGATIVE_UNEXPLAINED_BALANCE", f"{invoice.field_path}.red_links",
            f"红字发票 {invoice.number} 有 {from_cents(residual)} 无法分配冲销，"
            "会形成不可解释负余额，拒绝入账",
            invoice.evidence, rv,
            {**_money_calc(red_gross=red_amt, allocated=allocated, residual=residual),
             "allocations": [{"blue": a.blue_number, "amount": from_cents(a.amount_cents),
                              "method": a.method} for a in mine]},
            candidate_refs=[c.blue_number for c in plan.candidates
                            if c.red_number == invoice.number],
        ))

    # 守恒违约（理论上不会出现，双保险）
    for v in plan.conservation["violations"]:
        if v["invoice"] == invoice.number:
            anomalies.append(_anomaly(
                "RED_EXCEEDS_BLUE", invoice.field_path,
                "冲销分配超过蓝字发票可冲销余额",
                invoice.evidence, rv, v,
            ))
    return anomalies


# ---------------------------------------------------------------- 付款匹配

def verify_payments(invoice: Invoice, payments: List[Payment],
                    plan) -> Tuple[List[Anomaly], List[str], Optional[Payment]]:
    anomalies: List[Anomaly] = []
    rv = invoice.rule_version
    matched = [p for p in payments if p.invoice_number == invoice.number]
    payment_ids = [p.payment_id for p in matched]

    if invoice.kind == "red":
        # 红字不应直接收款；若有付款指向红字，挂人工
        for p in matched:
            anomalies.append(_anomaly(
                "PARTIAL_MATCH", p.field_path,
                f"付款凭证 {p.payment_id} 指向红字发票 {invoice.number}，应与原蓝票链路核对",
                p.evidence + invoice.evidence, rv,
                {"payment_amount": from_cents(p.amount_cents),
                 "red_gross": from_cents(invoice.gross_cents)},
            ))
        return anomalies, payment_ids, None

    open_balance = invoice.gross_cents  # 蓝票待付金额（冲销后的净额由 report 层结合红字计算）
    paid_total = sum(p.amount_cents for p in matched)
    primary = matched[0] if matched else None

    if not matched:
        # 未付款不是异常，只影响“付款证据”完整性标记
        return anomalies, payment_ids, None

    if paid_total < open_balance:
        anomalies.append(_anomaly(
            "PARTIAL_MATCH", f"{invoice.field_path}.gross",
            f"发票 {invoice.number} 存在部分付款，未清余额 {from_cents(open_balance - paid_total)}",
            tuple(e for p in matched for e in p.evidence), rv,
            {**_money_calc(gross=open_balance, paid=paid_total,
                           outstanding=open_balance - paid_total),
             "payment_ids": payment_ids},
        ))
    elif paid_total > open_balance:
        anomalies.append(_anomaly(
            "PAYMENT_OVERMATCH", f"{invoice.field_path}.gross",
            f"发票 {invoice.number} 付款超额 {from_cents(paid_total - open_balance)}",
            tuple(e for p in matched for e in p.evidence), rv,
            {**_money_calc(gross=open_balance, paid=paid_total,
                           overpaid=paid_total - open_balance),
             "payment_ids": payment_ids},
        ))

    # 跨期付款：付款会计期间 != 发票会计期间
    for p in matched:
        if invoice.fiscal_period and p.fiscal_period and p.fiscal_period != invoice.fiscal_period:
            anomalies.append(_anomaly(
                "CROSS_PERIOD_PAYMENT", p.field_path,
                f"付款 {p.payment_id} 期间 {p.fiscal_period} 与发票期间 "
                f"{invoice.fiscal_period} 不一致，触发跨期更正结论",
                p.evidence + invoice.evidence, rv,
                {"invoice_period": invoice.fiscal_period,
                 "payment_period": p.fiscal_period,
                 "invoice_number": invoice.number,
                 "payment_id": p.payment_id,
                 "paid_at": p.paid_at},
            ))

    # 付款时间早于认证/入账
    if primary and primary.paid_at:
        if invoice.certified_at and primary.paid_at < invoice.certified_at:
            anomalies.append(_anomaly(
                "TIMESTAMP_ORDER", primary.field_path,
                "付款时间早于发票认证时间",
                primary.evidence, rv,
                {"paid_at": primary.paid_at,
                 "certified_at": invoice.certified_at},
            ))
    return anomalies, payment_ids, primary


def orphan_payments(payments: List[Payment], known_numbers: set) -> List[Anomaly]:
    out: List[Anomaly] = []
    for p in payments:
        if p.invoice_number is None:
            out.append(_anomaly(
                "PAYMENT_TARGET_MISSING", p.field_path,
                f"付款 {p.payment_id} 未指明发票号，无法建立证据链",
                p.evidence, DEFAULT_RULE_VERSION,
                {"payment_amount": from_cents(p.amount_cents)},
            ))
        elif p.invoice_number not in known_numbers:
            out.append(_anomaly(
                "PAYMENT_TARGET_MISSING", p.field_path,
                f"付款 {p.payment_id} 指向不存在的发票 {p.invoice_number}",
                p.evidence, DEFAULT_RULE_VERSION,
                {"payment_amount": from_cents(p.amount_cents),
                 "missing_invoice": p.invoice_number},
            ))
    return out


def decide(anomalies: List[Anomaly]) -> str:
    """异常分级 -> 最终决策。HARD 拒绝；HIGH/MEDIUM 人工；无异常已核验。"""
    if any(a.level == AnomalyLevel.HARD.value for a in anomalies):
        return Decision.REJECTED.value
    if anomalies:
        return Decision.MANUAL_REVIEW.value
    return Decision.VERIFIED.value


def calculate(rows, payload):
    """基线兼容入口，委托给 pipeline 中的实现。"""
    from .pipeline import calculate as _calculate
    return _calculate(rows, payload)
