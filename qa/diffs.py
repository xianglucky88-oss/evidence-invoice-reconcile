"""Differential comparison and golden snapshots across the four layers.

Layers covered (each maps to one production module):

  parser   -> evidence_invoice_reconcile.parser.load_records
  engine   -> evidence_invoice_reconcile.engine.calculate
  report   -> evidence_invoice_reconcile.report.build_result
  pipeline -> evidence_invoice_reconcile.pipeline.run (orchestration)

Every comparison returns structured ``DiffItem`` records that localize a
discrepancy to an invoice number, payment token or envelope field, with
the intermediate amounts from the oracle attached. Snapshots are JSON
files under ``qa/snapshots``; a missing snapshot is recorded on first
run instead of failing (``--record`` writes the golden set).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from .oracle import audit, oracle_envelope

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"


@dataclass(frozen=True)
class DiffItem:
    layer: str
    location: str           # envelope:<field> | invoice:<n> | payment:<n>
    field: str
    actual: Any
    expected: Any
    delta: Any = None
    detail: str = ""


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _canonical_exception(exc: dict) -> tuple:
    return str(exc.get("number")), str(exc.get("reason"))


def compare_engine_envelope(actual: dict, payload: dict) -> list[DiffItem]:
    """Differential check of engine output against the oracle envelope."""
    expected = oracle_envelope(payload)
    diffs: list[DiffItem] = []

    # ---- gross_total: exact string, decimal-typed ------------------------
    actual_gross = actual.get("gross_total")
    exp_gross = expected["gross_total"]
    if not isinstance(actual_gross, str):
        diffs.append(DiffItem(
            "engine", "envelope:gross_total", "type",
            type(actual_gross).__name__, "str",
            detail="monetary envelope field must be a quantized string",
        ))
    else:
        try:
            delta = str((Decimal(actual_gross) - Decimal(exp_gross))
                        .quantize(Decimal("0.01")))
        except Exception:  # noqa: BLE001
            delta = None
        if actual_gross != exp_gross:
            diffs.append(DiffItem(
                "engine", "envelope:gross_total", "value",
                actual_gross, exp_gross, delta,
                detail="oracle gross = sum(net + declared tax)",
            ))

    # ---- exceptions: order-insensitive multiset, localized ---------------
    actual_exc = sorted(
        (_canonical_exception(e) for e in actual.get("exceptions", [])),
    )
    expected_exc = sorted(
        (e["number"], e["reason"]) for e in expected["exceptions"]
    )
    if actual_exc != expected_exc:
        only_actual = [e for e in actual_exc if e not in expected_exc]
        only_expected = [e for e in expected_exc if e not in actual_exc]
        diffs.append(DiffItem(
            "engine", "envelope:exceptions", "set",
            actual_exc, expected_exc,
            detail=f"unexpected={only_actual}; missing={only_expected}",
        ))
    # every exception entry must carry the localization keys
    for entry in actual.get("exceptions", []):
        if not isinstance(entry, dict) or "number" not in entry \
                or "reason" not in entry:
            diffs.append(DiffItem(
                "engine",
                f"invoice:{entry.get('number', '?')}"
                if isinstance(entry, dict) else "envelope:exceptions",
                "report_field", _jsonable(entry),
                '{"number": ..., "reason": ...}',
                detail="exception entries must localize invoice + reason",
            ))

    # ---- paid_numbers: verbatim passthrough incl. duplicates/order -------
    actual_paid = actual.get("paid_numbers")
    expected_paid = expected["paid_numbers"]
    if _jsonable(actual_paid) != _jsonable(expected_paid):
        diffs.append(DiffItem(
            "engine", "envelope:paid_numbers", "value",
            _jsonable(actual_paid), _jsonable(expected_paid),
            detail="payment evidence must pass through unchanged "
                   "(duplicates and order preserved)",
        ))
    return diffs


def compare_pipeline_envelope(actual: dict, payload: dict) -> list[DiffItem]:
    diffs = compare_engine_envelope(actual, payload)
    # report-layer envelope fields
    for field_name, expected_value in (
        ("project", "evidence-invoice-reconcile"),
        ("status", "ok"),
    ):
        if actual.get(field_name) != expected_value:
            diffs.append(DiffItem(
                "report", f"envelope:{field_name}", "value",
                actual.get(field_name), expected_value,
            ))
    if not isinstance(actual.get("data", None), type(None)):
        # data must be flattened into the envelope, not nested
        if "data" in actual:
            diffs.append(DiffItem(
                "report", "envelope:data", "shape",
                "present", "flattened",
                detail="build_result must flatten PipelineResult.data",
            ))
    return diffs


def compare_parser(records: list, payload: dict) -> list[DiffItem]:
    expected_rows = [r for r in payload.get("invoices", [])
                     if isinstance(r, dict)]
    diffs: list[DiffItem] = []
    if len(records) != len(expected_rows):
        diffs.append(DiffItem(
            "parser", "envelope:invoices", "count",
            len(records), len(expected_rows),
            detail="non-dict rows must be filtered, dict rows retained",
        ))
    for index, (actual_row, expected_row) in enumerate(
            zip(records, expected_rows)):
        number = expected_row.get("number", f"idx{index}")
        for key, value in expected_row.items():
            if actual_row.get(key) != value:
                diffs.append(DiffItem(
                    "parser", f"invoice:{number}#idx{index}", key,
                    actual_row.get(key), value,
                ))
        # parser returns copies, not the source object
        if records and actual_row is expected_row:
            diffs.append(DiffItem(
                "parser", f"invoice:{number}#idx{index}", "aliasing",
                "same object", "defensive copy",
            ))
    return diffs


def audit_view(payload: dict) -> dict:
    """Full oracle audit view, JSON-serialized, for snapshots."""
    return _jsonable(asdict(audit(payload)))


# ---------------------------------------------------------------------------
# golden snapshots
# ---------------------------------------------------------------------------

def snapshot_path(name: str) -> Path:
    return SNAPSHOT_DIR / f"{name}.json"


def build_snapshot(name: str, payload: dict, system_views: dict) -> dict:
    from evidence_invoice_reconcile.parser import load_records
    from evidence_invoice_reconcile.engine import calculate
    from evidence_invoice_reconcile.pipeline import run

    records = load_records(payload)
    engine_view = calculate(records, payload)
    pipeline_view = run(payload)
    snapshot = {
        "name": name,
        "input": payload,
        "oracle": audit_view(payload),
        "oracle_envelope": _jsonable(oracle_envelope(payload)),
        "views": _jsonable({
            "parser": records,
            "engine": engine_view,
            "pipeline": pipeline_view,
            **system_views,
        }),
        "diffs": [asdict(d) for d in compare_pipeline_envelope(
            pipeline_view, payload)],
    }
    return snapshot


def write_snapshot(name: str, payload: dict,
                   system_views: dict | None = None) -> Path:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = build_snapshot(name, payload, system_views or {})
    path = snapshot_path(name)
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2,
                               sort_keys=True) + "\n")
    return path


def check_snapshot(name: str, payload: dict) -> tuple[list[DiffItem], str]:
    """Return (diffs, status) where status is ok|recorded|drift."""
    fresh = build_snapshot(name, payload, {})
    path = snapshot_path(name)
    if not path.exists():
        write_snapshot(name, payload)
        return [], "recorded"
    golden = json.loads(path.read_text())
    fresh_text = json.dumps(fresh, ensure_ascii=False, sort_keys=True)
    golden_text = json.dumps(golden, ensure_ascii=False, sort_keys=True)
    if fresh_text == golden_text:
        return [], "ok"
    # localize the drift instead of dumping whole blobs
    drift: list[DiffItem] = []
    _localize_json_diff(golden, fresh, "", drift)
    return drift, "drift"


def _localize_json_diff(golden: Any, fresh: Any, path: str,
                        drift: list[DiffItem], layer: str = "snapshot") -> None:
    if type(golden) is not type(fresh):
        drift.append(DiffItem(layer, path or "root", "type",
                              type(fresh).__name__, type(golden).__name__))
        return
    if isinstance(golden, dict):
        keys = set(golden) | set(fresh)
        for key in sorted(keys):
            sub = f"{path}.{key}" if path else key
            if key not in golden:
                drift.append(DiffItem(layer, sub, "unexpected_field",
                                      fresh[key], None))
            elif key not in fresh:
                drift.append(DiffItem(layer, sub, "missing_field",
                                      None, golden[key]))
            else:
                _localize_json_diff(golden[key], fresh[key], sub, drift, layer)
    elif isinstance(golden, list):
        if len(golden) != len(fresh):
            drift.append(DiffItem(layer, path or "root", "length",
                                  len(fresh), len(golden)))
        for i, (g, f) in enumerate(zip(golden, fresh)):
            _localize_json_diff(g, f, f"{path}[{i}]", drift, layer)
    elif golden != fresh:
        delta = None
        try:
            # actual (fresh) minus golden, matching DiffItem semantics
            delta = str((Decimal(str(fresh)) - Decimal(str(golden)))
                       .quantize(Decimal("0.01")))
        except Exception:  # noqa: BLE001
            pass
        drift.append(DiffItem(layer, path or "root", "value",
                              actual=fresh, expected=golden, delta=delta))
