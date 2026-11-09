# 2026-11-09 hot-path efficiency

Replica polling and replica GET initialization checks now read only the
metadata row (`Store.watermark` / `MailboxViews.watermark`) instead of
`status()`, which counts all mails and pending outbox jobs. Full suite with
`-W error -p no:asyncio` (copied to /tmp): 98 passed / 1 failed before and after,
same environment-dependent SMTP network test.

# 2026-11-05 source and concurrency review

Python3.13.9, Windows11 build26100; all recipients are configured synthetic
local.test mailboxes. No external email or real AV service was used.
[Chapter review](../docs/chapter24-review.md),
[open-source audit](../docs/open-source-review_cn.md),
[raw performance](review-2026-11-05.json),
[raw feature evidence](features-2026-11-05.json).

## Correctness and concurrency

Baseline96 tests passed32.220s on2026-11-04; final **99 passed31.374s** on2026-11-05,
no skips, with `-W error`. Three new HTTP tests were added. Before the fix,
the new admission assertions failed for primary, API and concurrency1; after
the fix all16 HTTP tests passed2.105s. No speedup is inferred from suite timings.

With concurrency4, three actual HTTP notification requests wait at the same
SQLite mailbox cursor. A fourth positive-wait request is rejected503; invalid
waits still return400, a zero-wait query succeeds, and a real CAS read-state
write succeeds and wakes all three with version2. This runs both directly
against the primary and through an independent HTTP API server. Normal and
business-error completion permit subsequent positive-wait requests. The test
uses an entry-observer for synchronization; SQL and network work are real.
It is an in-process multi-server regression, not a nine-process saturation benchmark.

## Actual nine-process examples

Original demo: one unique inbox message, same sent ID on retry, matching
attachment bytes, replica search source, `scan_engine=test-markers-only`,
`external_delivery=false`; send-to-replica-search763.96ms.
Notification demo:363.629ms, one inbox event, same send ID, unread1 before CAS,
replica read-barrier version2, wrong-lineage409, resumed versions[2,3],
deletion version3, outsider events0 and one metadata-cache hit.

## HTTP/SMTP/object/replica measurement

| Metric | Observed |
| --- | ---: |
| Processes / primary shards / read replicas | 9 / 2 / 2 |
| Messages / sending clients | 40 / 4 |
| HTTP admission seconds / admitted messages per second | 0.971 / 41.21 |
| All delivered and replica-searchable seconds / messages per second | 5.143 / 7.78 |
| Unique SMTP deliveries / replica search matches | 40 / 40 |
| Search requests / clients | 40 / 4 |
| Search p50 / p95 milliseconds | 43.181 / 77.829 |
| Search requests per second | 90.75 |
| Sampled SQLite bytes / immutable object bytes | 1,187,840 / 18,000 |

Full tests and both examples completed before this measurement; the examples
and benchmark ran sequentially. A short localhost burst is not a controlled
before/after comparison, sustained capacity or production SLA. Repeated
same-query searches may benefit from the process-local metadata LRU and OS
caches. p95 uses sorted index37 among40 observations. Storage size includes
sampled SQLite sidecars and is not an atomic filesystem snapshot. Sender and
recipient occupy different primary shards; there is no cross-host scale proof.
Owned lab processes stop; private configs, disks and logs remain in ignored
`.runtime/lab-*` directories. SMTP idle timeout is not a total session budget;
objects/spool are unreplicated and no automatic write-primary failover exists.

## Static and documentation checks

Ruff E9/F passed for project source and the changed HTTP test; all three changed
Python files passed the format check. All30 project Python files parse with
Python3.10 syntax rules. The16 reviewed Markdown files contain525 valid local
links. All27 authored guides match generated output; all three chapter24
Mermaid diagrams rendered in an actual headless Edge browser. Scoped
`git diff --check` passed. Independent read-only source and document reviews
found no remaining confirmed issue in this change.

## Reproduce without replacing historical evidence

From the project directory with `PYTHONPATH=src` (set TEMP/TMP to an accessible
private directory when needed):

```powershell
python -W error -m unittest discover -s tests -v
python -W error examples/network_demo.py
python -W error examples/mailbox_updates_demo.py --output .runtime/features.json
python -W error tools/benchmark.py --output .runtime/benchmark.json
```

---

# Historical 2026-10-26 engineering review

96 tests passed in30.741s (`-W error`, no skips). Actual nine-process feature
notification342.560ms; original SMTP demo send-to-replica-search535.690ms.
Forty accepted/unique SMTP deliveries/replica search matches:40/40/40.
Admission1.002s,39.93messages/s; fully delivered and replicated4.973s,8.04messages/s.
Forty repeated searches/four clients:p50/p9543.571/58.355ms,93.04QPS.
Process-local metadata LRU now exists and may benefit repeated queries.
SQLite sampled bytes1,175,552; object bytes18,000. Sidecar size sampling is an
approximation, not an atomic storage snapshot. This small localhost measurement
is not a controlled speedup comparison, sustained capacity or durability SLA.

