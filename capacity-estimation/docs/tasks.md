# Chapter 3 Tasks

Status: reference implementation reviewed; 16 tests and six actual CLI examples pass. requirements.md/tasks.md are kept local only, per the existing convention.

- [x] Explicit SI/IEC units and Decimal input validation.
- [x] Demand, storage, network, cache, node, concurrency, and availability formulas.
- [x] Scenario JSON, assumption echo, sensitivity analysis, and CLI.
- [x] Original book examples, read-heavy example, tests, bilingual README, and verification records.
- [x] Clarified boundaries between the historical latency table, estimation, and real-world measurement.

## 2026-11-06 chapter/source review

- [x] Read the chapter, trace numerical validation/formulas/CLI with CodeGraph, and compare fixed upstream measurement code.
- [x] Reproduce and fix Decimal rounding below an exact whole-node boundary and loss of a spare at large integer counts.
- [x] Calculate node ceilings with exact rational inputs, retain integer totals, and add two prior-failure regressions.
- [x] Verify 16 tests, six CLI commands, bilingual documentation and explicit measurement/production boundaries.
