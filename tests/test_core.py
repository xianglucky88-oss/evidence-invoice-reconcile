import json
import unittest
from pathlib import Path
from evidence_invoice_reconcile.pipeline import run
from evidence_invoice_reconcile.parser import load_records
from evidence_invoice_reconcile.engine import calculate

def fixture():
    return json.loads((Path(__file__).parents[1] / "fixtures" / "input.json").read_text())

class TestCore(unittest.TestCase):
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

        assert result["gross_total"] == "52.00" and result["exceptions"]
