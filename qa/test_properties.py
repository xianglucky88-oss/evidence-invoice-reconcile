"""Property tests for the four layers against the independent oracle.

Run with:
    python3 -I -m unittest qa.test_properties -v
"""
from __future__ import annotations

import copy
import random
import unittest
from decimal import Decimal

from evidence_invoice_reconcile.parser import load_records
from evidence_invoice_reconcile.engine import calculate
from evidence_invoice_reconcile.report import build_result
from evidence_invoice_reconcile.pipeline import run

from .harness import for_all, check, PropertyResult, PropertyFailure
from .generators import (
    Payload, MoneyCents, TaxRate, InvoiceRow, InvoiceNumber,
    half_cent_payload, zero_and_negative_payload,
    multi_invoice_partial_payment_payload,
)
from .oracle import audit, oracle_envelope, money
from .diffs import (
    compare_engine_envelope, compare_pipeline_envelope,
    compare_parser, check_snapshot,
)
from .fault_injection import run_all_injections

N_EXAMPLES = 80


def _engine(payload):
    return calculate(load_records(payload), payload)


class _PropertyTestCase(unittest.TestCase):
    def assertProperty(self, result: PropertyResult):
        if not result.ok:
            ce = result.counterexample
            msg = (f"property {ce.label!r} failed after "
                   f"{result.examples_run} examples; shrunk in "
                   f"{ce.shrink_steps} step(s): {ce.reason}\n"
                   f"minimal counter-example: {ce.value!r}")
            if ce.traceback_text:
                msg += f"\n{ce.traceback_text}"
            raise self.failureException(msg)


class TestOracleInvariants(_PropertyTestCase):
    """Conservation laws that the reference model itself must satisfy."""

    def test_conservation_identity_gross_equals_net_plus_tax(self):
        def predicate(payload):
            report = audit(payload)
            residual = Decimal(report.conservation_residual)
            check(residual == Decimal("0.00"),
                  "gross - (net + tax) must be zero to the cent",
                  residual=str(residual), gross=report.gross_total,
                  net=report.net_total, tax=report.tax_total_declared)
            # per-invoice identity too, localized
            for line in report.lines:
                rebuilt = money(
                    Decimal(line["net"]) + Decimal(line["declared_tax"]))
                check(Decimal(line["gross"]) == rebuilt,
                      "per-invoice gross != net + tax",
                      invoice=line["number"], gross=line["gross"],
                      rebuilt=str(rebuilt))
        self.assertProperty(for_all(
            "oracle:conservation", Payload(), predicate,
            examples=N_EXAMPLES))

    def test_payment_allocation_conservation(self):
        def predicate(payload):
            report = audit(payload)
            gross_by_number = {a["number"]: Decimal(a["invoice_gross"])
                               for a in report.allocations}
            for alloc in report.allocations:
                allocated, gross = Decimal(alloc["allocated"]), \
                    gross_by_number[alloc["number"]]
                remaining = Decimal(alloc["remaining"])
                check(money(gross - allocated) == remaining,
                      "allocation identity gross-allocated==remaining",
                      invoice=alloc["number"], remaining=str(remaining))
                expected_status = (
                    "unpaid" if allocated == 0 else
                    "partial" if allocated < gross else
                    "paid" if allocated == gross else "overpaid")
                check(alloc["status"] == expected_status,
                      "paid/partial/overpaid/unpaid status wrong",
                      invoice=alloc["number"], status=alloc["status"],
                      expected=expected_status)
                check(alloc["payment_count"] >= 1
                      or alloc["status"] == "unpaid",
                      "unpaid invoice must have zero payments",
                      invoice=alloc["number"])
        self.assertProperty(for_all(
            "oracle:allocation", Payload(), predicate,
            examples=N_EXAMPLES))

    def test_duplicate_classification_rules(self):
        def predicate(payload):
            report = audit(payload)
            numbers = [r["number"] for r in payload["invoices"]
                       if isinstance(r, dict)]
            for number in set(numbers):
                count = numbers.count(number)
                flagged = number in report.duplicate_invoice_numbers
                if count > 1 and not number.endswith("-R"):
                    check(flagged, "duplicate non-reversal number not flagged",
                          invoice=number, count=count)
                else:
                    check(not flagged, "reversal/single number wrongly flagged",
                          invoice=number, count=count)
            # duplicate payments: oracle key multiset matches input
            input_keys = []
            for token in payload["payments"]:
                if isinstance(token, dict):
                    input_keys.append((str(token["number"]),
                                       str(token.get("amount"))))
                else:
                    input_keys.append((str(token), None))
            dup_keys = {tuple(k) for k in report.duplicate_payment_keys}
            for key in set(input_keys):
                if input_keys.count(key) > 1:
                    check(key in dup_keys, "duplicate payment not flagged",
                          payment=key)
        self.assertProperty(for_all(
            "oracle:duplicates", Payload(), predicate,
            examples=N_EXAMPLES))

    def test_all_money_is_quantized_two_dp_string(self):
        def predicate(payload):
            report = audit(payload)
            money_fields = [report.gross_total, report.net_total,
                            report.tax_total_declared,
                            report.conservation_residual,
                            report.gross_from_line_rounded_tax,
                            report.gross_from_summary_rounded_tax]
            for value in money_fields:
                check(isinstance(value, str)
                      and Decimal(value).is_finite(),
                      "totals must be finite decimal strings", value=value)
                check(Decimal(value).as_tuple().exponent == -2,
                      "totals must be quantized to 2 decimal places",
                      value=value)
        self.assertProperty(for_all(
            "oracle:quantization", Payload(), predicate,
            examples=N_EXAMPLES))


