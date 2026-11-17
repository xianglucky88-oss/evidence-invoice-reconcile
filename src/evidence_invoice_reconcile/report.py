"""证据报告：稳定、可重放的输出信封。

报告是 journal 事件流的只读投影：

- 顶层键固定排序，金额同时给出 ``cents`` 与两位十进制文本；
- 每个结论保留字段路径、来源证据、规则版本、金额计算过程；
- 全局/贪心两套分配的失败代价并列输出，便于审计比较；
- ``conclusion_timeline`` 展示同一发票的结论版本链（supersedes），
  证明“新结论不覆盖旧审计记录”。
"""
from __future__ import annotations

from typing import Any, Dict, List

from .models import AllocationPlan, Anomaly, Conclusion, Decision
from .money import from_cents
from .taxrules import available_versions

PROJECT = "evidence-invoice-reconcile"


def _anomaly_dict(a: Anomaly) -> Dict[str, Any]:
    return {
        "code": a.code,
        "level": a.level,
        "message": a.message,
        "field_path": a.field_path,
        "rule_version": a.rule_version,
        "evidence": [{"source": e.source, "file_id": e.file_id, "hash": e.hash,
                      "locator": e.locator, "observed_at": e.observed_at}
                     for e in a.evidence],
        "calc": a.calc,
        "candidate_refs": list(a.candidate_refs),
    }


def _plan_dict(plan: AllocationPlan) -> Dict[str, Any]:
    return {
        "method": plan.method,
        "candidates": [
            {
                "red_number": c.red_number,
                "blue_number": c.blue_number,
                "score": c.score,
                "link_declared": c.link_declared,
                "reasons": list(c.reasons),
            }
            for c in plan.candidates
        ],
        "allocations": [
            {
                "red_number": a.red_number,
                "blue_number": a.blue_number,
                "amount": from_cents(a.amount_cents),
                "amount_cents": a.amount_cents,
                "rule_version": a.rule_version,
                "method": a.method,
                "calc": a.calc,
            }
            for a in plan.allocations
        ],
        "failure_cost": plan.cost,
        "conservation": plan.conservation,
        "unallocated_red": from_cents(plan.unallocated_red_cents),
        "unmatched_blue": from_cents(plan.unmatched_blue_cents),
    }


def _conclusion_dict(conclusion: Conclusion, full: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "conclusion_id": conclusion.conclusion_id,
        "invoice_number": conclusion.invoice_number,
        "decision": conclusion.decision,
        "rule_version": conclusion.rule_version,
        "run_id": conclusion.run_id,
        "sequence": conclusion.sequence,
        "supersedes": conclusion.supersedes,
        "reason_codes": list(conclusion.reason_codes),
        "anomalies": full.get("anomalies", []),
        "allocations": full.get("allocations", []),
        "payment_ids": list(conclusion.payment_ids),
        "amounts": conclusion.amounts,
        "evidence": full.get("evidence", []),
        "input_hash": conclusion.input_hash,
        "booked": full.get("booked", conclusion.booked),
        "field_path": full.get("field_path"),
    }


def build_result(project: str, data: dict) -> dict:
    """基线兼容信封。"""
    return {"project": project, "status": "ok", **data}


