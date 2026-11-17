"""Independent quality-assurance layer for invoice reconciliation.

This package is test-only infrastructure. It never imports for side
effects and it does not modify the production package under
``src/evidence_invoice_reconcile``. Components:

  harness           stdlib property-testing framework with shrinking
  generators        domain-aware invoice/payment payload generators
  oracle            independent Decimal reference (amount) model
  diffs             four-layer differential comparison + golden snapshots
  fault_injection   adversarial/illegal input injectors
  mutation*         AST mutation engine, worker subprocess and runner
  corpus            deterministic shared corpus
  test_properties   unittest integration of all properties
"""
