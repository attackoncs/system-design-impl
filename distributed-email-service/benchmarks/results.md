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
