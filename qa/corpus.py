"""Deterministic shared corpus for mutation runs and regression checks.

The corpus blends hand-built boundary payloads (each one targets a
specific arithmetic or grouping rule) with a seeded random sample from
the property generators, so mutants cannot survive by special-casing the
few hand-written examples.
"""
from __future__ import annotations

import random
from copy import deepcopy

from .generators import (
    Payload, half_cent_payload, zero_and_negative_payload,
    multi_invoice_partial_payment_payload,
)


def _triple_duplicate_payload() -> dict:
    row = {"number": "D1", "net": "10.00", "tax_rate": "0.13",
           "tax": "1.30", "status": "normal"}
    return {
        "invoices": [deepcopy(row), deepcopy(row), deepcopy(row)],
        "payments": ["D1", "D1", "D1"],
    }


def _reversal_duplicate_payload() -> dict:
    rev = {"number": "R1-R", "net": "-10.00", "tax_rate": "0.13",
           "tax": "-1.30", "status": "red"}
    plain = {"number": "R1", "net": "10.00", "tax_rate": "0.13",
             "tax": "1.30", "status": "normal"}
    return {
        "invoices": [deepcopy(rev), deepcopy(rev), deepcopy(plain)],
        "payments": [],
    }


def _extra_precision_payload() -> dict:
    """Sub-cent inputs kept outside the zero-diff kill corpus.

    On 3-dp inputs the production engine accumulates the raw products
    and quantizes only the final total (113.01), while the per-line
    reference model quantizes each invoice gross first (113.02). This
    rounding-location difference is asserted explicitly in
    ``test_properties`` instead of being treated as noise.
    """
    return {
        "invoices": [
            {"number": "E1", "net": "100.00", "tax_rate": "0.13",
             "tax": "13.00", "status": "normal"},
            {"number": "E2", "net": "0.01", "tax_rate": "0.19",
             "tax": "0.00", "status": "normal"},
        ],
        "payments": [{"number": "E1", "amount": "50.00"}],
    }


def extra_precision_drift_payload() -> dict:
    """The actual 3-dp drift case, used by the rounding-level probe."""
    return {
        "invoices": [
            {"number": "E1", "net": "100.005", "tax_rate": "0.13",
             "tax": "13.00", "status": "normal"},
            {"number": "E2", "net": "0.005", "tax_rate": "0.19",
             "tax": "0.00", "status": "normal"},
        ],
        "payments": [],
    }


def _multi_mismatch_payload() -> dict:
    # mismatches in several positions plus a duplicate, to pin ordering
    return {
        "invoices": [
            {"number": "Q1", "net": "1.00", "tax_rate": "0.13",
             "tax": "9.99", "status": "normal"},
            {"number": "Q2", "net": "2.00", "tax_rate": "0.07",
             "tax": "8.88", "status": "normal"},
            {"number": "Q1", "net": "3.00", "tax_rate": "0.10",
             "tax": "0.30", "status": "normal"},
            {"number": "Q3", "net": "4.00", "tax_rate": "0.06",
             "tax": "7.77", "status": "normal"},
        ],
        "payments": ["Q2", {"number": "Q2", "amount": "1.00"}],
    }


def _non_dict_rows_payload() -> dict:
    return {
        "invoices": [
            {"number": "K1", "net": "1.00", "tax_rate": "0.13",
             "tax": "0.13", "status": "normal"},
            "scalar-row", 42, None,
        ],
        "payments": ["K1"],
    }


def build_corpus(seed: int = 20261117, random_count: int = 40) -> list[dict]:
    payloads = [
        half_cent_payload(),
        zero_and_negative_payload(),
        multi_invoice_partial_payment_payload(),
        _triple_duplicate_payload(),
        _reversal_duplicate_payload(),
        _extra_precision_payload(),
        _multi_mismatch_payload(),
        _non_dict_rows_payload(),
        {"invoices": [], "payments": []},
    ]
    rnd = random.Random(seed)
    strategy = Payload()
    payloads.extend(strategy.generate(rnd) for _ in range(random_count))
    return payloads
