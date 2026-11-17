"""Independent monetary reference model (test oracle).

This module re-derives every monetary fact of an invoice/payment payload
from first principles using :class:`decimal.Decimal`. It deliberately
shares no code with ``src/evidence_invoice_reconcile`` so that any
coincidence of bugs is impossible: a differential comparison between
this oracle and the production pipeline can only fail because one side
diverged from the documented arithmetic.

Two outputs are produced:

1. ``oracle_envelope`` -- the values the production engine *contracts*
   to provide (``gross_total`` / ``exceptions`` / ``paid_numbers``),
   recomputed independently, used for differential snapshots.
2. ``audit`` -- the full reference view required by finance: per-invoice
   net/tax/gross with intermediate amounts, rounding-level comparison,
   payment allocation with paid/partial/overpaid/unpaid status and
   duplicate-payment evidence, localized by invoice/payment identifier.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Any

CENT = Decimal("0.01")
REVERSAL_SUFFIX = "-R"


def _d(value: Any) -> Decimal:
    """Parse a money/rate value exactly; raise on illegal input."""
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        raise ValueError(f"illegal monetary value: {value!r}")
    if value is None:
        raise ValueError("illegal monetary value: None")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"illegal monetary value: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"non-finite monetary value: {value!r}")
    return result


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def money_text(value: Decimal) -> str:
    return str(money(value))


@dataclass(frozen=True)
class LineAudit:
    number: str
    index: int
    net: str
    tax_rate: str
    declared_tax: str
    expected_tax_line_rounded: str          # round(net * rate) per line
    raw_tax_product: str                     # unrounded product
    expected_tax_summary_rounded: str       # tax if rounding only at total
    rounding_delta: str                      # declared - line-rounded
    gross: str                              # net + declared tax
    is_reversal: bool
    findings: tuple = ()


@dataclass(frozen=True)
class PaymentAudit:
    token_index: int
    number: str
    requested_amount: str | None            # None => plain token, full claim
    is_duplicate: bool
    invoice_exists: bool


@dataclass(frozen=True)
class AllocationAudit:
    number: str
    invoice_gross: str
    allocated: str
    remaining: str
    status: str                             # paid|partial|overpaid|unpaid
    payment_count: int
    duplicate_payments: int


@dataclass(frozen=True)
class AuditReport:
    lines: list
    payments: list
    allocations: list
    gross_total: str
    gross_from_line_rounded_tax: str
    gross_from_summary_rounded_tax: str
    rounding_level: str                     # "line" | "summary" | "indeterminate"
    net_total: str
    tax_total_declared: str
    conservation_residual: str              # gross - (net + tax), must be 0.00
    duplicate_invoice_numbers: tuple
    duplicate_payment_keys: tuple
    unknown_payment_numbers: tuple
    exceptions: list
    diffs: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# invoice side
# ---------------------------------------------------------------------------

def audit_lines(rows: list) -> tuple[list[LineAudit], list[dict]]:
    lines: list[LineAudit] = []
    exceptions: list[dict] = []
    seen: dict[str, int] = {}
    for index, row in enumerate(rows):
        number = str(row["number"])
        seen[number] = seen.get(number, 0) + 1
        net = _d(row["net"])
        rate = _d(row["tax_rate"])
        declared_tax = _d(row["tax"])
        raw_product = net * rate
        expected_line = money(raw_product)
        findings = []
        if expected_line != declared_tax:
            findings.append("tax_mismatch")
            exceptions.append({"number": number, "reason": "tax_mismatch"})
        # Summary-level variant: same product, only quantized when the
        # whole tax column is totaled (see audit_totals).
        lines.append(LineAudit(
            number=number,
            index=index,
            net=money_text(net),
            tax_rate=str(rate),
            declared_tax=money_text(declared_tax),
            expected_tax_line_rounded=money_text(expected_line),
            raw_tax_product=str(raw_product),
            expected_tax_summary_rounded=money_text(raw_product),
            rounding_delta=money_text(declared_tax - expected_line),
            gross=money_text(net + declared_tax),
            is_reversal=number.endswith(REVERSAL_SUFFIX),
            findings=tuple(findings),
        ))
    for number, count in seen.items():
        if count > 1 and not number.endswith(REVERSAL_SUFFIX):
            exceptions.append({"number": number, "reason": "duplicate_number"})
    return lines, exceptions


def audit_totals(lines: list[LineAudit]) -> dict:
    """Aggregate with both rounding policies so the report can prove
    where the production engine rounds."""
    net_sum = Decimal("0")
    tax_declared_sum = Decimal("0")
    line_rounded_sum = Decimal("0")
    raw_product_sum = Decimal("0")
    gross_sum = Decimal("0")
    for line in lines:
        net_sum += Decimal(line.net)
        tax_declared_sum += Decimal(line.declared_tax)
        line_rounded_sum += Decimal(line.expected_tax_line_rounded)
        raw_product_sum += Decimal(line.raw_tax_product)
        gross_sum += Decimal(line.gross)
    summary_rounded_tax = money(raw_product_sum)
    gross_line = money(net_sum + line_rounded_sum)
    gross_summary = money(net_sum + summary_rounded_tax)
    gross_declared = money(gross_sum)
    # conservation identity: gross == net + declared tax to the cent
    residual = money(gross_declared - (money(net_sum) + money(tax_declared_sum)))
    if gross_declared == gross_line:
        level = "line"
    elif gross_declared == gross_summary:
        level = "summary"
    else:
        level = "indeterminate"
    return {
        "gross_total": money_text(gross_declared),
        "gross_from_line_rounded_tax": money_text(gross_line),
        "gross_from_summary_rounded_tax": money_text(gross_summary),
        "net_total": money_text(net_sum),
        "tax_total_declared": money_text(tax_declared_sum),
        "conservation_residual": money_text(residual),
        "rounding_level": level,
        "summary_rounded_tax": money_text(summary_rounded_tax),
        "line_rounded_tax": money_text(line_rounded_sum),
    }


# ---------------------------------------------------------------------------
# payment side
# ---------------------------------------------------------------------------

def _payment_key(entry: Any) -> tuple[str, str | None]:
    if isinstance(entry, dict):
        return str(entry["number"]), str(entry.get("amount"))
    return str(entry), None


def audit_payments(payload: dict, rows: list, lines: list) -> tuple[
        list[PaymentAudit], list[AllocationAudit], tuple, tuple]:
    payments = payload.get("payments", [])
    if not isinstance(payments, list):
        raise ValueError("payments must be a list")
    known_numbers = {line.number for line in lines}
    seen_keys: dict[tuple, int] = {}
    payment_audits: list[PaymentAudit] = []
    requested: dict[str, Decimal] = {}
    counts: dict[str, int] = {}
    duplicate_keys: set[tuple] = set()
    for index, entry in enumerate(payments):
        number, amount_text = _payment_key(entry)
        key = (number, amount_text)
        seen_keys[key] = seen_keys.get(key, 0) + 1
        if seen_keys[key] > 1:
            duplicate_keys.add(key)
        counts[number] = counts.get(number, 0) + 1
        if amount_text is None:
            # plain token: claim against the full invoice gross
            amount = next(
                (Decimal(line.gross) for line in lines if line.number == number),
                Decimal("0.00"),
            )
        else:
            amount = money(_d(amount_text))
        requested[number] = requested.get(number, Decimal("0.00")) + amount
        payment_audits.append(PaymentAudit(
            token_index=index,
            number=number,
            requested_amount=None if amount_text is None else money_text(amount),
            is_duplicate=seen_keys[key] > 1,
            invoice_exists=number in known_numbers,
        ))
    gross_by_number = {line.number: Decimal(line.gross) for line in lines}
    allocations: list[AllocationAudit] = []
    for number in sorted(known_numbers):
        gross = gross_by_number[number]
        allocated = money(requested.get(number, Decimal("0.00")))
        remaining = money(gross - allocated)
        if allocated == 0:
            status = "unpaid"
        elif allocated < gross:
            status = "partial"
        elif allocated == gross:
            status = "paid"
        else:
            status = "overpaid"
        dup = sum(1 for key, count in seen_keys.items()
                  if key[0] == number and count > 1)
        allocations.append(AllocationAudit(
            number=number,
            invoice_gross=money_text(gross),
            allocated=money_text(allocated),
            remaining=money_text(remaining),
            status=status,
            payment_count=counts.get(number, 0),
            duplicate_payments=dup,
        ))
    unknown = tuple(sorted({
        p.number for p in payment_audits if not p.invoice_exists
    }))
    # keys may contain None (plain token) vs str (amount token): sort
    # on a type-stable key
    sorted_dup = tuple(
        sorted(duplicate_keys, key=lambda k: (k[0], "" if k[1] is None
                                              else k[1], k[1] is None))
    )
    return payment_audits, allocations, sorted_dup, unknown


# ---------------------------------------------------------------------------
# top level
# ---------------------------------------------------------------------------

def audit(payload: dict) -> AuditReport:
    rows = payload.get("invoices", [])
    if not isinstance(rows, list):
        raise ValueError("invoices must be a list")
    rows = [r for r in rows if isinstance(r, dict)]
    lines, exceptions = audit_lines(rows)
    totals = audit_totals(lines)
    payments, allocations, dup_pay_keys, unknown = audit_payments(
        payload, rows, lines
    )
    dup_invoice = tuple(sorted({
        line.number for line in lines
        if sum(1 for other in lines if other.number == line.number) > 1
        and not line.is_reversal
    }))
    diffs = []
    for line in lines:
        if line.findings:
            diffs.append({
                "location": f"invoice:{line.number}#idx{line.index}",
                "field": "tax",
                "declared": line.declared_tax,
                "expected": line.expected_tax_line_rounded,
                "delta": line.rounding_delta,
                "reasons": list(line.findings),
            })
    for alloc in allocations:
        if alloc.status in ("partial", "overpaid") or alloc.duplicate_payments:
            diffs.append({
                "location": f"invoice:{alloc.number}",
                "field": f"payment:{alloc.status}",
                "declared": alloc.allocated,
                "expected": alloc.invoice_gross,
                "delta": money_text(
                    Decimal(alloc.allocated) - Decimal(alloc.invoice_gross)
                ),
                "reasons": [alloc.status]
                          + (["duplicate_payment"] if alloc.duplicate_payments
                             else []),
            })
    for number in unknown:
        diffs.append({
            "location": f"payment:{number}",
            "field": "payment:unknown_invoice",
            "declared": None,
            "expected": None,
            "delta": None,
            "reasons": ["unknown_invoice"],
        })
    return AuditReport(
        lines=[asdict(line) for line in lines],
        payments=[asdict(p) for p in payments],
        allocations=[asdict(a) for a in allocations],
        gross_total=totals["gross_total"],
        gross_from_line_rounded_tax=totals["gross_from_line_rounded_tax"],
        gross_from_summary_rounded_tax=totals["gross_from_summary_rounded_tax"],
        rounding_level=totals["rounding_level"],
        net_total=totals["net_total"],
        tax_total_declared=totals["tax_total_declared"],
        conservation_residual=totals["conservation_residual"],
        duplicate_invoice_numbers=dup_invoice,
        duplicate_payment_keys=tuple(
            list(k) for k in dup_pay_keys
        ),
        unknown_payment_numbers=unknown,
        exceptions=exceptions,
        diffs=diffs,
    )


def oracle_envelope(payload: dict) -> dict:
    """Independent recomputation of the engine's contracted envelope."""
    report = audit(payload)
    return {
        "gross_total": report.gross_total,
        "exceptions": sorted(
            (dict(e) for e in report.exceptions),
            key=lambda e: (e["number"], e["reason"]),
        ),
        "paid_numbers": list(payload.get("payments", [])),
    }
