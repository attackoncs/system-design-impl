# Back-of-the-envelope Estimation — Chapter 3

A self-contained Decimal calculator for workload QPS, peak traffic, logical/physical storage, bandwidth, cache, serving nodes, Little's-law concurrency and availability budgets. Every result retains assumptions and unit labels. **These are estimates, not measured service capacity or SLA guarantees.**

## Run

Python 3.10+, standard library only. In this directory:

```powershell
$env:PYTHONPATH = 'src'
python -m unittest discover -s tests -v
python -m capacity_estimation.cli estimate --scenario examples/social-media.json
python -m capacity_estimation.cli estimate --scenario examples/read-heavy.json --sweep peak_factor=2,5,10
python -m capacity_estimation.cli units "1 MiB"
python -m capacity_estimation.cli availability 99.99
```

Nine tests verify formulas, units, invalid assumptions, rounding and real CLI JSON output. Installation via `pip install -e .` also exposes `capacity-estimate`.

## Scenarios and units

JSON inputs accept known fields only; omitted fields use defaults exposed in output `assumptions`. Decimal strings are recommended. CLI decimal JSON literals are parsed directly to decimal strings; the Python API rejects binary floats and booleans. Calculations use 80 significant Decimal digits, so repeating fractions are rounded at that precision. Outputs are decimal strings except integral node counts.

Sizes require explicit, case-sensitive units: `B`, `KB`…`PB` use powers of 1000, while `KiB`…`PiB` use powers of 1024. `1 MB = 1000000 B`; `1 MiB = 1048576 B`. Lowercase `b` denotes bits and is rejected by the byte-size parser. Fractional quantities must resolve to whole bytes (`0.5 KiB` is valid; `0.1 B` is not). Bandwidth output uses **bits per second**, multiplying payload bytes by eight.

| Input group | Fields |
|---|---|
| Demand | `monthly_active_users`, `daily_active_ratio`, `writes_per_dau`, `reads_per_dau`, `peak_factor` |
| Payload | `metadata_size`, `media_fraction`, `media_size`, `read_response_size` |
| Storage | `retention_days`, `replicas`, `compression_ratio`, `index_overhead_ratio` |
| Cache | `cache_fraction`, `cache_item_size` |
| Compute | `server_qps`, `utilization`, `safety_factor`, `spare_nodes`, `mean_latency_ms` |
| Availability | `availability_percent` |

Fractions lie in [0,1]; compression/utilization are greater than zero. Peak/safety factors are at least one. Replica count is a positive integer; MAU/retention/spares are nonnegative whole counts. Counts/rates are nonnegative and finite. Inputs have bounded numeric magnitude/exponents; zero with an extreme exponent is also rejected to avoid pathological formatting.

The default `social-media.json` reproduces the chapter's hypothetical media-only exercise: 300M MAU, 50% DAU, two writes/day, 10% media, decimal 1MB media, 1825-day retention. Results are 300M writes/day, roughly 3472.22 average write QPS, 6944.44 peak QPS, **30TB/day and 54.75PB retained logical media**. Metadata is deliberately zero and replication one, so these are not total production storage requirements or current numbers from any social network.

`read-heavy.json` additionally estimates reads, metadata, compression, indexes, three replicas, hot-record cache, compute headroom and one spare node. Sensitivity sweeps independently vary MAU, peak factor, media fraction, replicas, utilization or safety factor (maximum20 scenarios), preserving all other assumptions.

## Model boundaries

Storage applies compression to logical retained bytes, then multiplies by replicas and `(1 + index_overhead_ratio)`. Cache holds a fraction of retained write records at the supplied item size; it has no implicit replication/compression factor. Network uses uncompressed application payloads and excludes TLS/protocol overhead, replication, backups, cache traffic and retransmission.

Serving nodes are rounded **up** from peak mixed-workload QPS, supplied per-node QPS, utilization and safety factor; explicit spare nodes are added afterward. The supplied QPS must come from a relevant benchmark before this estimate is operationally useful. No linear scalability or failure-domain guarantee is inferred.

Concurrency uses assumed peak arrival rate × **mean** latency, not p99. Availability uses a30-day month/365-day year and no SLA exclusions; 99.99% yields8.64 seconds/day and3153.6 seconds/year. Neither a replica multiplier nor this arithmetic proves achieved availability.

The book's old operation-latency table is historical context, not a current hardware default. Supply measured latency through `mean_latency_ms`; use the chapter2 laboratory and later project benchmarks for workload-specific observations. See [design](docs/design.md) and [verification](benchmarks/REPORT.md).

## Numerical isolation and module responsibilities

`quantities.py` owns explicit units and decimal validation; `scenario.py` validates scenario fields; `model.py` calculates estimates. Existing public imports remain supported. Each calculation uses an independent 80-digit decimal context with half-even rounding, unaffected by caller/thread rounding, precision or traps. Sensitivity sweeps require a scenario dict and a list/tuple of values. The deep-review suite passes 14 tests, including concurrent callers with different numeric settings. See [review progress](../DEEP_REVIEW_PROGRESS.md).
