"""输入解析与校验。

解析器负责把外部 JSON 载荷转换为不可变领域对象，并完成：

- 字段级校验，任何错误都携带 ``field_path``（如 ``invoices[2].lines[1].tax``）；
- 证据引用哈希校验；
- 按发票声明的 ``rule_version``（缺省取注册表默认版本）冻结税率规则，
  逐行计算预期税额，保留“票面税额 vs 预期税额”的完整计算过程；
- 汇总行合计与发票头金额的守恒检查由 engine/pipeline 层生成异常，
  解析器只负责结构化，不做最终裁决。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Tuple

from .hashing import hash_evidence_refs
from .models import EvidenceRef, Invoice, LineItem, Payment
from .money import MoneyError, parse_cents
from .taxrules import DEFAULT_RULE_VERSION, RuleVersionMissing, expected_line_tax, get_rule


class ParseError(ValueError):
    def __init__(self, field_path: str, message: str):
        super().__init__(f"{field_path}: {message}")
        self.field_path = field_path
        self.message = message


def _require(cond: bool, field_path: str, message: str) -> None:
    if not cond:
        raise ParseError(field_path, message)


def _evidence(raw: Any, field_path: str) -> Tuple[EvidenceRef, ...]:
    if raw is None:
        return ()
    _require(isinstance(raw, list), field_path, "evidence 必须是数组")
    refs = hash_evidence_refs(raw)
    out: List[EvidenceRef] = []
    for i, item in enumerate(refs):
        p = f"{field_path}[{i}]"
        _require(isinstance(item.get("source"), str) and item["source"], p, "缺少 source")
        _require(isinstance(item.get("file_id"), str) and item["file_id"], p, "缺少 file_id")
        out.append(EvidenceRef(
            source=item["source"],
            file_id=item["file_id"],
            hash=item.get("hash"),
            observed_at=item.get("observed_at"),
            locator=item.get("locator"),
        ))
    return tuple(out)


def _parse_lines(raw_invoice: dict, fp: str, rule) -> Tuple[List[LineItem], int, int]:
    raw_lines = raw_invoice.get("lines")
    lines: List[LineItem] = []
    if raw_lines is None:
        # 兼容无明细行的简表：单行承载整票金额
        raw_lines = [{
            "description": raw_invoice.get("description", raw_invoice["number"]),
            "rate_code": raw_invoice.get("rate_code", "goods"),
            "net": raw_invoice["net"],
            "tax": raw_invoice["tax"],
        }]
    _require(isinstance(raw_lines, list) and raw_lines, f"{fp}.lines", "明细行必须是非空数组")
    sum_net = sum_tax = 0
    for j, raw in enumerate(raw_lines):
        lp = f"{fp}.lines[{j}]"
        _require(isinstance(raw, dict), lp, "明细行必须是对象")
        for key in ("net", "tax"):
            _require(key in raw, lp, f"缺少 {key}")
        rate_code = raw.get("rate_code", "goods")
        _require(isinstance(rate_code, str), f"{lp}.rate_code", "rate_code 必须是字符串")
        try:
            net_c = parse_cents(raw["net"], field_path=f"{lp}.net")
            tax_c = parse_cents(raw["tax"], field_path=f"{lp}.tax")
        except MoneyError as exc:
            raise ParseError(lp, str(exc))
        try:
            expected = expected_line_tax(rule, Decimal(str(raw["net"])), rate_code)
        except Exception as exc:  # 未知税率代码等
            raise ParseError(f"{lp}.rate_code", str(exc))
        gross_c = net_c + tax_c
        sum_net += net_c
        sum_tax += tax_c
        lines.append(LineItem(
            line_no=j + 1,
            description=str(raw.get("description", "")),
            rate_code=rate_code,
            net_cents=net_c,
            tax_cents=tax_c,
            expected_tax_cents=int(expected.scaleb(2)),
            gross_cents=gross_c,
            field_path=lp,
            evidence=_evidence(raw.get("evidence"), f"{lp}.evidence"),
        ))
    return lines, sum_net, sum_tax


def parse_invoice(raw: dict, index: int) -> Invoice:
    fp = f"invoices[{index}]"
    _require(isinstance(raw, dict), fp, "发票必须是对象")
    for key in ("number", "net", "tax"):
        _require(key in raw, fp, f"缺少字段 {key}")
    number = raw["number"]
    _require(isinstance(number, str) and number.strip(), f"{fp}.number", "发票号必须是非空字符串")
    kind = raw.get("status", raw.get("kind", "normal"))
    _require(kind in ("normal", "red"), f"{fp}.status", "status 只能是 normal 或 red")
    rule_version = raw.get("rule_version") or DEFAULT_RULE_VERSION
    try:
        rule = get_rule(rule_version)
    except RuleVersionMissing as exc:
        raise ParseError(f"{fp}.rule_version", str(exc))

    lines, sum_net, sum_tax = _parse_lines(raw, fp, rule)
    header_net = parse_cents(raw["net"], field_path=f"{fp}.net")
    header_tax = parse_cents(raw["tax"], field_path=f"{fp}.tax")

    # 红字发票允许整体为负；蓝票禁止负数头金额
    if kind == "normal":
        _require(header_net >= 0 and header_tax >= 0, fp, "蓝字发票金额不能为负")
    else:
        _require(header_net <= 0 and header_tax <= 0, fp, "红字发票金额必须为负")

    red_links = raw.get("red_links") or raw.get("reverses") or []
    _require(isinstance(red_links, list), f"{fp}.red_links", "red_links 必须是数组")
    _require(all(isinstance(x, str) and x for x in red_links), f"{fp}.red_links", "red_links 必须是非空字符串数组")

    return Invoice(
        number=number,
        kind=kind,
        counterparty=raw.get("counterparty"),
        fiscal_period=raw.get("fiscal_period"),
        rule_version=rule_version,
        lines=tuple(lines),
        net_cents=header_net,
        tax_cents=header_tax,
        gross_cents=header_net + header_tax,
        status=kind,
        certified_at=raw.get("certified_at"),
        booked_at=raw.get("booked_at"),
        paid_at=raw.get("paid_at"),
        evidence=_evidence(raw.get("evidence"), f"{fp}.evidence"),
        field_path=fp,
        red_links=tuple(red_links),
        export_id=raw.get("export_id"),
        source_hash=raw.get("source_hash"),
        manual_correction_of=raw.get("manual_correction_of"),
    ), (sum_net, sum_tax)


def parse_payment(raw: dict, index: int) -> Payment:
    fp = f"payments[{index}]"
    _require(isinstance(raw, dict), fp, "付款必须是对象")
    pid = raw.get("payment_id") or raw.get("id")
    _require(isinstance(pid, str) and pid, f"{fp}.payment_id", "缺少 payment_id")
    amount_c = parse_cents(raw["amount"], field_path=f"{fp}.amount")
    _require(amount_c >= 0, f"{fp}.amount", "付款金额不能为负")
    return Payment(
        payment_id=pid,
        invoice_number=raw.get("invoice_number") or raw.get("invoice"),
        amount_cents=amount_c,
        paid_at=raw.get("paid_at"),
        fiscal_period=raw.get("fiscal_period"),
        evidence=_evidence(raw.get("evidence"), f"{fp}.evidence"),
        field_path=fp,
    )


def load_records(payload: dict) -> List[dict]:
    """向后兼容基线接口：返回原始发票 dict 列表。"""
    rows = payload.get("invoices", [])
    if not isinstance(rows, list):
        raise ParseError("invoices", "invoices 必须是数组")
    return [dict(row) for row in rows if isinstance(row, dict)]


def parse_payload(payload: dict) -> Dict[str, Any]:
    """完整解析。返回 invoices/payments/声明的 rule 版本与行合计校验数据。"""
    _require(isinstance(payload, dict), "$", "载荷必须是 JSON 对象")
    raw_invoices = payload.get("invoices", [])
    _require(isinstance(raw_invoices, list), "invoices", "invoices 必须是数组")
    raw_payments = payload.get("payments", [])
    _require(isinstance(raw_payments, list), "payments", "payments 必须是数组")

    invoices: List[Invoice] = []
    line_totals: Dict[str, Tuple[int, int]] = {}
    for i, raw in enumerate(raw_invoices):
        invoice, totals = parse_invoice(raw, i)
        invoices.append(invoice)
        line_totals[invoice.number] = totals

    payments: List[Payment] = []
    for i, raw in enumerate(raw_payments):
        # 兼容基线 fixture：payments 是发票号字符串数组
        if isinstance(raw, str):
            raw = {"payment_id": f"PAY-{raw}", "invoice_number": raw, "amount": "0.00"}
        payments.append(parse_payment(raw, i))

    return {
        "invoices": invoices,
        "payments": payments,
        "line_totals": line_totals,
        "declared_rule_version": payload.get("rule_version", DEFAULT_RULE_VERSION),
        "fiscal_period": payload.get("fiscal_period"),
    }