def build_report(*, payload: dict, parsed: dict, conclusions: List[Conclusion],
                 global_plan: AllocationPlan, greedy_plan: AllocationPlan,
                 batch_anomalies: List[Anomaly], run_id: str,
                 run_input_hash: str, state: Dict[str, Any],
                 skipped_idempotent: List[Dict[str, str]]) -> dict:
    invoices = parsed["invoices"]
    payments = parsed["payments"]

    # 金额守恒汇总（分）
    blue_gross = sum(i.gross_cents for i in invoices if i.kind == "normal")
    red_gross = sum(i.gross_cents for i in invoices if i.kind == "red")
    gross_total = blue_gross + red_gross
    paid_total = sum(p.amount_cents for p in payments)
    # ledger 金额快照存的是文本，按蓝/红方向分别汇总
    booked_blue = booked_red = 0
    for number in state["ledger"]:
        cid = state["ledger"][number]["conclusion_id"]
        conc = state["conclusions"][cid]
        if conc["decision"] != Decision.VERIFIED.value:
            continue
        gross = _text_to_cents(conc["amounts"]["gross"])
        if gross >= 0:
            booked_blue += gross
        else:
            booked_red += gross
    booked_net = booked_blue + booked_red

    by_decision = {d.value: [] for d in Decision}
    full_by_id = {c["conclusion_id"]: c for c in state["conclusions"].values()}
    for c in conclusions:
        full = dict(full_by_id.get(c.conclusion_id, {}))
        # 入账状态以独立 LEDGER 事件（ledger 成员关系）为准，
        # 不依赖结论事件创建时冻结的 booked 字段
        full["booked"] = c.invoice_number in state["ledger"]
        by_decision[c.decision].append(_conclusion_dict(c, full))

    # 结论版本时间线
    timeline = []
    for number, history in sorted(state["invoice_history"].items()):
        timeline.append({
            "invoice_number": number,
            "versions": [
                {
                    "conclusion_id": cid,
                    "decision": state["conclusions"][cid]["decision"],
                    "sequence": state["conclusions"][cid]["sequence"],
                    "supersedes": state["conclusions"][cid].get("supersedes"),
                    "input_hash": state["conclusions"][cid]["input_hash"],
                    "rule_version": state["conclusions"][cid]["rule_version"],
                    "booked": state["conclusions"][cid].get("booked", False),
                }
                for cid in history
            ],
        })

    strategy_comparison = {
        "global": _plan_dict(global_plan),
        "greedy": _plan_dict(greedy_plan),
        "global_advantage_cents": (
            greedy_plan.cost["total_failure_cost"]
            - global_plan.cost["total_failure_cost"]
        ),
        "chosen_method": global_plan.method,
        "choice_reason": "全局最小费用流在相同候选与惩罚价格下总成本不高于贪心，且与输入顺序无关",
    }

    envelope = {
        "project": PROJECT,
        "status": "ok",
        "run_id": run_id,
        "run_input_hash": run_input_hash,
        "rule_registry_versions": list(available_versions()),
        "totals": {
            "invoice_count": len(invoices),
            "payment_count": len(payments),
            "blue_gross": _money(blue_gross),
            "red_gross": _money(red_gross),
            "gross_total": _money(gross_total),
            "paid_total": _money(paid_total),
            "booked_blue_gross": _money(booked_blue),
            "booked_red_gross": _money(booked_red),
            "booked_net": _money(booked_net),
            "conservation_identity": (
                "gross_total = blue_gross + red_gross; "
                "booked_net = booked_blue_gross + booked_red_gross（红字号为负，冲销后净额守恒）; "
                "ledger 仅含 verified 且未被重复导出拦截的发票"
            ),
        },
        "decisions": {
            "verified": by_decision[Decision.VERIFIED.value],
            "manual_review": by_decision[Decision.MANUAL_REVIEW.value],
            "rejected": by_decision[Decision.REJECTED.value],
            "counts": {d.value: len(by_decision[d.value]) for d in Decision},
        },
        "allocation": strategy_comparison,
        "batch_anomalies": [_anomaly_dict(a) for a in batch_anomalies],
        "conclusion_timeline": timeline,
        "ledger": [
            {
                "invoice_number": number,
                "conclusion_id": entry["conclusion_id"],
                "run_id": entry["run_id"],
                "booked_at": entry["booked_at"],
                "amounts": entry["amounts"],
            }
            for number, entry in sorted(state["ledger"].items())
        ],
        "idempotent_skips": skipped_idempotent,
        "evidence_chain": _evidence_chain(invoices, payments, state),
        # ---- 基线兼容字段 ----
        "gross_total": from_cents(gross_total),
        "exceptions": _legacy_exceptions(by_decision),
        "paid_numbers": [p.invoice_number for p in payments if p.invoice_number],
    }
    return envelope


def _money(cents: int) -> Dict[str, Any]:
    return {"cents": cents, "text": from_cents(cents)}


def _text_to_cents(text: str) -> int:
    from decimal import Decimal
    return int(Decimal(text).scaleb(2))


def _legacy_exceptions(by_decision: Dict[str, list]) -> List[Dict[str, str]]:
    out = []
    for bucket in (Decision.MANUAL_REVIEW.value, Decision.REJECTED.value):
        for item in by_decision[bucket]:
            for code in item["reason_codes"]:
                out.append({"number": item["invoice_number"], "reason": code.lower()})
    return out


def _evidence_chain(invoices, payments, state) -> List[Dict[str, Any]]:
    """每张发票的端到端证据链：文件哈希 → 认证 → 入账 → 付款 → 结论。"""
    pay_index = {}
    for p in payments:
        pay_index.setdefault(p.invoice_number, []).append(p)
    chain = []
    for inv in invoices:
        linked_payments = pay_index.get(inv.number, [])
        chain.append({
            "invoice_number": inv.number,
            "source_files": [
                {"source": e.source, "file_id": e.file_id, "hash": e.hash,
                 "observed_at": e.observed_at, "locator": e.locator}
                for e in inv.evidence
            ],
            "timeline": {
                "certified_at": inv.certified_at,
                "booked_at": inv.booked_at,
                "paid_at": [p.paid_at for p in linked_payments],
            },
            "latest_conclusion_id": state["invoice_latest"].get(inv.number),
            "ledger_booked": inv.number in state["ledger"],
        })
    return chain