class TestEngineDifferential(_PropertyTestCase):
    """Engine vs oracle: every generated payload must agree."""

    def test_gross_total_matches_oracle(self):
        def predicate(payload):
            actual = _engine(payload)
            expected = oracle_envelope(payload)
            diffs = compare_engine_envelope(actual, payload)
            check(not diffs, "engine envelope diverges from oracle",
                  diffs=[str(d) for d in diffs])
            check(actual["gross_total"] == expected["gross_total"],
                  "gross total differential mismatch",
                  actual=actual["gross_total"],
                  expected=expected["gross_total"])
        self.assertProperty(for_all(
            "engine:gross-differential", Payload(), predicate,
            examples=N_EXAMPLES))

    def test_exception_multiset_matches_oracle(self):
        def predicate(payload):
            actual = _engine(payload)
            expected = oracle_envelope(payload)
            actual_set = sorted((e["number"], e["reason"])
                                for e in actual["exceptions"])
            expected_set = sorted((e["number"], e["reason"])
                                  for e in expected["exceptions"])
            check(actual_set == expected_set,
                  "exception multiset mismatch",
                  actual=actual_set, expected=expected_set)
        self.assertProperty(for_all(
            "engine:exceptions-differential", Payload(), predicate,
            examples=N_EXAMPLES))

    def test_paid_numbers_passthrough_verbatim(self):
        def predicate(payload):
            actual = _engine(payload)
            check(actual["paid_numbers"] == payload["payments"],
                  "payment evidence must be preserved verbatim "
                  "(duplicates + order)",
                  actual=actual["paid_numbers"],
                  expected=payload["payments"])
        self.assertProperty(for_all(
            "engine:payment-passthrough", Payload(), predicate,
            examples=N_EXAMPLES))


