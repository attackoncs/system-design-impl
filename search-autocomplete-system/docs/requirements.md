# Requirements: Search Autocomplete System

Source: root `System Design Interview.md`, Chapter 14, Design A Search Autocomplete System.

## Functional requirements

- R1: Match only the beginning of a query and return at most five suggestions,
  ordered by descending historical frequency, then lexicographically for ties.
- R2: Support lowercase English letters and spaces (the chapter includes phrases).
  Normalize case/whitespace; bound queries and prefixes to 50 characters. Empty
  prefixes return no suggestions. Reject other characters; no spelling correction.
- R3: Append completed-search events with timestamp and stable event IDs to durable
  storage. Retrying an event must not count it twice; conflicting reuse is rejected.
- R4: Aggregate an explicit UTC time window and build immutable, versioned snapshots
  offline. A weekly window is the default reference use case; suggestion requests
  must not update search frequencies.
- R5: Publish snapshots atomically. Readers observe one complete generation. A
  failed build/publication leaves the previous generation available. Restart loads
  the latest published snapshot.
- R6: Cache top five at trie nodes and provide a bounded request cache keyed by
  generation and filter revision. Cache eviction/miss must preserve correctness.
- R7: Block/unblock specific suggestions immediately at query time, including
  cached responses. Exclude blocked queries from subsequent snapshot builds.
  Blocking is an operator-supplied rule, not an automated content classifier.
- R8: Partition lexicographically sorted queries into balanced shards and keep a
  versioned shard map. Short prefixes spanning ranges must merge global top five.
- R9: Provide actual HTTP query nodes and a coordinator that contacts shard replicas,
  fails over on unavailable replicas and rejects mixed snapshot versions. Missing
  shards return unavailable rather than an incorrectly ranked partial response.
- R10: Include runnable local/network examples, English/Chinese READMEs, tests and
  a measured latency tool. Deployment and scale limits must be explicit.

## Validation and boundaries

- Chapter target: under 100 ms response, about 24,000 average / 48,000 peak QPS and
  10 million daily users. A local measurement is not proof of those capacities.
- Tests must cover ranks, phrases, ties, invalid input, event deduplication, time
  windows, snapshot atomicity/restart, cache eviction, filtering, multi-shard merge,
  actual HTTP replica failure and version mismatch.
- Initial storage is SQLite on one host; query replicas load immutable snapshots.
  Multi-host object storage, distributed log ingestion, consensus metadata, automatic
  infrastructure provisioning and live trending are extensions, not claimed features.
- Do not commit requirements.md/tasks.md unless the user changes the existing policy.
