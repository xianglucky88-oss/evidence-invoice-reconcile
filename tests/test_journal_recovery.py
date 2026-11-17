"""WAL 崩溃恢复与文件锁的专项测试。

模拟两类故障：
1. 事务处理中进程被杀：wal-<tx>.ndjson 残留在 state_dir，
   下次打开 journal 时必须隔离 WAL 并写入 RUN_ABORT，主事件流不被污染。
2. 回滚路径：事务上下文内抛异常，WAL 不得并入主流。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from evidence_invoice_reconcile.journal import (
    AuditJournal, EV_RUN_ABORT, EV_RUN_START,
)
from evidence_invoice_reconcile.pipeline import run


class TestWALRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_orphan_wal_after_simulated_crash_is_quarantined(self):
        journal = AuditJournal(self.tmp)
        with journal:
            with journal.transaction("RUN-A", "RUN-A-0001", "hash-a") as w:
                w.append("CONCLUSION_ISSUED", {
                    "conclusion_id": "c1", "invoice_number": "INV1",
                    "decision": "verified", "run_id": "RUN-A", "sequence": 1,
                    "supersedes": None, "input_hash": "hash-a",
                })
                w.commit_marker()
        # 手动制造一个“提交前崩溃”的残留 WAL
        orphan = Path(self.tmp) / "wal-RUN-CRASH-0009.ndjson"
        orphan.write_text(json.dumps({
            "event_id": "deadbeef", "type": EV_RUN_START,
            "recorded_at": "1970-01-01T00:00:00.000000Z",
            "payload": {"run_id": "RUN-CRASH", "tx": "RUN-CRASH-0009"},
            "prev_hash": None, "event_hash": "x",
        }) + "\n")

        # 重新打开：恢复逻辑应隔离 WAL 并追加 ABORT 事件
        journal2 = AuditJournal(self.tmp)
        with journal2:
            state = journal2.replay()
        self.assertFalse(orphan.exists())
        quarantined = list(Path(self.tmp).glob("aborted-wal-RUN-CRASH-0009.ndjson"))
        self.assertEqual(len(quarantined), 1)
        abort_runs = [r for r in state["runs"].values() if r["status"] == "aborted"]
        self.assertTrue(any(r.get("abort_reason") == "wal_recovered_after_crash"
                            for r in abort_runs))
        # 主流中崩溃事务的结论不存在
        self.assertNotIn("c-dead", state["conclusions"])

    def test_exception_inside_transaction_rolls_back_wal(self):
        journal = AuditJournal(self.tmp)
        with self.assertRaises(RuntimeError):
            with journal:
                with journal.transaction("RUN-B", "RUN-B-0001", "hash-b") as w:
                    w.append("CONCLUSION_ISSUED", {
                        "conclusion_id": "c2", "invoice_number": "INV2",
                        "decision": "verified", "run_id": "RUN-B", "sequence": 1,
                        "supersedes": None, "input_hash": "hash-b",
                    })
                    raise RuntimeError("simulated processing failure")
        with journal:
            state = journal.replay()
        self.assertNotIn("c2", state["conclusions"])
        # WAL 已隔离，主流只保留 ABORT 事件
        wal_left = list(Path(self.tmp).glob("wal-*.ndjson"))
        self.assertEqual(wal_left, [])
        events = AuditJournal(self.tmp).events()
        self.assertIn(EV_RUN_ABORT, [e["type"] for e in events])

    def test_pipeline_after_crash_resumes_without_silent_rewrite(self):
        payload = {
            "invoices": [
                {"number": "B1", "status": "normal", "net": "100.00", "tax": "13.00",
                 "rule_version": "TAX-2018.5", "source_hash": "a" * 64},
            ],
            "payments": [],
        }
        run(payload, state_dir=self.tmp)
        before = AuditJournal(self.tmp).latest_conclusion("B1")["conclusion_id"]

        # 崩溃残留不影响后续正常重跑，且已确认结论 ID 不变
        orphan = Path(self.tmp) / "wal-RUN-Z-0099.ndjson"
        orphan.write_text(json.dumps({"event_id": "z"}) + "\n")
        report = run(payload, state_dir=self.tmp)
        after = AuditJournal(self.tmp).latest_conclusion("B1")["conclusion_id"]
        self.assertEqual(before, after)
        self.assertTrue(report["idempotent_skips"])


if __name__ == "__main__":
    unittest.main()