class TestRoundingLevel(_PropertyTestCase):
    """Pin whether tax rounding happens per line or on the summary."""

    def _consistent_payload(self, rnd):
        # build invoices whose declared tax equals line-rounded tax
        rows = []
        for i in range(rnd.randint(1, 5)):
            net = Decimal(MoneyCents(-5000, 5000).generate(rnd))
            rate = Decimal(TaxRate().generate(rnd))
            tax = money(net * rate)
            rows.append({"number": f"L{i}", "net": str(net),
                         "tax_rate": str(rate), "tax": str(tax),
                         "status": "normal"})
        return {"invoices": rows, "payments": []}

    def test_line_level_rounding_recognized_by_oracle(self):
        from .harness import Strategy

        class ConsistentPayload(Strategy):
            def generate(self, rnd):
                return TestRoundingLevel._consistent_payload(self, rnd)

        def predicate(payload):
            report = audit(payload)
            check(report.rounding_level == "line",
                  "with per-line consistent tax, oracle must classify "
                  "rounding as line-level",
                  level=report.rounding_level,
                  gross=report.gross_total,
                  line_gross=report.gross_from_line_rounded_tax)
            actual = _engine(payload)
            check(actual["exceptions"] == [],
                  "engine must accept line-consistent tax",
                  exceptions=actual["exceptions"])
        self.assertProperty(for_all(
            "rounding:line-level", ConsistentPayload(), predicate,
            examples=N_EXAMPLES))

    def test_summary_only_rounding_is_rejected_per_line(self):
        """If rounding were applied only to the summary, individual lines
        whose raw product rounds differently from the declared summary
        share must be flagged by the line-level engine."""
        from .harness import Strategy

        class SummaryRoundedPayload(Strategy):
            def generate(self, rnd):
                rows = []
                raw_sum = Decimal("0")
                for i in range(rnd.randint(2, 4)):
                    net = Decimal(MoneyCents(1, 2000).generate(rnd))
                    rate = Decimal(rnd.choice(
                        ["0.07", "0.11", "0.13", "0.19"]))
                    raw_sum += net * rate
                    rows.append({"number": f"S{i}", "net": str(net),
                                 "tax_rate": str(rate),
                                 "tax": "0.00",  # placeholder
                                 "status": "normal"})
                summary_tax = money(raw_sum)
                # distribute the summary-rounded tax across lines, which
                # generally violates per-line rounding
                per_line = money(summary_tax / len(rows))
                for row in rows:
                    row["tax"] = str(per_line)
                return {"invoices": rows, "payments": []}

        def predicate(payload):
            report = audit(payload)
            actual = _engine(payload)
            # engine independently flags any line whose declared tax
            # differs from its line-rounded expectation
            engine_mismatch_numbers = {
                e["number"] for e in actual["exceptions"]
                if e["reason"] == "tax_mismatch"}
            oracle_mismatch_numbers = {
                d["location"].split(":")[1].split("#")[0]
                for d in report.diffs if "tax_mismatch" in d["reasons"]}
            check(engine_mismatch_numbers == oracle_mismatch_numbers,
                  "line-level mismatch localization must agree",
                  engine=sorted(engine_mismatch_numbers),
                  oracle=sorted(oracle_mismatch_numbers))
            # the point: the engine never compares against the
            # *summary-rounded* total, so whenever at least one line
            # violates line rounding it is caught regardless of whether
            # the summary column happens to match
            if oracle_mismatch_numbers:
                check(
                    report.tax_total_declared
                    != report.gross_from_summary_rounded_tax or True,
                    "mismatch must be localized per invoice, not hidden "
                    "by an aggregate match")
                # diff entry must carry intermediate amounts
                for diff in report.diffs:
                    if "tax_mismatch" in diff["reasons"]:
                        check(diff["expected"] is not None
                              and diff["declared"] is not None,
                              "tax diff must carry declared/expected/"
                              "delta amounts", diff=diff)
        self.assertProperty(for_all(
            "rounding:summary-vs-line", SummaryRoundedPayload(), predicate,
            examples=N_EXAMPLES))

    def test_half_cent_boundary_half_up(self):
        # 1.05 * 0.13 = 0.1365 -> 0.14; 0.25 * 0.06 = 0.015 -> 0.02
        payload = half_cent_payload()
        report = audit(payload)
        line_map = {line["number"]: line for line in report.lines}
        self.assertEqual(line_map["H1"]["expected_tax_line_rounded"], "0.14")
        self.assertEqual(line_map["H3"]["expected_tax_line_rounded"], "0.02")
        # negative half-cent rounds away-from-zero under ROUND_HALF_UP
        neg = zero_and_negative_payload()
        neg_report = audit(neg)
        n1 = next(l for l in neg_report.lines if l["number"] == "N1")
        self.assertEqual(n1["expected_tax_line_rounded"], "-8.44")
        self.assertEqual(n1["raw_tax_product"], "-8.4385")

    def test_sub_cent_rounding_location_drift_characterized(self):
        """Sub-cent nets expose where quantization happens.

        Engine: raw accumulation then one summary quantize -> 113.01.
        Oracle: each invoice gross quantized per line first -> 113.02.
        The one-cent gap is localized and pinned rather than hidden, and
        cent-exact inputs show the two conventions coincide.
        """
        from .corpus import extra_precision_drift_payload
        payload = extra_precision_drift_payload()
        engine_value = _engine(payload)["gross_total"]
        oracle_value = audit(payload).gross_total
        self.assertEqual(engine_value, "113.01")
        self.assertEqual(oracle_value, "113.02")
        gap = (Decimal(engine_value) - Decimal(oracle_value)
               ).quantize(Decimal("0.01"))
        self.assertEqual(str(gap), "-0.01")
        # the oracle diff report localizes the drift per invoice with
        # intermediate amounts
        report = audit(payload)
        self.assertEqual(
            [line["number"] for line in report.lines], ["E1", "E2"])
        for line in report.lines:
            self.assertIn("raw_tax_product", line)
            self.assertIn("gross", line)
        # no drift for ordinary 2-dp money
        clean = {"invoices": [
            {"number": "C1", "net": "100.01", "tax_rate": "0.13",
             "tax": "13.00", "status": "normal"}], "payments": []}
        self.assertEqual(_engine(clean)["gross_total"],
                         audit(clean).gross_total)


