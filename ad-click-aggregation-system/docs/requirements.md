# Requirements: Ad Click Event Aggregation

Based on Chapter 22 of《System Design Interview.md》in the root directory. The goal is a locally repeatable, verifiable reference implementation; the book's one billion events per day and 50,000 QPS peak are production design targets, not acceptance results measured on this machine.

1. Accept appended JSONL or HTTP click events carrying a stable `event_id`, with fields for advertisement, event time in seconds, user, IP, and a two-letter country code.
2. Query advertisement counts over complete-minute `[from,to)` ranges and Top N over the past M minutes, with defaults M=1 and N=100; support exact AND filtering by user/IP/country.
3. Bucket by event time, supporting out-of-order events, late-window corrections, and persistent deduplication; repeated clicks by the same user are not directly treated as duplicate events.
4. Persist raw partition logs and independent aggregation workers, with outputs and consumption checkpoints committed atomically. HTTP retries and worker restarts must not double-count.
5. Split hot advertisements by event identity, sum exactly across partitions before ranking, and never prune local candidates early.
6. Raw logs can be replayed page by page into a new database; an independent batch job reconciles against online dimensional results at a fixed checkpoint.
7. State explicitly that closed windows remain revisable; responses carry a checkpoint vector, watermark, backlog, and consistency boundaries; queries fail when any required partition is unavailable.
8. Provide authentication, bounded request/candidate budgets, capacity backpressure, verification of failures in real independent processes, and measured benchmark reports.

Core review: 2026-10-26, chapter lines9664–10506. Final executed validation is
recorded in [benchmarks/results.md](../benchmarks/results.md), with book coverage
in [chapter22-review.md](chapter22-review.md). Additional acceptance requirements:

9. Reuse a bounded query executor; validate every worker's lineage, position and
   totals, share an I/O deadline, fail closed on missing required partitions, and
   retain capacity until accepted operations actually exit.
10. Bound administrative replay/reconciliation time, events and dimensional state;
    verify source pages and old positions, freeze mutable inputs before writer locks,
    and atomically publish source checkpoints only after a validated remote ACK.
11. Keep HTTP admission, framing/JSON/query validation, authentication before body
    reads, direct outbound transport and shutdown ownership explicit. Bind before
    starting the worker, surface sanitized retry errors and drain accepted work.
12. Maintain English/Chinese READMEs, design, local requirements/tasks and reports
    when behavior changes. Historical counts are not current test evidence.

The second output queue/database writer, physical distributed MapReduce DAG,
external-sink exactly-once and production distributed batch engine remain outside
this implementation. The reference's atomic effect boundary is one worker's
counts, deduplication and input positions in the same local database transaction.

This chapter does not provide ad fraud detection, personal-information anonymization, payment/billing settlement, cross-machine high availability, dynamic online partition expansion, automatic archive cleanup, or a Kafka implementation. Raw events and deduplication identities are retained continuously within an explicit capacity budget; once capacity is reached, operational migration is required.
