# Independent QA Layer — Invoice / Tax / Payment Reconciliation

A test-only, **zero-dependency** quality assurance layer that proves the
reconciliation pipeline conserves money at decimal boundaries and under
duplicate-payment conditions. It does **not** modify the production
package in `src/`; the reconciliation algorithm is never rewritten here.

The environment has no network access and no `pip`, `pytest`,
`hypothesis` or `mutmut`, so every technique is re-implemented on the
Python standard library (`unittest`, `ast`, `decimal`, `subprocess`,
`random` with fixed seeds).

## How to run

```bash
# property tests + differential snapshots + fault injection (fast, ~0.1s)
python3 -I qa/run_tests.py

# same, plus the full AST mutation campaign (~30s, 22 mutants, isolated
# subprocesses) and a machine-readable qa/report.json
python3 -I qa/run_all.py --mutations

# (re)write golden snapshots after an intentional contract change
python3 -I qa/run_tests.py --record
```

`-I` (isolated mode) is used so no ambient `json.py`, site-package or
environment variable can influence a run. The legacy suite still passes
unchanged: `PYTHONPATH=src python3 -m unittest discover -s tests`.

## Architecture

| File | Role |
| --- | --- |
| `harness.py` | Property-testing framework: deterministic strategies, a shrink lattice and greedy counter-example reduction. |
| `generators.py` | Domain generators — cent-exact money, tax rates rich in half-cent boundaries, reversal (`-R`) numbers, duplicate invoices/payments, shuffled order. |
| `oracle.py` | **Independent amount reference model.** Pure `Decimal` re-derivation of every monetary fact, sharing no code with the engine. Computes per-line net/tax/gross, both rounding policies, payment allocation and paid/partial/overpaid/unpaid status. |
| `diffs.py` | Four-layer differential comparison (`parser`, `engine`, `report`, `pipeline`) and golden snapshots. Every diff localizes to `invoice:<number>`, `payment:<token>` or `envelope:<field>` with declared/expected/delta intermediate amounts. |
| `fault_injection.py` | 17 adversarial input injectors (illegal amounts, NaN/Infinity, float artefacts, booleans, missing keys, wrong types, duplicates, sub-cent precision, empty payload). |
| `mutation.py` / `mutation_worker.py` / `mutation_runner.py` | AST-level mutation engine, kill-suite executed in an isolated package copy via subprocess, and the campaign runner. |
| `corpus.py` | Deterministic shared corpus (hand-built boundary payloads + 40 seeded generated payloads). |
| `test_properties.py` | `unittest` integration: 22 tests across all techniques. |
| `snapshots/*.json` | Golden differential snapshots (input, oracle view, all four layer views, localized diffs). |

## What is asserted

### Conservation and domain arithmetic
- **Identity** per invoice and in aggregate: `gross == net + tax` to the
  cent; `conservation_residual` is always `0.00`.
- **Tax relationship** `tax == round_half_up(net * rate)` at two decimal
  places, with mismatches localized per invoice including the raw
  unrounded product and the rounding delta.
- **Rounding location** is measured explicitly: the oracle computes both
  the per-line-rounded and summary-rounded totals and classifies the
  engine as `line` / `summary` / `indeterminate`.
- **Half-cent boundaries**, including negative half-cents
  (`1.05 × 0.13 = 0.1365 → 0.14`, `-120.55 × 0.07 = -8.4385 → -8.44`).
- **Zero amounts, negative adjustments, signed zero, sub-cent input.**
- **Payment allocation**: `gross - allocated == remaining`, statuses
  `unpaid / partial / paid / overpaid`, duplicate-payment keys and
  payments referencing unknown invoices.

### Grouping, ordering and reporting
- Duplicate invoice numbers are flagged except reversal documents
  (`-R`); the triple-duplicate and reversal cases are pinned.
- Duplicate payment tokens (string and `{number, amount}` forms) are
  preserved verbatim and flagged; grouping is order-independent.
- Input reordering preserves the canonical envelope; the engine's known
  *exception-list-order* quirk is characterized (content stable, order
  input-dependent) rather than silently assumed away.
- Envelope field set, string typing of money, 2-dp quantization,
  flattening of `PipelineResult.data`, defensive copies in the parser.

### Techniques (not just fixed examples)
- **Property-based testing** — 80 generated payloads per property with
  edge-biased cent sampling and fixed seeds.
- **Counter-example reduction** — every failure is greedily shrunk
  against the strategy lattice; e.g. a failing gross-sign property
  reduces to a single invoice with the exact minimal amount.
- **Differential snapshots** — engine vs. independent oracle, stored
  golden JSON with drift localized field-by-field.
- **Fault injection** — outcomes are classified `contracted` (safe
  behavior pinned by an assertion) or `hazard` (documented exposure).
- **Mutation testing** — 22 AST mutants targeting amount type, rounding,
  grouping, ordering and report-field defects. Result: **21 killed,
  1 equivalent** (the equivalent mutant is justified inline).

## Findings produced by this layer

1. **NaN propagation hazard (finance-relevant).** The engine performs no
   input validation. `Decimal("NaN")` for a tax value flows through
   arithmetic and is emitted as `"gross_total": "NaN"`, while
   `Infinity` is accidentally rejected only because `quantize` raises.
   The oracle rejects both up front. Recorded as a localized `hazard`
   in `report.json`; fixing it belongs to a production change, not the
   QA layer.
2. **Sub-cent rounding-location gap.** With 3-dp inputs
   (`100.005`, `0.005`) the engine accumulates raw values and quantizes
   only the summary (`113.01`); the per-line reference model quantizes
   each invoice first (`113.02`). The one-cent gap is pinned in
   `test_sub_cent_rounding_location_drift_characterized`. For ordinary
   2-dp money both conventions coincide, which is also asserted.
3. **Exception ordering depends on input order.** The exception
   multiset is deterministic; the list order follows invoice order and
   duplicate detection appends after the tax loop. Characterized so a
   future sort-based fix is an explicit, reviewed change.
4. **Payment evidence is passthrough-only.** The engine never allocates
   payments or computes paid/unpaid status; the oracle supplies the full
   allocation model and the differential report keeps the engine honest
   on everything it does contract to provide.
