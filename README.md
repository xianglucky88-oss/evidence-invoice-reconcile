# Evidence Invoice Reconcile

Reconcile invoices with tax rules, reversal documents, and payment evidence.

## Modules

- `src/evidence_invoice_reconcile/models.py`: immutable result contract.
- `src/evidence_invoice_reconcile/parser.py`: input validation and record extraction.
- `src/evidence_invoice_reconcile/engine.py`: domain calculation only.
- `src/evidence_invoice_reconcile/report.py`: stable output envelope.
- `src/evidence_invoice_reconcile/pipeline.py`: orchestration boundary.
- `src/evidence_invoice_reconcile/cli.py`: deterministic JSON CLI.

## Run

```bash
PYTHONPATH=src python -m evidence_invoice_reconcile.cli
PYTHONPATH=src python -m unittest discover -s tests -v
```

The fixture is local and deterministic; no network or credentials are required.

## Independent QA layer

`qa/` contains a dependency-free quality-assurance layer (property tests
with counter-example reduction, an independent Decimal amount reference
model, differential golden snapshots across all four layers, fault
injection and AST mutation testing). It never modifies the code above.

```bash
python3 -I qa/run_tests.py                 # properties + snapshots + faults
python3 -I qa/run_all.py --mutations       # full campaign, writes qa/report.json
```

See [`qa/README.md`](qa/README.md) for the architecture and the findings
the layer documents (NaN propagation hazard, sub-cent rounding-location
gap, exception ordering).
