"""编排管线：匹配候选 → 冲销分配 → 账务提交 → 证据报告的事务边界。

四个阶段及其事务语义：

1. **候选/分配阶段（只读事务）**：解析 + 全局/贪心两套分配 + 守恒检查，
   不落任何账，失败可整体重跑。
2. **结论阶段（每票一个 WAL 事务）**：逐票生成结论事件。单票异常不影响
   其他发票；批次中断时已提交的结论永久保留，重跑自动跳过。
3. **账务提交阶段（结论事务内同一 WAL）**：只有 ``verified`` 结论写入
   ledger；重复导出、拒绝、人工挂账一律不提交。
4. **证据报告阶段（只读）**：从 journal 重放生成报告，不修改任何状态。

重跑保证：同一 ``(发票号, 输入哈希)`` 永远返回同一 conclusion_id；
输入变化产生新结论并以 ``supersedes`` 链接旧结论，旧审计记录不被覆盖。
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from .engine import (
    decide, orphan_payments, verify_allocations, verify_invoice, verify_payments,
)
from .hashing import canonical_hash
from .journal import (
    AuditJournal, EV_BOOKING, EV_BOOKING_REJECT, EV_CONCLUSION, EV_SUPERSEDE,
    fingerprint_payload,
)
from .matching import build_plans
from .models import Allocation, Anomaly, Conclusion, Decision, Invoice, Payment
from .money import from_cents
from .parser import parse_payload
from .report import build_report


def _invoice_input_hash(invoice: Invoice, payments: List[Payment]) -> str:
    """单票输入指纹：发票全字段 + 指向它的付款凭证。"""
    payload = {
        "number": invoice.number,
        "kind": invoice.kind,
        "rule_version": invoice.rule_version,
        "net": from_cents(invoice.net_cents),
        "tax": from_cents(invoice.tax_cents),
        "lines": [{"net": from_cents(l.net_cents), "tax": from_cents(l.tax_cents),
                   "rate_code": l.rate_code} for l in invoice.lines],
        "red_links": list(invoice.red_links),
        "evidence": [{"source": e.source, "file_id": e.file_id, "hash": e.hash}
                     for e in invoice.evidence],
        "export_id": invoice.export_id,
        "source_hash": invoice.source_hash,
        "manual_correction_of": invoice.manual_correction_of,
        "payments": [{"payment_id": p.payment_id, "amount": from_cents(p.amount_cents),
                      "paid_at": p.paid_at, "fiscal_period": p.fiscal_period,
                      "evidence_hash": [e.hash for e in p.evidence]}
                     for p in payments if p.invoice_number == invoice.number],
    }
    return canonical_hash(payload)


def _anomaly_to_dict(a: Anomaly) -> Dict[str, Any]:
    return {
        "code": a.code,
        "level": a.level,
        "message": a.message,
        "field_path": a.field_path,
        "evidence": [{"source": e.source, "file_id": e.file_id, "hash": e.hash,
                      "locator": e.locator, "observed_at": e.observed_at}
                     for e in a.evidence],
        "rule_version": a.rule_version,
        "calc": a.calc,
        "candidate_refs": list(a.candidate_refs),
    }


def _allocation_to_dict(a: Allocation) -> Dict[str, Any]:
    return {
        "red_number": a.red_number,
        "blue_number": a.blue_number,
        "amount": from_cents(a.amount_cents),
        "amount_cents": a.amount_cents,
        "method": a.method,
        "rule_version": a.rule_version,
        "calc": a.calc,
    }


def run(payload: dict, *, state_dir: Optional[str] = None,
        journal: Optional[AuditJournal] = None,
        fail_after: Optional[int] = None) -> dict:
    """执行一次完整对账。

    ``state_dir`` 为 None 时使用内存账本（测试/无状态 CLI）；
    ``fail_after`` 用于批次中断演练：处理 N 张发票后抛出异常，
    已提交的前 N 个 WAL 事务必须仍然完整可重放。
    """
    parsed = parse_payload(payload)
    invoices: List[Invoice] = parsed["invoices"]
    payments: List[Payment] = parsed["payments"]
    line_totals = parsed["line_totals"]
    run_input_hash = fingerprint_payload(payload)

    # 阶段 1：候选 + 双策略分配（只读，无副作用）
    reds = [i for i in invoices if i.kind == "red"]
    blues = [i for i in invoices if i.kind == "normal"]
    known_numbers = {i.number for i in invoices}
    global_plan, greedy_plan = build_plans(reds, blues, parsed["declared_rule_version"])

    # 重复检测索引
    duplicates: Dict[str, List[Invoice]] = {}
    export_seen: Dict[str, List[Invoice]] = {}
    evidence_index: Dict[str, list] = {}
    for inv in invoices:
        duplicates.setdefault(inv.number, []).append(inv)
        if inv.export_id:
            export_seen.setdefault(inv.export_id, []).append(inv)
        for ev in inv.evidence:
            evidence_index.setdefault(f"{inv.number}|{ev.source}", []).append(ev)

    owns_journal = journal is None
    journal = journal or AuditJournal(state_dir)
    run_id = journal.run_id()

    conclusions: List[Conclusion] = []
    skipped_idempotent: List[Dict[str, str]] = []

    with journal:
        state = journal.replay()
        # 阶段 2/3：逐票结论 + 账务提交，每张发票一个独立 WAL 事务
        for order, inv in enumerate(invoices):
            if fail_after is not None and order >= fail_after:
                raise _BatchInterrupted(
                    run_id=run_id,
                    processed=[c.conclusion_id for c in conclusions],
                    remaining=[i.number for i in invoices[order:]],
                )
            input_hash = _invoice_input_hash(inv, payments)
            existing_id = state["input_hash_index"].get(f"{inv.number}|{input_hash}")
            if existing_id:
                # 幂等：同输入哈希重放，原结论原样返回，不产生新事件
                prior = state["conclusions"][existing_id]
                conclusions.append(_rehydrate(prior))
                skipped_idempotent.append({
                    "invoice_number": inv.number,
                    "conclusion_id": existing_id,
                    "reason": "identical_input_hash",
                })
                continue

            tx = f"{run_id}-{order + 1:04d}"
            with journal.transaction(run_id, tx, input_hash) as writer:
                anomalies = verify_invoice(inv, line_totals, duplicates, export_seen,
                                           evidence_index, known_numbers)
                anomalies += verify_allocations(inv, global_plan, known_numbers)
                pay_anoms, payment_ids, _primary = verify_payments(inv, payments, global_plan)
                anomalies += pay_anoms

                prior = journal.latest_conclusion(inv.number, state)
                # 跨运行证据漂移：同号发票的新版本输入相对历史结论，
                # 原始文件哈希发生变化 => 强制人工的可追溯新结论
                drift = _detect_hash_drift(inv, prior)
                if drift:
                    anomalies.append(drift)

                decision = decide(anomalies)

                mine = [a for a in global_plan.allocations if a.red_number == inv.number] \
                    if inv.kind == "red" else \
                    [a for a in global_plan.allocations if a.blue_number == inv.number]
                amounts = _amount_snapshot(inv, payments, mine)
                sequence = (max(
                    (state["conclusions"][cid].get("sequence", 1)
                     for cid in state["invoice_history"].get(inv.number, [])),
                    default=0) + 1)
                conclusion_id = f"CONC-{inv.number}-{sequence:03d}-{uuid.uuid4().hex[:8]}"

                event_payload = {
                    "conclusion_id": conclusion_id,
                    "invoice_number": inv.number,
                    "decision": decision,
                    "rule_version": inv.rule_version,
                    "run_id": run_id,
                    "sequence": sequence,
                    "supersedes": prior["conclusion_id"] if prior else None,
                    "reason_codes": sorted({a.code for a in anomalies}),
                    "anomalies": [_anomaly_to_dict(a) for a in anomalies],
                    "allocations": [_allocation_to_dict(a) for a in mine],
                    "payment_ids": payment_ids,
                    "amounts": amounts,
                    "evidence": [{"source": e.source, "file_id": e.file_id,
                                  "hash": e.hash, "locator": e.locator,
                                  "observed_at": e.observed_at}
                                 for e in inv.evidence],
                    "input_hash": input_hash,
                    "booked": False,
                    "field_path": inv.field_path,
                }
                writer.append(EV_SUPERSEDE if prior else EV_CONCLUSION, event_payload)

                # 阶段 3：账务提交（同一 WAL 事务）。事件已做快照隔离，
                # 这里只追加独立的账务事件，不回写结论事件。
                booked = False
                if decision == Decision.VERIFIED.value:
                    # 重复导出即使没有硬异常也不允许重复入账
                    already_booked = inv.number in state["ledger"]
                    if already_booked or any(a.code == "DUPLICATE_EXPORT" for a in anomalies):
                        writer.append(EV_BOOKING_REJECT, {
                            "invoice_number": inv.number,
                            "conclusion_id": conclusion_id,
                            "run_id": run_id,
                            "reason": "duplicate_export_already_booked",
                        })
                    else:
                        writer.append(EV_BOOKING, {
                            "invoice_number": inv.number,
                            "conclusion_id": conclusion_id,
                            "run_id": run_id,
                            "amounts": amounts,
                            "allocations": event_payload["allocations"],
                        })
                        booked = True
                else:
                    writer.append(EV_BOOKING_REJECT, {
                        "invoice_number": inv.number,
                        "conclusion_id": conclusion_id,
                        "run_id": run_id,
                        "reason": f"decision_{decision}",
                    })
                writer.commit_marker()
            # 事务已提交，刷新内存视图
            state = journal.replay()
            event_payload["booked"] = booked  # 仅用于本次内存返回，不影响已落账事件
            conclusions.append(_rehydrate(event_payload))

        # 孤儿付款属于批次级异常（挂在报告层，不归属单票）
        batch_anomalies = orphan_payments(payments, known_numbers)

    final_state = journal.replay()
    # 阶段 4：只读证据报告
    report = build_report(
        payload=payload,
        parsed=parsed,
        conclusions=conclusions,
        global_plan=global_plan,
        greedy_plan=greedy_plan,
        batch_anomalies=batch_anomalies,
        run_id=run_id,
        run_input_hash=run_input_hash,
        state=final_state,
        skipped_idempotent=skipped_idempotent,
    )
    if owns_journal and state_dir is not None:
        report["state_dir"] = state_dir
    return report


def _detect_hash_drift(inv: Invoice, prior: Optional[dict]):
    """检测同号发票相对历史结论的原始文件哈希漂移。

    历史结论的 evidence 中记录了当时的文件哈希；当前发票 evidence 中
    同 (source, file_id) 的哈希若不同，即构成漂移，返回 HIGH 级异常。
    新结论以 supersedes 链接旧结论，旧记录不被覆盖。
    """
    if prior is None:
        return None
    prior_evs = {(e.get("source"), e.get("file_id")): e.get("hash")
                 for e in prior.get("evidence", [])}
    drifted = []
    for ev in inv.evidence:
        old = prior_evs.get((ev.source, ev.file_id))
        if old is not None and ev.hash is not None and old != ev.hash:
            drifted.append({"source": ev.source, "file_id": ev.file_id,
                            "prior_hash": old, "current_hash": ev.hash,
                            "locator": ev.locator})
    if not drifted:
        return None
    from .models import Anomaly
    from .engine import _anomaly
    return _anomaly(
        "EVIDENCE_HASH_DRIFT", f"{inv.field_path}.evidence",
        f"发票 {inv.number} 的原始文件哈希相对结论 {prior['conclusion_id']} 发生变化",
        inv.evidence, inv.rule_version,
        {
            "prior_conclusion_id": prior["conclusion_id"],
            "prior_input_hash": prior.get("input_hash"),
            "current_input_hash_hint_source": inv.source_hash,
            "drifted_files": drifted,
        },
    )


class _BatchInterrupted(RuntimeError):
    def __init__(self, *, run_id: str, processed: List[str], remaining: List[str]):
        super().__init__(f"批次中断: run={run_id}, 已提交 {len(processed)} 张, 剩余 {len(remaining)} 张")
        self.run_id = run_id
        self.processed = processed
        self.remaining = remaining


BatchInterrupted = _BatchInterrupted


def _amount_snapshot(inv: Invoice, payments: List[Payment],
                     allocations: List[Allocation]) -> Dict[str, Any]:
    paid = sum(p.amount_cents for p in payments if p.invoice_number == inv.number)
    if inv.kind == "red":
        red_amt = -inv.gross_cents
        allocated = sum(a.amount_cents for a in allocations)
        return {
            "net": from_cents(inv.net_cents),
            "tax": from_cents(inv.tax_cents),
            "gross": from_cents(inv.gross_cents),
            "red_abs_gross": from_cents(red_amt),
            "allocated_gross": from_cents(allocated),
            "unexplained_residual": from_cents(red_amt - allocated),
            "paid": from_cents(paid),
        }
    reversed_by_red = sum(a.amount_cents for a in allocations)
    net_position = inv.gross_cents - reversed_by_red
    return {
        "net": from_cents(inv.net_cents),
        "tax": from_cents(inv.tax_cents),
        "gross": from_cents(inv.gross_cents),
        "reversed_by_red": from_cents(reversed_by_red),
        "net_position_after_reversal": from_cents(net_position),
        "paid": from_cents(paid),
        "open_balance": from_cents(max(net_position - paid, 0)),
    }


def _rehydrate(payload: Dict[str, Any]) -> Conclusion:
    return Conclusion(
        conclusion_id=payload["conclusion_id"],
        invoice_number=payload["invoice_number"],
        decision=payload["decision"],
        rule_version=payload["rule_version"],
        run_id=payload["run_id"],
        sequence=payload["sequence"],
        supersedes=payload.get("supersedes"),
        reason_codes=tuple(payload["reason_codes"]),
        anomalies=tuple(),  # 完整异常在事件载荷中；重放对象仅承载摘要
        allocations=tuple(),
        payment_ids=tuple(payload.get("payment_ids", [])),
        amounts=payload["amounts"],
        evidence=tuple(),
        input_hash=payload["input_hash"],
        created_at=payload.get("created_at", ""),
        booked=payload.get("booked", False),
    )


# ------------------------------------------------------------- 基线兼容接口

def calculate(rows: List[dict], payload: dict) -> dict:
    """基线兼容：在新引擎上计算 gross_total 与异常摘要。

    保持基线契约（``gross_total``/``exceptions``/``paid_numbers``）不变，
    内部完全走新的解析与规则版本逻辑。
    """
    from decimal import Decimal
    from .money import from_cents
    from .taxrules import expected_invoice_tax, get_rule
    parsed = parse_payload(payload)
    gross = 0
    exceptions: List[Dict[str, str]] = []
    seen: Dict[str, int] = {}
    for inv in parsed["invoices"]:
        gross += inv.gross_cents
        seen[inv.number] = seen.get(inv.number, 0) + 1
        rule = get_rule(inv.rule_version)
        expected = expected_invoice_tax(
            rule,
            [{"net": Decimal(from_cents(l.net_cents)), "rate_code": l.rate_code}
             for l in inv.lines],
        )
        if int(expected.scaleb(2)) != inv.tax_cents:
            exceptions.append({"number": inv.number, "reason": "tax_mismatch"})
    for number, count in seen.items():
        if count > 1 and not number.endswith("-R"):
            exceptions.append({"number": number, "reason": "duplicate_number"})
    return {
        "gross_total": from_cents(gross),
        "exceptions": exceptions,
        "paid_numbers": [p.invoice_number for p in parsed["payments"]],
    }
