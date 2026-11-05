# Tasks: Distributed Email Service

Source: root-level `System Design Interview.md`, Chapter 24, chapter starting at line 11227.

Status: source/concurrency review complete (2026-11-05); 99 tests passed in 31.374s with real nine-process HTTP/SMTP. Production gaps remain documented.

## Scope

HTTP mail send/receive, attachments, read filtering, subject/sender/body search, and anti-spam/anti-virus interfaces; identity authentication is not part of this chapter's main scope.

## Implementation tasks

- [x] T1. Write requirements/design, separating metadata, object bytes, the delivery queue and the inverted index.
- [x] T2. Implement HTTP inbox/sent folders, read CAS, 50-item pagination and a static identity adapter.
- [x] T3. Implement durable send jobs, real local SMTP, MIME receiving and the gateway spool; public external delivery is not implemented or executed.
- [x] T4. Implement an independent HTTP object store, SHA256 checks and owner-only attachment authorization; 2 MiB per attachment, 8 MiB for the complete RFC message.
- [x] T5. Implement the Scanner interface, quarantine and scanner-unavailable retry; test marker rules only, quarantining everything by default when no scanner is configured.
- [x] T6. Implement dual-primary user sharding and two independent asynchronous read-only replicas; dedup/tombstone/lease recovery, without claiming that replicas can be automatically promoted to a writable primary.
- [x] T7. Implement the subject/sender/body inverted term index; primary deletes sync in the same transaction, replicas are eventually consistent.
- [x] T8. Verify real SMTP, lost acknowledgements, attachment verification, node restart, read-state conflicts, quarantine and replica search.
- [x] T9. Provide the nine-process example, Chinese and English READMEs, design and measured reports, and update the root status; production scale not verified.

## Acceptance criteria

- Repeatable tests exist for correctness, durable restart, idempotency and failure paths.
- Distributed capabilities must be verified with actual independent processes/networks; simulated implementations are labeled separately.
- External cloud services/vendors use replaceable adapters; without configured credentials, no claim that real services are verified.
- The original book's scale and availability numbers are design goals, not directly proven by local tests.
- Commit only after the user requests it; requirements.md/tasks.md under docs continue the no-commit convention.

## Acceptance evidence and remaining boundaries

- 6 core storage tests + 3 real nine-process network tests all pass (21.838 seconds).
- 40 emails all delivered via HTTP/SMTP and searchable from replicas, 40 unique gateway deliveries; replica search p95 76.077ms.
- After the primary exits, replicas remain readable and writes return 503; restart recovery after gateway acknowledgement loss and scanner unavailability shows no duplicates. See ../benchmarks/results.md.
- No real AV, public external delivery, primary/replica election, object/queue replication, automatic privacy retention or object GC yet; local measurements do not represent billion-user scale.
- This review does not commit or push changes; commit status is separate from chapter acceptance.

## 2026-10-26 review

- [x] Fixed-folder counts and folder messages; user-scoped cursor notifications.
- [x] Optional lineage/position read barrier and bounded watermark-aware metadata cache.
- [x] Freeze contracts, atomic replica replay and lease token/expiry fencing.
- [x] Bound HTTP/SMTP, direct RPC budgets, cancellation and owned thread cleanup.
- [x] 96 tests passed (30.741s, -W error); real nine-process demos and measurements.
- [ ] Production AV/spam, Cc/Bcc/threading, public deliverability and replicated HA/scale.

## 2026-11-05 source and concurrency review

- [x] Read the chapter and actual pinned Stalwart/maddy queue implementations; record [scope and differences](open-source-review_cn.md).
- [x] Reserve ordinary HTTP capacity by capping positive-wait notifications at `floor(3 * concurrency / 4)` per instance; overflow returns 503. One worker permits only zero-wait polls.
- [x] Retain notification permits until the owning response ends, including errors/cancellation; validate credentials and finite 0..2-second waits first.
- [x] Add three regressions: primary/API saturation plus real CAS wake-up, one-worker immediate polling and error cleanup. Before the fix, three subcases failed; all 16 HTTP tests now pass.
- [x] Strict full suite: 99 passed in 31.374s, no skips. Both nine-process examples and the 40-message/40-search benchmark passed; [raw results](../benchmarks/results.md).
- [x] Synchronize bilingual documents, correct the stale no-read-barrier statement, and update the learning guide.