[Raw performance](review-2026-10-26.json), [feature evidence](features-2026-10-26.json),
[chapter review](../docs/chapter24-review.md).

---

## Historical 2026-10-20 report (before the metadata cache)

# Validation and measurements

Date: 2026-10-20. Python 3.13.9, Windows 11 build 26100. All recipients are synthetic configured `local.test` accounts. No email was sent to an external system. Raw data: [results.json](results.json).

## Correctness

```powershell
New-Item -ItemType Directory -Force .runtime/tmp | Out-Null
$env:TEMP = (Resolve-Path .runtime/tmp).Path
$env:TMP = $env:TEMP
python -m unittest discover -s tests -v
```

**Nine tests passed in 21.838 seconds**: six core storage tests and three real nine-process HTTP/SMTP tests. The lab sender and recipient deliberately occupy different primary shards. Replicas and objects are independent processes; SMTP uses actual `smtplib` and TCP protocol commands, not an in-memory mock transport.

| Area | Verified |
| --- | --- |
| Idempotency and tombstones | Duplicate receive/send returns the same ID; changed content conflicts; receiving a deleted delivery again does not restore it |
| Read conflicts | Concurrent HTTP read-flag writers with the same version produce one success and one 409; other users cannot inspect attachments |
| Search and pagination | Subject/sender/body field matching, AND terms, 50+5 pagination, and search removal on delete |
| Durable replication | Separate replica receives metadata/terms over HTTP; gaps/checksum/lineage errors fail; replay/reopen preserves read version and delete state |
| Jobs and leases | Reclaimed lease fences the old worker and survives primary reopen; SMTP acceptance updates sent status |
| Object authorization/integrity | Owner-scoped uploads; SHA256 verified before download; modified object bytes return 409; path-like keys rejected |
| Real MIME transfer | HTTP upload → saved MIME → SMTP DATA → recipient mailbox → matching binary attachment download |
| Missing SMTP confirmation | Gateway durably accepts then returns 451; sender retries after worker/gateway restart; one gateway spool row and one inbox copy remain |
| Scanner retry | Scanner-unavailable control keeps inbox empty and message queued; resetting control after restart delivers it |
| Node availability | Kill primary for recipient; router reads caught-up replica, rejects writes with 503; retained-disk primary/replica restart catches up |
| Quarantine and external boundary | Decoded attachment test marker routes to quarantine and download returns 403; external HTTP recipient rejected and SMTP RCPT returns 550 |

The first network run found a missing response-serializer import, which prevented HTTP health replies. The import was fixed; the corrected network suite and final full suite passed. Corruption, lost-confirmation and scanner-down controls are explicit fault injections; service kill/restart and TCP/HTTP transport are real processes. No real malware detector, public MTA, automatic election, host failure or replicated object durability is claimed.

## Demo

```powershell
python examples/network_demo.py
```

Observed nine processes, same sent ID on replay, one inbox message, matching binary attachment and successful replica body search. From initial send through duplicate send, catch-up and replica search/download: **684.07 ms** in one observation. The result reports `scan_engine=test-markers-only` and `external_delivery=false`.

## HTTP/SMTP/replica benchmark

```powershell
python tools/benchmark.py --output benchmarks/results.json
```

| Metric | Observed |
| --- | ---: |
| Processes / primaries / read replicas | 9 / 2 / 2 |
| Messages / sending clients | 40 / 4 |
| HTTP admission wall time | 1.711 s |
| All messages delivered and replica-searchable | 5.716 s |
| Admitted messages per second | 23.37 |
| Delivered and replicated messages per second | 7.00 |
| Unique SMTP deliveries / replica matches | 40 / 40 |
| Search requests / clients | 40 / 4 |
| Search p50 / p95 | 49.433 / 76.077 ms |
| Search requests per second | 79.22 |
| SQLite bytes / object bytes at measurement | 1,024,000 / 18,000 |

Send requests go through HTTP, saved object bytes, durable sender queues, TCP SMTP, durable receiving spool, recipient primary and asynchronous metadata/index replication. The benchmark asserts all forty accepted messages match the replica query. Queries return actual descriptors, not canned counts. The run overlapped some correctness-test execution, so contention can affect measurements. Forty same-query searches benefit from OS page cache, but there is no application result cache.

This short localhost burst is not a sustained capacity test, high-cardinality search benchmark, public SMTP test, scanner-performance measurement or availability SLA. Primary control tables, SMTP spool and objects are not replicated for automatic write recovery. No automated log retention or object GC exists. Public mail delivery, real AV/spam providers, replicated objects/spool, primary fencing/election, privacy lifecycle and multi-host scale/fault validation remain production work.
