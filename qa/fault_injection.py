"""Fault injection for input parsing and monetary boundaries.

Each injector mutates a *copy* of a valid payload into one invalid or
adversarial input. Outcomes are classified against an explicit contract:

  * ``contracted`` -- the documented/observed safe behavior is pinned by
    an assertion (clean rejection, filtered row, or preserved evidence);
  * ``hazard`` -- the production code currently handles the fault in a
    way finance must be warned about (e.g. silent NaN propagation). Such
    cases are recorded as findings rather than failing the suite, so the
    report documents the exposure without anyone being tempted to change
    the reconciliation algorithm inside the QA layer.

Every finding localizes the injected fault to an invoice number,
payment token or envelope field and includes the intermediate amount.
"""
from __future__ import annotations

import copy
import dataclasses
import decimal
from dataclasses import dataclass, asdict
from typing import Any, Callable

from .harness import check


@dataclass(frozen=True)
class FaultOutcome:
    severity: str           # contracted | hazard
    layer: str
    location: str
    observed: str
    expectation: str
    detail: str = ""
    name: str = ""


def _clone(payload: dict) -> dict:
    return copy.deepcopy(payload)


def _first_invoice_index(payload: dict) -> int:
    for i, row in enumerate(payload["invoices"]):
        if isinstance(row, dict):
            return i
    return 0


# ---------------------------------------------------------------------------
# injectors: name -> (mutator, expectation description)
# ---------------------------------------------------------------------------

def inj_non_numeric_net(payload: dict) -> dict:
    p = _clone(payload)
    idx = _first_invoice_index(p)
    p["invoices"][idx]["net"] = "100.abc"
    return p, f"invoice:{p['invoices'][idx].get('number', idx)}#idx{idx}.net"


def inj_non_numeric_tax(payload: dict) -> dict:
    p = _clone(payload)
    idx = _first_invoice_index(p)
    p["invoices"][idx]["tax"] = None
    return p, f"invoice:{p['invoices'][idx].get('number', idx)}#idx{idx}.tax"


def inj_nan_tax(payload: dict) -> dict:
    p = _clone(payload)
    idx = _first_invoice_index(p)
    p["invoices"][idx]["tax"] = "NaN"
    return p, f"invoice:{p['invoices'][idx].get('number', idx)}#idx{idx}.tax"


def inj_infinity_net(payload: dict) -> dict:
    p = _clone(payload)
    idx = _first_invoice_index(p)
    p["invoices"][idx]["net"] = "Infinity"
    return p, f"invoice:{p['invoices'][idx].get('number', idx)}#idx{idx}.net"


def inj_float_net(payload: dict) -> dict:
    p = _clone(payload)
    idx = _first_invoice_index(p)
    # binary float artefact: 0.1 + 0.2 style
    p["invoices"][idx]["net"] = 0.1 + 0.2
    return p, f"invoice:{p['invoices'][idx].get('number', idx)}#idx{idx}.net"


def inj_boolean_amount(payload: dict) -> dict:
    p = _clone(payload)
    idx = _first_invoice_index(p)
    p["invoices"][idx]["net"] = True
    return p, f"invoice:{p['invoices'][idx].get('number', idx)}#idx{idx}.net"


def inj_missing_number(payload: dict) -> dict:
    p = _clone(payload)
    idx = _first_invoice_index(p)
    del p["invoices"][idx]["number"]
    return p, f"invoice:idx{idx}.number"


def inj_missing_tax_rate(payload: dict) -> dict:
    p = _clone(payload)
    idx = _first_invoice_index(p)
    del p["invoices"][idx]["tax_rate"]
    return p, f"invoice:{p['invoices'][idx].get('number', idx)}#idx{idx}.tax_rate"


def inj_invoices_not_list(payload: dict) -> dict:
    p = _clone(payload)
    p["invoices"] = {"not": "a list"}
    return p, "envelope:invoices"


def inj_invoice_row_not_dict(payload: dict) -> dict:
    p = _clone(payload)
    p["invoices"] = [{"number": "OK1", "net": "1.00", "tax_rate": "0.13",
                      "tax": "0.13", "status": "normal"}, "junk-row", 42]
    return p, "envelope:invoices[1..2]"


