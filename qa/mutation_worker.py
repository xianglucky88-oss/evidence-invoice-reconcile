"""Kill-suite executed inside a mutation worker subprocess.

Usage:
    python3 -I -m qa.mutation_worker <mutated_package_dir> <mid>

The worker imports the *mutated* copy of ``evidence_invoice_reconcile``
from ``mutated_package_dir`` and verifies it against the independent
oracle and the structural contracts on the deterministic corpus. Any
divergence, crash or contract violation kills the mutant.

Exit code 0 = survived, 1 = killed. A single JSON line is printed.
"""
from __future__ import annotations

import json
import re
import sys
import traceback
from decimal import Decimal


def main() -> int:
    package_dir, mid = sys.argv[1], sys.argv[2]
    # qa package lives under the workspace root
    workspace = __file__.rsplit("/qa/", 1)[0]
    sys.path.insert(0, package_dir)
    sys.path.insert(0, workspace)

    from qa.corpus import build_corpus
    from qa.oracle import oracle_envelope
    from qa.generators import Payload
    import random

    def fail(reason: str) -> int:
        print(json.dumps({"mid": mid, "status": "killed",
                          "reason": reason}, ensure_ascii=False))
        return 1

    try:
        from evidence_invoice_reconcile.parser import load_records
        from evidence_invoice_reconcile.engine import calculate
        from evidence_invoice_reconcile.report import build_result
        from evidence_invoice_reconcile.pipeline import run
    except BaseException as exc:  # import-time mutant kills itself
        return fail(f"import failure: {type(exc).__name__}: {exc}")

    corpus = build_corpus()
    money_re = re.compile(r"^-?\d+\.\d{2}$")

    def canonical(payload: dict) -> dict:
        records = load_records(payload)
        out = calculate(records, payload)
        return {
            "gross_total": out["gross_total"],
            "exceptions": sorted(
                (e["number"], e["reason"]) for e in out["exceptions"]),
            "paid_numbers": list(out["paid_numbers"]),
        }

    try:
        for payload in corpus:
            expected = oracle_envelope(payload)

            # parser: row filtering + defensive copies
            records = load_records(payload)
            expected_rows = [r for r in payload["invoices"]
                             if isinstance(r, dict)]
            if len(records) != len(expected_rows):
                return fail(
                    f"parser row count {len(records)} != "
                    f"{len(expected_rows)} on payload "
                    f"numbers={[r.get('number') for r in payload['invoices']]}")
            for row, src in zip(records, expected_rows):
                if row is src:
                    return fail("parser returned aliased source row "
                                "(defensive copy removed)")
                if row != src:
                    return fail("parser mutated row contents")

            # engine envelope vs independent oracle
            actual = calculate(records, payload)
            if not isinstance(actual.get("gross_total"), str):
                return fail("gross_total lost its string type")
            if not money_re.match(actual["gross_total"]):
                return fail(f"gross_total not 2-dp string: "
                            f"{actual['gross_total']!r}")
            if not Decimal(actual["gross_total"]).is_finite():
                return fail("gross_total non-finite")
            if actual["gross_total"] != expected["gross_total"]:
                return fail(
                    f"gross_total {actual['gross_total']} != oracle "
                    f"{expected['gross_total']} on numbers="
                    f"{[r.get('number') for r in payload['invoices']]}")
            actual_exc = sorted(
                (e["number"], e["reason"]) for e in actual["exceptions"])
            if actual_exc != sorted(
                    (e["number"], e["reason"])
                    for e in expected["exceptions"]):
                return fail(f"exception set {actual_exc} != oracle "
                            f"{sorted((e['number'], e['reason']) for e in expected['exceptions'])}")
            # every exception localizes number + reason with exact labels
            for entry in actual["exceptions"]:
                if set(entry.keys()) != {"number", "reason"}:
                    return fail(f"exception report fields wrong: {entry}")
                if entry["reason"] not in ("tax_mismatch",
                                           "duplicate_number"):
                    return fail(f"exception reason label changed: {entry}")
            if actual["paid_numbers"] != payload["payments"]:
                return fail("paid_numbers passthrough altered "
                            "(order/duplicates must be preserved)")
            if not isinstance(actual["paid_numbers"], list):
                return fail("paid_numbers default type changed")

            # pipeline + report envelope
            envelope = run(payload)
            if envelope.get("project") != "evidence-invoice-reconcile":
                return fail(f"project field wrong: {envelope.get('project')}")
            if envelope.get("status") != "ok":
                return fail(f"status field wrong: {envelope.get('status')}")
            if "data" in envelope:
                return fail("report nests data instead of flattening")
            if envelope["gross_total"] != expected["gross_total"]:
                return fail("pipeline gross disagrees with oracle")

            # determinism under input reordering (invoices only)
            shuffled = dict(payload)
            rows = list(payload["invoices"])
            if len(rows) > 1:
                rev = dict(payload)
                rev["invoices"] = list(reversed(rows))
                c1, c2 = canonical(payload), canonical(rev)
                if c1["gross_total"] != c2["gross_total"]:
                    return fail("gross_total depends on invoice ordering")
                if c1["exceptions"] != c2["exceptions"]:
                    return fail("canonical exception set depends on order")

        # direct parser contract: non-list rejected with documented message
        try:
            load_records({"invoices": {"no": "list"}})
        except ValueError as exc:
            if str(exc) != "invoices must be a list":
                return fail(f"parser error message drifted: {exc!r}")
        else:
            return fail("parser no longer rejects non-list invoices")

        # missing payments key must default to a list (never None),
        # keeping the envelope type stable
        no_payments = {"invoices": [
            {"number": "NP1", "net": "1.00", "tax_rate": "0.13",
             "tax": "0.13", "status": "normal"}]}
        out_np = calculate(load_records(no_payments), no_payments)
        if out_np.get("paid_numbers") != []:
            return fail(f"paid_numbers default drifted: "
                        f"{out_np.get('paid_numbers')!r}")
        if not isinstance(out_np.get("paid_numbers"), list):
            return fail("paid_numbers default lost list type")

        # report layer direct contract incl. data flattening
        wrapped = build_result("evidence-invoice-reconcile",
                               {"gross_total": "0.00",
                                "exceptions": [], "paid_numbers": []})
        if wrapped != {"project": "evidence-invoice-reconcile",
                       "status": "ok", "gross_total": "0.00",
                       "exceptions": [], "paid_numbers": []}:
            return fail(f"build_result envelope shape wrong: {wrapped}")

        # rounding-mode probe: 1.05 * 0.13 = 0.1365 -> 0.14 (HALF_UP)
        probe = {"invoices": [
            {"number": "RB1", "net": "1.05", "tax_rate": "0.13",
             "tax": "0.14", "status": "normal"}], "payments": []}
        out = calculate(load_records(probe), probe)
        if out["exceptions"]:
            return fail(f"rounding mode probe failed: {out['exceptions']}")
        # negative half-cent: -120.55 * 0.07 = -8.4385 -> -8.44
        probe_neg = {"invoices": [
            {"number": "RB2", "net": "-120.55", "tax_rate": "0.07",
             "tax": "-8.44", "status": "red"}], "payments": []}
        out_neg = calculate(load_records(probe_neg), probe_neg)
        if out_neg["exceptions"]:
            return fail(f"negative rounding probe failed: {out_neg['exceptions']}")

        # reversal-suffix probe: duplicate -R numbers are legal
        rev = {"invoices": [
            {"number": "RR1-R", "net": "-1.00", "tax_rate": "0.13",
             "tax": "-0.13", "status": "red"},
            {"number": "RR1-R", "net": "-1.00", "tax_rate": "0.13",
             "tax": "-0.13", "status": "red"}], "payments": []}
        out_rev = calculate(load_records(rev), rev)
        if any(e["reason"] == "duplicate_number" for e in out_rev["exceptions"]):
            return fail("reversal suffix exemption broken")

        # duplicate threshold probe: exactly 2 equal non-reversal numbers
        dup = {"invoices": [
            {"number": "DD1", "net": "1.00", "tax_rate": "0.13",
             "tax": "0.13", "status": "normal"},
            {"number": "DD1", "net": "1.00", "tax_rate": "0.13",
             "tax": "0.13", "status": "normal"}], "payments": []}
        out_dup = calculate(load_records(dup), dup)
        if not any(e["reason"] == "duplicate_number"
                   and e["number"] == "DD1" for e in out_dup["exceptions"]):
            return fail("duplicate detection broken")

        # extra-precision probe: documented rounding-location behavior.
        # Engine accumulates raw and quantizes at the summary; oracle
        # quantizes each line first. Both must stay finite 2-dp strings
        # and their documented values are pinned (113.01 vs 113.02), so
        # any rounding/quantize mutant still shifts one side.
        prec = {"invoices": [
            {"number": "EP1", "net": "100.005", "tax_rate": "0.13",
             "tax": "13.00", "status": "normal"},
            {"number": "EP2", "net": "0.005", "tax_rate": "0.19",
             "tax": "0.00", "status": "normal"}], "payments": []}
        out_prec = calculate(load_records(prec), prec)
        if not money_re.match(out_prec["gross_total"]):
            return fail(f"final quantize lost: {out_prec['gross_total']!r}")
        if out_prec["gross_total"] != "113.01":
            return fail(f"summary-rounding contract drifted on sub-cent "
                        f"input: {out_prec['gross_total']} (expected 113.01)")
        prec_report = oracle_envelope(prec)
        if prec_report["gross_total"] != "113.02":
            return fail("oracle line-rounding reference drifted: "
                        f"{prec_report['gross_total']} (expected 113.02)")
        # both conventions agree exactly for cent-exact money
        cent_exact = {"invoices": [
            {"number": "CE1", "net": "100.01", "tax_rate": "0.13",
             "tax": "13.00", "status": "normal"}], "payments": []}
        out_ce = calculate(load_records(cent_exact), cent_exact)
        if out_ce["gross_total"] != oracle_envelope(
                cent_exact)["gross_total"]:
            return fail("cent-exact inputs must not drift between line "
                        "and summary rounding")

        # randomized determinism probe (mutant must stay stable)
        rnd = random.Random(424242)
        strat = Payload()
        for _ in range(10):
            p1 = strat.generate(rnd)
            p2 = strat.generate(rnd)
            if canonical(p1) != canonical(p1) or canonical(p2) != canonical(p2):
                return fail("non-idempotent engine output")

    except BaseException as exc:
        return fail(f"crash on valid corpus: {type(exc).__name__}: {exc}\n"
                    f"{traceback.format_exc()}")

    print(json.dumps({"mid": mid, "status": "survived"},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
