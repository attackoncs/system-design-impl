# Chapter 3 — Explicit-assumption estimation model

The local source chapter introduces powers of two, operation latency, availability budgets and a hypothetical social-network workload. The executable scope is unit-safe arithmetic, transparent assumptions and sensitivity analysis. It does not fetch current provider SLAs, recommend hardware or treat historical latency constants as measured facts.

Use Decimal inside a local80-significant-digit context. Decimal inputs are strings/integers (or Decimal in the Python API), never binary floats. CLI JSON numeric fractions are parsed as strings. Inputs are finite, bounded in magnitude/length/exponent and validated semantically. Outputs retain decimal strings; recurring divisions are precision-limited. Unit parsing is case-sensitive and byte values must resolve to whole bytes. DecimalMB and binaryMiB are deliberately different.

Let M=MAU, d=daily-active ratio, w/r=writes/reads per DAU, p=peak multiplier, T=retention days, R=replicas, c=compression ratio, i=index overhead, h=hot fraction, s=cache item size:

| Result | Formula / units |
|---|---|
| DAU | M×d, estimated users |
| Daily writes / reads | DAU×w / DAU×r, operations/day |
| Average QPS | Daily operations÷86400 |
| Peak QPS | Average QPS×p |
| Average write bytes | Metadata bytes + media fraction×media bytes |
| Daily logical bytes | Daily writes×average write bytes |
| Retained logical bytes | Daily logical bytes×T |
| Physical bytes | Retained logical bytes×c×R×(1+i) |
| Cache bytes | Daily writes×T×h×s |
| Average ingress bits/s | Daily logical bytes×8÷86400 |
| Average egress bits/s | Daily reads×read response bytes×8÷86400 |
| Peak bandwidth | Average bandwidth×p |
| Serving nodes | ceil(peak total QPS×safety factor÷(node QPS×utilization)) |
| Total nodes | Serving nodes + explicit spare count |
| Peak inflight estimate | Peak total QPS×assumed mean latency in seconds |
| Downtime seconds | Period seconds×(1−availability percent÷100) |

Month/year durations are30/365 days. At zero demand serving nodes may be0 while configured spares remain. Ratios are bounded to[0,1], compression/utilization are strictly positive, peak/safety factors≥1, and counts requiring discrete units are whole. Extreme zero exponents are also rejected before formatting to prevent huge strings.

Cache fraction means a fraction of all retained write records, not a hit ratio; no read-QPS reduction is inferred from it. Index overhead is modeled on each compressed replica. Network excludes protocol/security overhead, retries, replication and cache traffic. Node QPS is a supplied mixed-workload assumption; latency is mean response time. These distinctions prevent presenting arithmetic as an independent system model.

Sensitivity analysis creates independent copies of one input scenario, replacing only a whitelisted field, at most20 scenarios. It does not optimize parameters or imply certainty. There are no live prices, stale hardware tables or third-party services.

Fourteen tests check reference arithmetic, decimal/binary conversion, compression/index/replica ordering, bandwidth bits, node ceilings, Little's law, availability boundaries, invalid inputs, sensitivity monotonicity and actual CLI processes. See `../benchmarks/REPORT.md` for commands and saved outputs. A production capacity plan still requires measured workloads, bottleneck identification, growth distribution, redundancy/failure domains and operational SLO definitions.

## Engineering organization (2026-10-23)

quantities.py owns unit parsing and an independent 80-digit half-even decimal context; scenario.py validates inputs; model.py owns formulas. Caller rounding, exponent limits and traps do not leak into calculations, including concurrent callers. Sweep containers are explicitly validated. Five additional regression cases cover these contracts.