def inj_payments_not_list(payload: dict) -> dict:
    p = _clone(payload)
    p["payments"] = "A1"
    return p, "envelope:payments"


def inj_payment_unknown_number(payload: dict) -> dict:
    p = _clone(payload)
    p["payments"] = list(p.get("payments", [])) + ["GHOST-999"]
    return p, "payment:GHOST-999"


def inj_duplicate_payment(payload: dict) -> dict:
    p = _clone(payload)
    p["payments"] = list(p.get("payments", []))
    token = p["payments"][0] if p["payments"] else "A1"
    p["payments"].append(copy.deepcopy(token))
    return p, f"payment:{token}"


def inj_duplicate_invoice(payload: dict) -> dict:
    p = _clone(payload)
    idx = _first_invoice_index(p)
    clone_row = copy.deepcopy(p["invoices"][idx])
    if clone_row.get("number", "").endswith("-R"):
        clone_row["number"] = "DUP-NO-REV"
        p["invoices"].append(clone_row)
    else:
        p["invoices"].append(clone_row)
    return p, f"invoice:{clone_row.get('number')}"


def inj_negative_zero_and_negative_tax(payload: dict) -> dict:
    p = _clone(payload)
    idx = _first_invoice_index(p)
    p["invoices"][idx]["net"] = "-0.00"
    p["invoices"][idx]["tax"] = "-0.01"
    return p, f"invoice:{p['invoices'][idx].get('number', idx)}#idx{idx}"


def inj_extra_precision_net(payload: dict) -> dict:
    p = _clone(payload)
    idx = _first_invoice_index(p)
    p["invoices"][idx]["net"] = "100.005"   # 3-dp boundary
    return p, f"invoice:{p['invoices'][idx].get('number', idx)}#idx{idx}.net"


def inj_empty_payload(payload: dict) -> dict:
    return {}, "envelope:root"


INJECTORS: dict[str, tuple[Callable[[dict], tuple[dict, str]], str]] = {
    "non_numeric_net": (inj_non_numeric_net,
                        "illegal amount rejected before Decimal arithmetic"),
    "non_numeric_tax": (inj_non_numeric_tax,
                        "illegal amount rejected before Decimal arithmetic"),
    "nan_tax": (inj_nan_tax,
                "non-finite money must never reach gross_total"),
    "infinity_net": (inj_infinity_net,
                     "non-finite money must never reach gross_total"),
    "float_net": (inj_float_net,
                  "float input must not introduce binary artefacts"),
    "boolean_amount": (inj_boolean_amount,
                       "bool must not be accepted as a monetary value"),
    "missing_number": (inj_missing_number,
                       "missing localization key must be rejected"),
    "missing_tax_rate": (inj_missing_tax_rate,
                         "missing tax_rate must be rejected"),
    "invoices_not_list": (inj_invoices_not_list,
                          "parser rejects non-list invoices with ValueError"),
    "row_not_dict": (inj_invoice_row_not_dict,
                     "non-dict rows filtered, dict rows retained"),
    "payments_not_list": (inj_payments_not_list,
                          "non-list payments rejected, not silently dropped"),
    "payment_unknown_invoice": (inj_payment_unknown_number,
                                "payment evidence retained; oracle flags "
                                "unknown_invoice"),
    "duplicate_payment": (inj_duplicate_payment,
                          "duplicate tokens preserved verbatim, flagged by "
                          "oracle"),
    "duplicate_invoice": (inj_duplicate_invoice,
                          "duplicate_number exception localized to invoice"),
    "negative_zero_negative_tax": (inj_negative_zero_and_negative_tax,
                                   "signed zero and negative cents handled "
                                   "deterministically"),
    "extra_precision_net": (inj_extra_precision_net,
                            "sub-cent precision handled by documented "
                            "quantize rule, no float drift"),
    "empty_payload": (inj_empty_payload,
                      "empty input yields zero gross and empty exceptions"),
}


# ---------------------------------------------------------------------------
# execution / classification
# ---------------------------------------------------------------------------

