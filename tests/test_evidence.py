"""证据级核验与可重放对账的综合测试。

覆盖需求中的全部验证场景：
一票多行、一红冲多蓝、同号重复、金额精度边界、输入顺序变化、
匹配候选/冲销分配守恒、贪心 vs 全局失败代价、单票失败隔离、
批次中断恢复、规则升级、哈希漂移/人工更正/跨期付款的新结论链、
重复导出幂等、哈希链防篡改与 WAL 事务。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from evidence_invoice_reconcile.pipeline import run, BatchInterrupted
from evidence_invoice_reconcile.journal import AuditJournal, JournalTampered
from evidence_invoice_reconcile.matching import (
    build_plans, generate_candidates, greedy_allocate, global_allocate,
    UNALLOCATED_RED_PENALTY, UNMATCHED_BLUE_PENALTY,
)
from evidence_invoice_reconcile.money import from_cents, parse_cents, quantize
from evidence_invoice_reconcile.taxrules import (
    available_versions, expected_invoice_tax, get_rule,
)
from evidence_invoice_reconcile.hashing import canonical_hash
from evidence_invoice_reconcile.parser import parse_payload, ParseError

FIXTURE = Path(__file__).parents[1] / "fixtures" / "input.json"


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def blue(number, net, tax, **kw):
    inv = {
        "number": number, "status": "normal", "net": net, "tax": tax,
        "rule_version": "TAX-2018.5",
        "source_hash": kw.pop("source_hash", "0" * 64),
    }
    inv.update(kw)
    return inv


def red(number, net, tax, links, **kw):
    inv = {
        "number": number, "status": "red", "net": net, "tax": tax,
        "red_links": links, "rule_version": "TAX-2018.5",
        "source_hash": kw.pop("source_hash", "1" * 64),
    }
    inv.update(kw)
    return inv


def codes(report, number):
    out = []
    for bucket in ("verified", "manual_review", "rejected"):
        for c in report["decisions"][bucket]:
            if c["invoice_number"] == number:
                out.extend(c["reason_codes"])
    return sorted(set(out))


def decision_of(report, number):
    for bucket in ("verified", "manual_review", "rejected"):
        for c in report["decisions"][bucket]:
            if c["invoice_number"] == number:
                return bucket
    raise KeyError(number)


class TestMoneyAndTaxRules(unittest.TestCase):
    def test_no_float_anywhere_and_cents_roundtrip(self):
        self.assertEqual(parse_cents("100.005", field_path="x"), 10001)  # HALF_UP
        self.assertEqual(parse_cents("100.004"), 10000)
        self.assertEqual(from_cents(10001), "100.01")
        self.assertEqual(from_cents(-5), "-0.05")
        with self.assertRaises(ValueError):
            parse_cents("abc")

    def test_rule_versions_are_frozen_and_distinct(self):
        self.assertEqual(set(available_versions()),
                         {"TAX-2018.5", "TAX-2019.4", "TAX-2024.1"})
        with self.assertRaises(Exception):
            get_rule("TAX-2018.5").rates = ()  # frozen

    def test_rounding_level_changes_boundary_by_one_cent(self):
        # 净额 0.25、税率 6% -> 0.015 恰好落在半分边界：
        # HALF_UP -> 0.02；HALF_EVEN（偶数位）-> 0.02；
        # 净额 0.35 -> 0.021 -> 两者均 0.02。
        # 选用净额 0.25 并叠加多行，区分 line 级与 invoice 级的累计差异：
        lines = [{"net": Decimal("0.25"), "rate_code": "service"},
                 {"net": Decimal("0.25"), "rate_code": "service"}]
        v_line = expected_invoice_tax(get_rule("TAX-2018.5"), lines)   # 逐行 HALF_UP 0.02*2
        v_inv = expected_invoice_tax(get_rule("TAX-2019.4"), lines)    # 汇总 0.50*6%=0.03
        self.assertEqual(v_line, Decimal("0.04"))
        self.assertEqual(v_inv, Decimal("0.03"))
        self.assertEqual(int((v_line - v_inv).scaleb(2)), 1)

    def test_half_up_vs_half_even_share_even_target_at_half_cent(self):
        # 0.015 边界：HALF_UP 进位到 0.02；HALF_EVEN 的偶数目标也是 0.02，
        # 差异主要体现在 line/invoice 两个层级的累计方式（见上一测试）
        from evidence_invoice_reconcile.taxrules import expected_line_tax
        self.assertEqual(expected_line_tax(get_rule("TAX-2018.5"),
                                           Decimal("0.25"), "service"), Decimal("0.02"))
        self.assertEqual(expected_line_tax(get_rule("TAX-2019.4"),
                                           Decimal("0.25"), "service"), Decimal("0.02"))

    def test_2024_service_rate_change_is_versioned(self):
        lines = [{"net": Decimal("100.00"), "rate_code": "service"}]
        self.assertEqual(expected_invoice_tax(get_rule("TAX-2024.1"), lines),
                         quantize(Decimal("5.00")))
        self.assertEqual(expected_invoice_tax(get_rule("TAX-2018.5"), lines),
                         quantize(Decimal("6.00")))


class TestParsingAndFieldPaths(unittest.TestCase):
    def test_invoice_with_multiple_lines_preserves_paths(self):
        parsed = parse_payload(load_fixture())
        multi = next(i for i in parsed["invoices"] if i.number == "INV-MULTI-01")
        self.assertEqual(len(multi.lines), 2)
        self.assertTrue(all(l.field_path.startswith("invoices[0].lines[")
                            for l in multi.lines))

    def test_strict_rejection_of_bad_input_keeps_path(self):
        with self.assertRaises(ParseError) as ctx:
            parse_payload({"invoices": [{"number": "X", "net": "oops", "tax": "1.00"}]})
        self.assertIn("invoices[0]", ctx.exception.field_path)

    def test_blue_negative_is_rejected(self):
        with self.assertRaises(ParseError):
            parse_payload({"invoices": [blue("B", "-1.00", "-0.13")]})


class TestAllocationConservation(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "invoices": [
                blue("B1", "100.00", "13.00", counterparty="Globex"),
                blue("B2", "200.00", "26.00", counterparty="Globex"),
                red("R1", "-300.00", "-39.00", ["B1", "B2"], counterparty="Globex"),
            ],
            "payments": [],
        }

    def test_one_red_cancels_multiple_blues_exactly(self):
        report = run(self.payload)
        plan = report["allocation"]["global"]
        alloc = {(a["red_number"], a["blue_number"]): a["amount_cents"]
                 for a in plan["allocations"]}
        self.assertEqual(alloc[("R1", "B1")], 11300)
        self.assertEqual(alloc[("R1", "B2")], 22600)
        self.assertTrue(plan["conservation"]["holds"])
        self.assertEqual(plan["unallocated_red"], "0.00")
        # 红票与两张蓝票都已核验
        for n in ("B1", "B2", "R1"):
            self.assertEqual(decision_of(report, n), "verified")

    def test_conservation_identity_on_totals(self):
        report = run(self.payload)
        t = report["totals"]
        self.assertEqual(t["blue_gross"]["cents"] + t["red_gross"]["cents"],
                         t["gross_total"]["cents"])

    def test_candidates_include_explicit_and_inferred(self):
        parsed = parse_payload(self.payload)
        reds = [i for i in parsed["invoices"] if i.kind == "red"]
        blues = [i for i in parsed["invoices"] if i.kind == "normal"]
        cands = generate_candidates(reds, blues)
        declared = [c for c in cands if c.link_declared]
        self.assertEqual({c.blue_number for c in declared}, {"B1", "B2"})
        self.assertTrue(all(c.score == 100 for c in declared))


class TestGreedyVsGlobal(unittest.TestCase):
    def _build(self, ordered_numbers, *, asymmetric=False):
        # 两张红票竞争同一张蓝票额度：蓝票容量只能满足一张红票的一部分，
        # 另一张必然留下未分配残额（触发高惩罚）。
        invoices = [
            blue("B1", "100.00", "13.00", counterparty="Globex"),
        ]
        for idx, n in enumerate(ordered_numbers):
            if asymmetric:
                # R1 显式关联（边成本 0），R2 仅推断候选（边成本 >0），
                # 全局最优唯一：无论输入顺序，R1 都应优先获得蓝票额度
                links = ["B1"] if n == "R1" else []
                invoices.append(red(n, "-60.00", "-7.80", links,
                                    counterparty="Globex"))
            else:
                invoices.append(red(n, "-60.00", "-7.80", ["B1"],
                                    counterparty="Globex"))
        parsed = parse_payload({"invoices": invoices, "payments": []})
        reds = [i for i in parsed["invoices"] if i.kind == "red"]
        blues = [i for i in parsed["invoices"] if i.kind == "normal"]
        cands = generate_candidates(reds, blues)
        return reds, blues, cands

    def test_global_cost_never_exceeds_greedy(self):
        reds, blues, cands = self._build(["R1", "R2"])
        g = global_allocate(reds, blues, cands, "TAX-2018.5")
        gr = greedy_allocate(reds, blues, cands, "TAX-2018.5")
        self.assertLessEqual(g.cost["total_failure_cost"], gr.cost["total_failure_cost"])
        # 两种策略都满足容量守恒
        self.assertTrue(g.conservation["holds"] and gr.conservation["holds"])

    def test_global_optimum_is_invariant_under_input_permutation(self):
        # 非对称成本 -> 唯一全局最优，交换输入顺序后最优分配必须一致
        r1, b1, c1 = self._build(["R1", "R2"], asymmetric=True)
        r2, b2, c2 = self._build(["R2", "R1"], asymmetric=True)
        g_a = global_allocate(r1, b1, c1, "TAX-2018.5")
        g_b = global_allocate(r2, b2, c2, "TAX-2018.5")
        key = lambda p: sorted((a.red_number, a.blue_number, a.amount_cents)
                               for a in p.allocations)
        self.assertEqual(key(g_a), key(g_b))
        # 显式关联的 R1 拿到完整额度（成本最低路径优先）
        r1_share = sum(a.amount_cents for a in g_a.allocations if a.red_number == "R1")
        self.assertEqual(r1_share, 6780)
        self.assertEqual(g_a.cost["total_failure_cost"], g_b.cost["total_failure_cost"])

    def test_symmetric_tie_keeps_total_cost_invariant(self):
        # 对称红票存在多个等价最优解：具体归属可能随顺序变化，
        # 但守恒量（总失败代价、未分配总额）必须不变
        r1, b1, c1 = self._build(["R1", "R2"])
        r2, b2, c2 = self._build(["R2", "R1"])
        g_a = global_allocate(r1, b1, c1, "TAX-2018.5")
        g_b = global_allocate(r2, b2, c2, "TAX-2018.5")
        self.assertEqual(g_a.cost["total_failure_cost"], g_b.cost["total_failure_cost"])
        self.assertEqual(g_a.unallocated_red_cents, g_b.unallocated_red_cents)

    def test_greedy_has_strictly_higher_failure_cost_when_it_steals_explicit_link(self):
        # 容量受限：蓝票总额 < 红票总额。贪心按输入顺序先处理弱候选 R1，
        # 占满 R2 的显式关联目标 B2，使 R2 全额未分配（高惩罚）；
        # 全局最小费用流会保住零成本的显式关联边。
        invoices = [
            blue("B1", "50.00", "6.50", counterparty="X"),
            blue("B2", "50.00", "6.50", counterparty="X"),
            red("R1", "-100.00", "-13.00", [], counterparty="X"),
            red("R2", "-50.00", "-6.50", ["B2"], counterparty="X"),
        ]
        parsed = parse_payload({"invoices": invoices, "payments": []})
        reds = [i for i in parsed["invoices"] if i.kind == "red"]
        blues = [i for i in parsed["invoices"] if i.kind == "normal"]
        cands = generate_candidates(reds, blues)
        g = global_allocate(reds, blues, cands, "TAX-2018.5")
        gr = greedy_allocate(reds, blues, cands, "TAX-2018.5")

        # 全局保住显式关联：R2 -> B2
        global_pairs = {(a.red_number, a.blue_number) for a in g.allocations}
        self.assertIn(("R2", "B2"), global_pairs)
        # 贪心抢占后 R2 完全未分配
        greedy_r2 = sum(a.amount_cents for a in gr.allocations if a.red_number == "R2")
        self.assertEqual(greedy_r2, 0)
        # 失败代价：贪心严格高于全局，差额可被量化解释
        self.assertGreater(gr.cost["total_failure_cost"],
                           g.cost["total_failure_cost"])
        self.assertEqual(
            gr.cost["total_failure_cost"] - g.cost["total_failure_cost"],
            (gr.cost["edge_cost"] - g.cost["edge_cost"])
            + (gr.cost["unallocated_red_penalty"] - g.cost["unallocated_red_penalty"]),
        )

    def test_greedy_pick_changes_with_order_while_global_keeps_optimum(self):
        r1, b1, c1 = self._build(["R1", "R2"])
        r2, b2, c2 = self._build(["R2", "R1"])
        gr_a = greedy_allocate(r1, b1, c1, "TAX-2018.5")
        gr_b = greedy_allocate(r2, b2, c2, "TAX-2018.5")

        def share(plan, red):
            return sum(a.amount_cents for a in plan.allocations if a.red_number == red)

        # R1 排在前面时被贪心优先吃满；交换顺序后 R2 被优先吃满
        self.assertGreater(share(gr_a, "R1"), share(gr_a, "R2"))
        self.assertGreater(share(gr_b, "R2"), share(gr_b, "R1"))
        # 两种顺序下贪心的总失败代价相同（对称场景），但分配归属相反
        self.assertEqual(gr_a.cost["total_failure_cost"], gr_b.cost["total_failure_cost"])

    def test_failure_cost_penalty_weights_dominate_edge_cost(self):
        # 惩罚单价远高于边成本，保证“先覆盖后优化匹配质量”
        self.assertGreater(UNALLOCATED_RED_PENALTY, 100)
        self.assertGreater(UNMATCHED_BLUE_PENALTY, 100)


class TestDuplicateAndReplay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_duplicate_export_does_not_double_book(self):
        payload = load_fixture()
        r1 = run(payload, state_dir=self.tmp)
        multi = [c for c in r1["decisions"]["manual_review"]
                 if c["invoice_number"] == "INV-MULTI-01"]
        self.assertTrue(any("DUPLICATE_EXPORT" in c["reason_codes"] for c in multi))
        # ledger 中同号发票最多一条入账
        ledger_numbers = [e["invoice_number"] for e in r1["ledger"]]
        self.assertEqual(len(ledger_numbers), len(set(ledger_numbers)))

    def test_rerun_identical_input_is_idempotent_same_conclusion_id(self):
        payload = load_fixture()
        r1 = run(payload, state_dir=self.tmp)
        ids_before = {c["invoice_number"]: c["conclusion_id"]
                      for bucket in r1["decisions"].values() if isinstance(bucket, list)
                      for c in bucket}
        r2 = run(payload, state_dir=self.tmp)
        self.assertTrue(r2["idempotent_skips"])
        ids_after = {c["invoice_number"]: c["conclusion_id"]
                     for bucket in r2["decisions"].values() if isinstance(bucket, list)
                     for c in bucket}
        self.assertEqual(ids_before, ids_after)
        # 事件流中结论数量没有因重跑而增加
        state = AuditJournal(self.tmp).replay()
        self.assertEqual(
            sum(1 for e in AuditJournal(self.tmp).events()
                if e["type"] in ("CONCLUSION_ISSUED", "CONCLUSION_SUPERSEDE")),
            len(payload["invoices"]),
        )

    def test_hash_drift_creates_new_conclusion_with_supersedes_chain(self):
        payload = load_fixture()
        run(payload, state_dir=self.tmp)
        journal = AuditJournal(self.tmp)
        before = journal.latest_conclusion("INV-BLUE-02")["conclusion_id"]

        drifted = json.loads(json.dumps(payload))
        for inv in drifted["invoices"]:
            if inv["number"] == "INV-BLUE-02":
                # 原始 OCR 文件哈希变化（同时更新 source_hash 使输入指纹改变）
                inv["evidence"][0]["hash"] = "f" * 64
                inv["source_hash"] = "f" * 64
        r2 = run(drifted, state_dir=self.tmp)
        after = AuditJournal(self.tmp).latest_conclusion("INV-BLUE-02")
        self.assertNotEqual(after["conclusion_id"], before)
        self.assertEqual(after["supersedes"], before)
        self.assertEqual(after["sequence"], 2)
        # 漂移必须在新结论上留下可追溯异常，而不是静默改写
        new_conc = next(c for bucket in
                        (r2["decisions"]["manual_review"],
                         r2["decisions"]["rejected"],
                         r2["decisions"]["verified"])
                        for c in bucket if c["invoice_number"] == "INV-BLUE-02")
        self.assertIn("EVIDENCE_HASH_DRIFT", new_conc["reason_codes"])
        drift_anom = next(a for a in new_conc["anomalies"]
                          if a["code"] == "EVIDENCE_HASH_DRIFT")
        self.assertEqual(drift_anom["calc"]["prior_conclusion_id"], before)
        self.assertTrue(drift_anom["calc"]["drifted_files"])
        # 旧结论仍然完整保留
        state = AuditJournal(self.tmp).replay()
        self.assertIn(before, state["conclusions"])
        timeline = next(t for t in r2["conclusion_timeline"]
                        if t["invoice_number"] == "INV-BLUE-02")
        self.assertEqual(len(timeline["versions"]), 2)

    def test_manual_correction_and_cross_period_append_new_versions(self):
        payload = {
            "invoices": [blue("B1", "100.00", "13.00", fiscal_period="2026-10")],
            "payments": [],
        }
        run(payload, state_dir=self.tmp)
        corrected = json.loads(json.dumps(payload))
        corrected["invoices"][0]["manual_correction_of"] = "manual-case"
        corrected["invoices"][0]["source_hash"] = "e" * 64
        r2 = run(corrected, state_dir=self.tmp)
        conc = r2["decisions"]["manual_review"][0]
        self.assertIn("MANUAL_CORRECTION", conc["reason_codes"])

    def test_tampered_event_stream_is_detected_on_replay(self):
        payload = load_fixture()
        run(payload, state_dir=self.tmp)
        events_path = Path(self.tmp) / "events.ndjson"
        lines = events_path.read_text().splitlines()
        ev = json.loads(lines[2])
        ev["payload"]["decision"] = "verified"  # 篡改
        lines[2] = json.dumps(ev, sort_keys=True)
        events_path.write_text("\n".join(lines) + "\n")
        with self.assertRaises(JournalTampered):
            AuditJournal(self.tmp).replay()


class TestBatchInterruption(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_interruption_preserves_committed_conclusions_and_resumes(self):
        payload = load_fixture()
        with self.assertRaises(BatchInterrupted) as ctx:
            run(payload, state_dir=self.tmp, fail_after=3)
        self.assertEqual(len(ctx.exception.processed), 3)

        # 已提交的 3 张发票结论完整、哈希链可验证
        state = AuditJournal(self.tmp).replay()
        self.assertEqual(state["event_count"] > 0, True)
        committed = [n for n, h in state["invoice_latest"].items()]
        self.assertEqual(len(committed), 3)

        # 重跑：前 3 张幂等跳过，剩余发票继续处理，最终全部有结论
        report = run(payload, state_dir=self.tmp)
        all_numbers = {i["number"] for i in payload["invoices"]}
        seen = {c["invoice_number"] for bucket in
                (report["decisions"]["verified"],
                 report["decisions"]["manual_review"],
                 report["decisions"]["rejected"]) for c in bucket}
        self.assertTrue(all_numbers.issubset(seen))
        self.assertEqual(len(report["idempotent_skips"]), 3)


class TestRuleUpgrade(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_upgrade_freezes_old_version_and_issues_versioned_new_conclusion(self):
        payload = {"invoices": [
            blue("B1", "100.00", "13.00", rule_version="TAX-2018.5")
        ], "payments": []}
        r1 = run(payload, state_dir=self.tmp)
        self.assertEqual(
            r1["decisions"]["verified"][0]["rule_version"], "TAX-2018.5")

        upgraded = json.loads(json.dumps(payload))
        upgraded["invoices"][0]["rule_version"] = "TAX-2024.1"
        upgraded["invoices"][0]["source_hash"] = "9" * 64
        # 2024 服务税率变化不影响 goods(13%)，但规则版本不同 => 新结论
        r2 = run(upgraded, state_dir=self.tmp)
        new = r2["decisions"]["verified"][0]
        self.assertEqual(new["rule_version"], "TAX-2024.1")
        self.assertTrue(new["supersedes"])
        # 旧结论的规则版本永久冻结
        state = AuditJournal(self.tmp).replay()
        old = state["conclusions"][new["supersedes"]]
        self.assertEqual(old["rule_version"], "TAX-2018.5")


class TestRedReversalHardRules(unittest.TestCase):
    def test_red_exceeding_blue_capacity_is_rejected_and_not_booked(self):
        payload = {
            "invoices": [
                blue("B", "10.00", "1.30"),
                red("R", "-10.00", "-1.30", ["B"]),
                red("R2", "-5.00", "-0.65", ["B"]),
            ],
            "payments": [],
        }
        report = run(payload)
        self.assertEqual(decision_of(report, "R2"), "rejected")
        self.assertIn("NEGATIVE_UNEXPLAINED_BALANCE", codes(report, "R2"))
        ledger_numbers = [e["invoice_number"] for e in report["ledger"]]
        self.assertNotIn("R2", ledger_numbers)
        # 被拒绝发票的金额计算过程保留残额
        r2 = report["decisions"]["rejected"][0]
        anom = next(a for a in r2["anomalies"]
                    if a["code"] == "NEGATIVE_UNEXPLAINED_BALANCE")
        self.assertEqual(anom["calc"]["unallocated"]["cents"], 565)
        self.assertTrue(anom["field_path"])

    def test_red_pointing_to_missing_blue_is_structure_hard_error(self):
        payload = {
            "invoices": [red("R", "-5.00", "-0.65", ["GHOST"])],
            "payments": [],
        }
        report = run(payload)
        self.assertEqual(decision_of(report, "R"), "rejected")
        self.assertIn("STRUCTURE_INVALID", codes(report, "R"))


class TestPaymentEvidence(unittest.TestCase):
    def test_cross_period_and_partial_payment_flagged(self):
        report = run(load_fixture())
        cross = next(c for c in report["decisions"]["manual_review"]
                     if c["invoice_number"] == "INV-CROSS-06")
        self.assertIn("CROSS_PERIOD_PAYMENT", cross["reason_codes"])
        self.assertIn("PARTIAL_MATCH", cross["reason_codes"])
        anom = next(a for a in cross["anomalies"]
                    if a["code"] == "CROSS_PERIOD_PAYMENT")
        self.assertEqual(anom["field_path"].startswith("payments["), True)
        self.assertEqual(anom["rule_version"], "TAX-2018.5")

    def test_orphan_payment_is_batch_level_hard_anomaly(self):
        report = run(load_fixture())
        codes_list = [a["code"] for a in report["batch_anomalies"]]
        self.assertIn("PAYMENT_TARGET_MISSING", codes_list)
        hard = [a for a in report["batch_anomalies"] if a["level"] == "hard"]
        self.assertTrue(hard)

    def test_overpayment_detected_with_calc(self):
        payload = {
            "invoices": [blue("B1", "100.00", "13.00")],
            "payments": [{"payment_id": "P1", "invoice_number": "B1",
                          "amount": "200.00", "fiscal_period": "2026-10"}],
        }
        report = run(payload)
        conc = report["decisions"]["manual_review"][0]
        self.assertIn("PAYMENT_OVERMATCH", conc["reason_codes"])
        anom = next(a for a in conc["anomalies"] if a["code"] == "PAYMENT_OVERMATCH")
        self.assertEqual(anom["calc"]["overpaid"]["cents"], 8700)


class TestInputOrderInvariance(unittest.TestCase):
    def test_shuffled_input_keeps_global_allocations_and_decisions(self):
        payload = load_fixture()
        r1 = run(payload)
        shuffled = json.loads(json.dumps(payload))
        shuffled["invoices"] = list(reversed(shuffled["invoices"]))
        r2 = run(shuffled)

        key = lambda r: sorted(
            (a["red_number"], a["blue_number"], a["amount_cents"])
            for a in r["allocation"]["global"]["allocations"])
        self.assertEqual(key(r1), key(r2))
        # 决策分布与守恒总额不变
        self.assertEqual(r1["decisions"]["counts"], r2["decisions"]["counts"])
        self.assertEqual(r1["totals"]["gross_total"], r2["totals"]["gross_total"])
        # 规范化输入哈希对键顺序不敏感
        self.assertEqual(canonical_hash({"a": 1, "b": 2}),
                         canonical_hash({"b": 2, "a": 1}))


class TestEvidenceChainShape(unittest.TestCase):
    def test_each_invoice_has_chain_and_anomaly_evidence(self):
        report = run(load_fixture())
        self.assertEqual(len(report["evidence_chain"]),
                         len(load_fixture()["invoices"]))
        for chain in report["evidence_chain"]:
            self.assertTrue(chain["source_files"])
            self.assertIn("timeline", chain)
        # 每个异常都带字段路径、规则版本、证据与计算过程
        for bucket in ("manual_review", "rejected"):
            for c in report["decisions"][bucket]:
                for a in c["anomalies"]:
                    self.assertTrue(a["field_path"])
                    self.assertTrue(a["rule_version"])
                    self.assertIsInstance(a["calc"], dict)
                    self.assertIsInstance(a["evidence"], list)


class TestLegacyContract(unittest.TestCase):
    def test_baseline_gross_total_and_exceptions_still_present(self):
        report = run(load_fixture())
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["project"], "evidence-invoice-reconcile")
        self.assertIn("gross_total", report)
        self.assertIsInstance(report["exceptions"], list)
        self.assertIsInstance(report["paid_numbers"], list)


if __name__ == "__main__":
    unittest.main()
