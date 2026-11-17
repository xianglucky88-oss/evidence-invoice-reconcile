import json
import unittest
from decimal import Decimal
from pathlib import Path

from evidence_invoice_reconcile.pipeline import run
from evidence_invoice_reconcile.parser import load_records
from evidence_invoice_reconcile.engine import calculate


def fixture():
    return json.loads((Path(__file__).parents[1] / "fixtures" / "input.json").read_text())


class TestCore(unittest.TestCase):
    """基线契约：模块边界与输出信封在重构后仍然成立。"""

    def test_pipeline_and_modules(self):
        payload = fixture()
        rows = load_records(payload)
        self.assertTrue(rows)
        data = calculate(rows, payload)
        self.assertIsInstance(data, dict)
        self.assertTrue(data)
        result = run(payload)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["project"], 'evidence-invoice-reconcile')

        # 兼容字段仍然存在，且 gross_total 等于全部发票含税金额之和
        self.assertIn("gross_total", result)
        self.assertIn("exceptions", result)
        self.assertIn("paid_numbers", result)
        expected = sum((Decimal(str(inv["net"])) + Decimal(str(inv["tax"]))
                        for inv in payload["invoices"]), Decimal("0.00"))
        self.assertEqual(result["gross_total"], f"{expected:.2f}")
        # 新引擎能够发现异常（重复导出/税额/跨期等）
        self.assertTrue(result["exceptions"])

    def test_three_way_decision_buckets_present(self):
        result = run(fixture())
        for bucket in ("verified", "manual_review", "rejected"):
            self.assertIn(bucket, result["decisions"])
        self.assertEqual(
            sum(result["decisions"]["counts"].values()),
            len(fixture()["invoices"]),
        )


if __name__ == "__main__":
    unittest.main()