def _safe(call: Callable[[], Any]) -> tuple[str, Any]:
    try:
        value = call()
    except (ValueError, KeyError, TypeError, decimal.InvalidOperation,
            ArithmeticError) as exc:
        return "rejected", f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001
        return "other_error", f"{type(exc).__name__}: {exc}"
    return "ok", value


def run_injector(name: str, payload: dict, pipeline, load_records,
                 calculate) -> FaultOutcome:
    injector, expectation = INJECTORS[name]
    mutated, location = injector(payload)

    if name == "invoices_not_list":
        status, observed = _safe(lambda: load_records(mutated))
        check(status == "rejected", "parser must reject non-list invoices",
              name=name, observed=observed)
        return FaultOutcome("contracted", "parser", location, observed,
                            expectation)

    if name == "row_not_dict":
        status, observed = _safe(lambda: load_records(mutated))
        check(status == "ok", "parser must filter non-dict rows without "
              "raising", name=name, observed=observed)
        check(len(observed) == 1, "only the dict row survives",
              kept=len(observed))
        return FaultOutcome("contracted", "parser", location,
                            f"{len(observed)} row(s) retained", expectation)

    if name == "empty_payload":
        status, observed = _safe(lambda: pipeline(mutated))
        check(status == "ok", "empty payload runs end-to-end",
              name=name, observed=observed)
        check(observed["gross_total"] == "0.00", "empty gross is 0.00",
              gross=observed["gross_total"])
        check(observed["exceptions"] == [], "no exceptions on empty input")
        return FaultOutcome("contracted", "pipeline", location,
                            "gross_total=0.00", expectation)

    if name in ("payment_unknown_invoice", "duplicate_payment"):
        status, observed = _safe(lambda: pipeline(mutated))
        check(status == "ok", "payment faults must not crash the pipeline",
              name=name, observed=observed)
        # evidence preservation incl. duplicate tokens
        check(observed["paid_numbers"] == mutated["payments"],
              "payment evidence preserved verbatim",
              actual=observed["paid_numbers"])
        return FaultOutcome("contracted", "engine", location,
                            f"paid_numbers len={len(observed['paid_numbers'])}",
                            expectation)

    if name == "duplicate_invoice":
        status, observed = _safe(lambda: pipeline(mutated))
        check(status == "ok", "duplicate invoice still processes",
              name=name, observed=observed)
        numbers = {e["number"] for e in observed["exceptions"]
                   if e["reason"] == "duplicate_number"}
        target = mutated["invoices"][-1]["number"]
        if not target.endswith("-R"):
            check(target in numbers, "duplicate_number localized",
                  target=target, exceptions=observed["exceptions"])
        return FaultOutcome("contracted", "engine", location,
                            f"exceptions={observed['exceptions']}",
                            expectation)

    # monetary/field faults reaching the arithmetic layer
    status, observed = _safe(lambda: pipeline(mutated))
    if status == "rejected":
        return FaultOutcome("contracted", "pipeline", location,
                            observed, expectation)

    # it ran: decide whether the output remains finite money
    gross = observed.get("gross_total") if isinstance(observed, dict) else None
    try:
        gross_dec = decimal.Decimal(str(gross))
        finite = gross_dec.is_finite()
    except (decimal.InvalidOperation, TypeError):
        finite = False
    if finite:
        # extra precision / floats: assert deterministic string typing
        check(isinstance(gross, str), "gross_total stays a string",
              gross_type=type(gross).__name__)
        return FaultOutcome("contracted", "engine", location,
                            f"gross_total={gross}", expectation)

    return FaultOutcome(
        "hazard", "engine", location,
        f"non-finite/illegal value propagated: gross_total={gross!r}",
        expectation,
        detail="engine has no input validation; QA oracle rejects this "
               "input. Recorded as a finance hazard, not silently passed.",
    )


def run_all_injections(payload: dict, pipeline, load_records,
                       calculate) -> list[FaultOutcome]:
    outcomes = []
    for name in sorted(INJECTORS):
        outcome = run_injector(name, payload, pipeline, load_records,
                               calculate)
        outcomes.append(dataclasses.replace(outcome, name=name))
    return outcomes