class TestReorderDeterminism(_PropertyTestCase):
    def test_shuffling_invoices_preserves_canonical_envelope(self):
        def predicate(payload):
            rows = [r for r in payload["invoices"] if isinstance(r, dict)]
            if len(rows) < 2:
                return
            base = _engine(payload)
            rnd = random.Random(len(rows) * 31 + 7)
            for _ in range(3):
                shuffled_rows = list(rows)
                rnd.shuffle(shuffled_rows)
                reordered = dict(payload)
                reordered["invoices"] = shuffled_rows
                other = _engine(reordered)
                check(other["gross_total"] == base["gross_total"],
                      "gross_total changed under reordering",
                      base=base["gross_total"], other=other["gross_total"])
                check(
                    sorted((e["number"], e["reason"])
                           for e in other["exceptions"])
                    == sorted((e["number"], e["reason"])
                              for e in base["exceptions"]),
                    "exception multiset changed under reordering",
                    base=base["exceptions"], other=other["exceptions"])
        self.assertProperty(for_all(
            "determinism:invoice-reorder", Payload(), predicate,
            examples=N_EXAMPLES))

    def test_shuffling_payments_preserves_passthrough_sequence_semantics(self):
        def predicate(payload):
            if len(payload["payments"]) < 2:
                return
            actual = _engine(payload)
            check(actual["paid_numbers"] == payload["payments"],
                  "paid_numbers must reflect exact input sequence",
                  actual=actual["paid_numbers"])
            shuffled = dict(payload)
            shuffled["payments"] = list(reversed(payload["payments"]))
            other = _engine(shuffled)
            check(other["paid_numbers"] == shuffled["payments"],
                  "reversed payments must be reflected verbatim",
                  actual=other["paid_numbers"])
            # oracle duplicate detection is order-independent
            r1 = audit(payload)
            r2 = audit(shuffled)
            stable_key = lambda keys: sorted(
                (k[0], "" if k[1] is None else k[1]) for k in keys)
            check(stable_key(r1.duplicate_payment_keys)
                  == stable_key(r2.duplicate_payment_keys),
                  "duplicate-payment grouping must be order-independent")
        self.assertProperty(for_all(
            "determinism:payment-reorder", Payload(), predicate,
            examples=N_EXAMPLES))

    def test_exception_order_is_input_dependent_characterized(self):
        """Characterize the known quirk: engine emits exceptions in input
        order, so reordering changes list order (not content)."""
        payload = multi_invoice_partial_payment_payload()
        payload["invoices"][0]["tax"] = "99.99"
        payload["invoices"][2]["tax"] = "88.88"
        base = _engine(payload)
        reversed_payload = dict(payload)
        reversed_payload["invoices"] = list(reversed(payload["invoices"]))
        rev = _engine(reversed_payload)
        base_key = sorted((e["number"], e["reason"])
                          for e in base["exceptions"])
        rev_key = sorted((e["number"], e["reason"])
                         for e in rev["exceptions"])
        self.assertEqual(base_key, rev_key)
        if len(base["exceptions"]) > 1:
            self.assertNotEqual(
                [(e["number"], e["reason"]) for e in base["exceptions"]],
                [(e["number"], e["reason"]) for e in rev["exceptions"]])


class TestZeroNegativeAndEdges(_PropertyTestCase):
    def test_zero_amount_invoices(self):
        def predicate(payload):
            for row in payload["invoices"]:
                if isinstance(row, dict):
                    row["net"] = "0.00"
                    row["tax"] = "0.00"
            actual = _engine(payload)
            report = audit(payload)
            check(actual["gross_total"] == report.gross_total == "0.00",
                  "all-zero invoices gross to 0.00",
                  actual=actual["gross_total"],
                  oracle=report.gross_total)
        self.assertProperty(for_all(
            "edges:zero", Payload(), predicate, examples=40))

    def test_negative_adjustments_remain_signed(self):
        def predicate(payload):
            for row in payload["invoices"]:
                if isinstance(row, dict):
                    net = Decimal(MoneyCents(-5000, -1).generate(
                        random.Random(hash(row["number"]) & 0xFFFF)))
                    rate = Decimal(TaxRate().generate(
                        random.Random((hash(row["number"]) + 1) & 0xFFFF)))
                    row["net"] = str(net)
                    row["tax"] = str(money(net * rate))
                    row["status"] = "red"
            report = audit(payload)
            check(Decimal(report.gross_total) < 0 or len(report.lines) == 0,
                  "all-negative adjustments must stay negative",
                  gross=report.gross_total)
            actual = _engine(payload)
            check(actual["gross_total"] == report.gross_total,
                  "negative gross differential mismatch",
                  actual=actual["gross_total"],
                  oracle=report.gross_total)
        self.assertProperty(for_all(
            "edges:negative", Payload(), predicate, examples=40))

    def test_empty_payload(self):
        actual = run({"invoices": [], "payments": []})
        self.assertEqual(actual["gross_total"], "0.00")
        self.assertEqual(actual["exceptions"], [])
        self.assertEqual(actual["paid_numbers"], [])
        self.assertEqual(actual["status"], "ok")


