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
