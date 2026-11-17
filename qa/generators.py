"""Domain value generators for invoice/payment payloads.

All monetary generators are cent-exact: a value is chosen as an integer
number of cents and rendered as a 2-dp decimal string, so the generated
valid corpus never contains binary-float artefacts.

The generators deliberately over-sample boundary conditions that matter
for financial reconciliation:
    * zero amounts, negative adjustments and reversal documents (-R)
    * half-cent tax products (where rounding mode is visible)
    * duplicate invoice numbers and duplicate payments
    * payments to unknown invoice numbers (overpayment evidence)
    * shuffled input order
"""
from __future__ import annotations

import decimal
import random

from .harness import (
    Booleans, FixedDict, Integers, Lists, SampledFrom, Strategy,
)

D001 = decimal.Decimal("0.01")

# Common tax rates; 0.06 makes B1-style cases (50*0.06=3.00 exact),
# while 0.13/0.07/0.11 produce many half-cent rounding boundaries.
TAX_RATES = ["0.00", "0.05", "0.06", "0.07", "0.10", "0.11", "0.13", "0.19", "0.20"]

# cent values that sit on or near rounding boundaries when multiplied by
# common rates; deliberately includes zero and negatives.
EDGE_CENTS = [
    0, 1, -1, 2, -2, 5, -5, 10, -10, 25, -25, 50, -50,
    100, -100, 101, -101, 125, -125, 500, -500, 1000, -1000,
    333, -333, 777, -777, 999, -999, 10000, -10000,
]