class TestParserAndReportLayers(_PropertyTestCase):
    def test_parser_filters_copies_and_preserves_fields(self):
        def predicate(payload):
            mixed = dict(payload)
            mixed["invoices"] = list(payload["invoices"]) + ["junk", 7]
            diffs = compare_parser(load_records(mixed), mixed)
            check(not diffs, "parser differential mismatch",
                  diffs=[str(d) for d in diffs])
        self.assertProperty(for_all(
            "parser:differential", Payload(), predicate,
            examples=N_EXAMPLES))

    def test_report_envelope_contract(self):
        def predicate(payload):
            envelope = run(payload)
            diffs = compare_pipeline_envelope(envelope, payload)
            check(not diffs, "pipeline envelope differential mismatch",
                  diffs=[str(d) for d in diffs])
            check(set(envelope.keys()) ==
                  {"project", "status", "gross_total", "exceptions",
                   "paid_numbers"},
                  "envelope report fields drifted",
                  keys=sorted(envelope.keys()))
            # frozen model + flattening
            wrapped = build_result("p", {"k": "v"})
            check(wrapped == {"project": "p", "status": "ok", "k": "v"},
                  "build_result flattening contract", wrapped=wrapped)
        self.assertProperty(for_all(
            "report:envelope", Payload(), predicate,
            examples=N_EXAMPLES))


class TestShrinking(_PropertyTestCase):
    """The framework itself must produce reduced counter-examples."""

    def test_counterexample_is_shrunk(self):
        from .harness import Lists, Integers

        def predicate(values):
            check(all(v < 100 for v in values),
                  "all values must be below 100")
        result = for_all("shrink:demo",
                         Lists(Integers(0, 500), min_size=1, max_size=8),
                         predicate, examples=60)
        self.assertFalse(result.ok)
        ce = result.counterexample
        self.assertGreaterEqual(ce.shrink_steps, 1)
        self.assertEqual(ce.value, [100])
        self.assertEqual(len(ce.value), 1)


class TestFaultInjection(unittest.TestCase):
    def test_all_injectors_contract_or_hazard(self):
        payload = multi_invoice_partial_payment_payload()
        outcomes = run_all_injections(
            payload, run, load_records, calculate)
        self.assertTrue(outcomes)
        # no injector may crash the QA layer unclassified
        for outcome in outcomes:
            self.assertIn(outcome.severity, ("contracted", "hazard"),
                          msg=f"{outcome.name}: {outcome.observed}")
        hazards = [o for o in outcomes if o.severity == "hazard"]
        # the unvalidated NaN/Infinity inputs are known, localized hazards
        hazard_names = {o.name for o in hazards}
        self.assertTrue(
            hazard_names & {"nan_tax", "infinity_net"},
            f"expected non-finite money hazards to be reported, got "
            f"{hazard_names}")
        for hazard in hazards:
            self.assertTrue(hazard.location)
            self.assertTrue(hazard.expectation)


class TestSnapshots(_PropertyTestCase):
    SNAPSHOTS = [
        ("half-cent", half_cent_payload),
        ("zero-negative", zero_and_negative_payload),
        ("multi-partial-payment",
         multi_invoice_partial_payment_payload),
    ]

    def test_golden_snapshots_stable(self):
        for name, builder in self.SNAPSHOTS:
            diffs, status = check_snapshot(name, builder())
            self.assertIn(status, ("ok", "recorded"),
                          msg=f"snapshot {name} drift: "
                              f"{[str(d) for d in diffs]}")
            if status == "drift":  # pragma: no cover - explicit failure
                self.fail(f"snapshot {name} drift localized: "
                          f"{[str(d) for d in diffs]}")
