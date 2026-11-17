#!/usr/bin/env python3
"""QA layer entry point, executable directly under isolated mode.

    python3 -I qa/run_all.py                # properties, faults, snapshots
    python3 -I qa/run_all.py --mutations    # plus the mutation campaign
    python3 -I qa/run_all.py --record       # refresh golden snapshots

Exit code is non-zero if any property, snapshot or fault contract fails.
A machine-readable report is written to qa/report.json (git-ignored).
"""
from __future__ import annotations

import json
import sys
import unittest
from dataclasses import asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parent
for path in (str(WORKSPACE / "src"), str(WORKSPACE), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)

from qa.generators import (  # noqa: E402
    half_cent_payload, zero_and_negative_payload,
    multi_invoice_partial_payment_payload,
)
from qa.diffs import write_snapshot  # noqa: E402
from evidence_invoice_reconcile.pipeline import run  # noqa: E402
from evidence_invoice_reconcile.parser import load_records  # noqa: E402
from evidence_invoice_reconcile.engine import calculate  # noqa: E402
from qa.fault_injection import run_all_injections  # noqa: E402

GOLDEN = [
    ("half-cent", half_cent_payload),
    ("zero-negative", zero_and_negative_payload),
    ("multi-partial-payment",
     multi_invoice_partial_payment_payload),
]


def main() -> int:
    report: dict = {"snapshots": [], "fault_injection": [],
                    "mutations": []}

    if "--record" in sys.argv:
        report["snapshots_recorded"] = [
            str(write_snapshot(name, builder()))
            for name, builder in GOLDEN]

    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName("qa.test_properties")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    properties_ok = result.wasSuccessful()

    outcomes = run_all_injections(
        multi_invoice_partial_payment_payload(),
        run, load_records, calculate)
    report["fault_injection"] = [asdict(o) for o in outcomes]
    hazards = [o.name for o in outcomes if o.severity == "hazard"]
    faults_ok = all(o.severity in ("contracted", "hazard")
                    for o in outcomes)

    mutations_ok = True
    if "--mutations" in sys.argv:
        from qa.mutation_runner import run_all as run_mutations
        mutation_results = run_mutations()
        report["mutations"] = [asdict(m) for m in mutation_results]
        survivors = [m.mid for m in mutation_results
                     if m.status == "survived"]
        not_applied = [m.mid for m in mutation_results
                       if m.status == "not_applied"]
        mutations_ok = not survivors and not not_applied
        print("\nMutation summary:")
        for m in mutation_results:
            print(f"  {m.mid} [{m.module}] {m.status:11s} "
                  f"{m.description}")
        killed = sum(1 for m in mutation_results
                     if m.status == "killed")
        total = len(mutation_results)
        print(
            f"  killed {killed}/{total} (equivalent: "
            f"{sum(1 for m in mutation_results if m.status == 'equivalent')})")
        if survivors:
            print(f"  SURVIVORS: {survivors}")
        if not_applied:
            print(f"  NOT APPLIED: {not_applied}")

    report["summary"] = {
        "properties_ok": properties_ok,
        "faults_ok": faults_ok,
        "mutations_ok": mutations_ok,
        "hazards": hazards,
    }
    out_path = WORKSPACE / "qa" / "report.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False,
                                   indent=2, sort_keys=True) + "\n")
    print(f"\nQA report written to {out_path}")
    print(f"hazards (documented finance exposures): {hazards}")
    ok = properties_ok and faults_ok and mutations_ok
    print("QA RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