def cents_to_text(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


class MoneyCents(Strategy):
    """Cent-exact monetary amount rendered as a decimal string."""

    def __init__(self, lo: int = -50000, hi: int = 50000,
                 edge_weight: float = 0.65):
        self.lo, self.hi, self.edge_weight = lo, hi, edge_weight

    def generate(self, rnd: random.Random) -> str:
        if rnd.random() < self.edge_weight:
            pool = [c for c in EDGE_CENTS if self.lo <= c <= self.hi]
            cents = rnd.choice(pool) if pool else rnd.randint(self.lo, self.hi)
        else:
            cents = rnd.randint(self.lo, self.hi)
        return cents_to_text(cents)

    def shrink(self, value: str):
        try:
            cents = int(decimal.Decimal(value) * 100)
        except (decimal.InvalidOperation, ValueError):
            return
        seen = {value}
        for target in (0, 1, -1, 100, -100):
            if self.lo <= target <= self.hi:
                text = cents_to_text(target)
                if text not in seen:
                    seen.add(text)
                    yield text
        if cents and self.lo <= cents // 2 <= self.hi:
            text = cents_to_text(cents // 2)
            if text not in seen:
                yield text


class TaxRate(Strategy):
    def __init__(self, rates=None):
        self.rates = rates or TAX_RATES

    def generate(self, rnd: random.Random) -> str:
        return rnd.choice(self.rates)

    def shrink(self, value: str):
        if value in self.rates:
            idx = self.rates.index(value)
            for j in range(idx):
                yield self.rates[j]
        yield "0.00"


class InvoiceNumber(Strategy):
    """Invoice numbers including reversal (-R) variants."""

    def __init__(self, max_base: int = 8):
        self.max_base = max_base

    def generate(self, rnd: random.Random) -> str:
        base = chr(ord("A") + rnd.randint(0, self.max_base - 1)) + str(
            rnd.randint(1, 9)
        )
        return base + ("-R" if rnd.random() < 0.3 else "")

    def shrink(self, value: str):
        for simpler in ("A1", "A1-R", "B1"):
            if simpler != value:
                yield simpler


class InvoiceRow(FixedDict):
    def __init__(self, number_strategy: Strategy | None = None,
                 money: MoneyCents | None = None,
                 rate: TaxRate | None = None):
        super().__init__({
            "number": number_strategy or InvoiceNumber(),
            "net": money or MoneyCents(),
            "tax_rate": rate or TaxRate(),
            "tax": money or MoneyCents(),
            "status": SampledFrom(["normal", "red"]),
        })


class PaymentToken(Strategy):
    """Payment entries: plain invoice numbers or {number, amount} maps."""

    def __init__(self, number_pool: list[str] | None = None):
        self.pool = number_pool or ["A1", "A1-R", "B1", "C2", "ZZ9"]

    def generate(self, rnd: random.Random) -> object:
        number = rnd.choice(self.pool)
        if rnd.random() < 0.5:
            return number
        return {"number": number, "amount": MoneyCents(0, 50000).generate(rnd)}

    def shrink(self, value):
        if isinstance(value, dict):
            yield value["number"]
        for simple in ("A1", "B1"):
            if simple != value:
                yield simple


class Payload(Strategy):
    """Full input payload with controllable duplicates and ordering.

    ``generate`` builds an invoice list and then optionally:
      * reuses an existing number (duplicate invoice, non -R or -R)
      * reuses a payment token (duplicate payment)
      * shuffles both lists (input reordering)
    """

    def __init__(self, max_invoices: int = 7, max_payments: int = 7,
                 allow_duplicates: bool = True):
        self.max_invoices = max_invoices
        self.max_payments = max_payments
        self.allow_duplicates = allow_duplicates

    def generate(self, rnd: random.Random) -> dict:
        n_inv = rnd.randint(1, self.max_invoices)
        number_strat = InvoiceNumber()
        money = MoneyCents()
        rate = TaxRate()
        rows = [
            InvoiceRow(number_strat, money, rate).generate(rnd)
            for _ in range(n_inv)
        ]
        if self.allow_duplicates and len(rows) > 1 and rnd.random() < 0.6:
            victim = rnd.randrange(len(rows))
            clone = dict(rows[victim])
            # 50/50 exact duplicate vs same number different amounts
            if rnd.random() < 0.5:
                clone["net"] = money.generate(rnd)
                clone["tax"] = money.generate(rnd)
            insert_at = rnd.randrange(len(rows) + 1)
            rows.insert(insert_at, clone)
        numbers = [r["number"] for r in rows]
        pay_strat = PaymentToken(numbers + ["UNKNOWN-" + str(i) for i in range(2)])
        payments = [pay_strat.generate(rnd) for _ in range(rnd.randint(0, self.max_payments))]
        if self.allow_duplicates and payments and rnd.random() < 0.5:
            payments.append(rnd.choice(payments))
        rnd.shuffle(rows)
        rnd.shuffle(payments)
        return {"invoices": rows, "payments": payments,
                "project_marker": rnd.randint(0, 1_000_000)}

    def shrink(self, value: dict):
        rows = value.get("invoices", [])
        # drop invoices
        if len(rows) > 1:
            for i in range(len(rows)):
                smaller = dict(value)
                smaller["invoices"] = rows[:i] + rows[i + 1:]
                yield smaller
        # shrink each field of each invoice
        for i, row in enumerate(rows):
            for field_name, strat in (
                ("net", MoneyCents()), ("tax", MoneyCents()),
                ("tax_rate", TaxRate()), ("number", InvoiceNumber()),
            ):
                if field_name not in row:
                    continue
                for shrunk in strat.shrink(row[field_name]):
                    candidate = dict(value)
                    new_rows = [dict(r) for r in rows]
                    new_rows[i][field_name] = shrunk
                    candidate["invoices"] = new_rows
                    yield candidate
        # drop payments
        payments = value.get("payments", [])
        if payments:
            for i in range(len(payments)):
                smaller = dict(value)
                smaller["payments"] = payments[:i] + payments[i + 1:]
                yield smaller


# ---------------------------------------------------------------------------
# Hand-built boundary scenarios (used by snapshots and as shrink seeds)
# ---------------------------------------------------------------------------

def half_cent_payload() -> dict:
    """1.01 net * 0.13 = 0.1313 -> half-up 0.13; declared tax varies.

    Includes a true half-cent case: 1.05 * 0.13 = 0.1365 -> 0.14.
    """
    return {
        "invoices": [
            {"number": "H1", "net": "1.05", "tax_rate": "0.13",
             "tax": "0.14", "status": "normal"},
            {"number": "H2", "net": "1.01", "tax_rate": "0.13",
             "tax": "0.13", "status": "normal"},
            {"number": "H3", "net": "0.25", "tax_rate": "0.06",
             "tax": "0.02", "status": "normal"},  # 0.015 -> 0.02 half up
        ],
        "payments": ["H1", "H1", {"number": "H2", "amount": "0.50"}],
    }


def zero_and_negative_payload() -> dict:
    return {
        "invoices": [
            {"number": "Z1", "net": "0.00", "tax_rate": "0.13",
             "tax": "0.00", "status": "normal"},
            {"number": "N1", "net": "-120.55", "tax_rate": "0.07",
             "tax": "-8.44", "status": "red"},  # -8.4385 -> -8.44 half up
            {"number": "N1-R", "net": "-120.55", "tax_rate": "0.07",
             "tax": "-8.44", "status": "red"},
        ],
        "payments": [],
    }


def multi_invoice_partial_payment_payload() -> dict:
    return {
        "invoices": [
            {"number": "M1", "net": "100.00", "tax_rate": "0.13",
             "tax": "13.00", "status": "normal"},
            {"number": "M2", "net": "200.00", "tax_rate": "0.19",
             "tax": "38.00", "status": "normal"},
            {"number": "M3", "net": "50.00", "tax_rate": "0.06",
             "tax": "3.00", "status": "normal"},
        ],
        "payments": [
            {"number": "M1", "amount": "50.00"},   # partial
            {"number": "M2", "amount": "999.99"},  # overpayment
            {"number": "GHOST", "amount": "7.00"}, # unknown invoice
            "M3", "M3",                               # duplicate plain token
        ],
    }
