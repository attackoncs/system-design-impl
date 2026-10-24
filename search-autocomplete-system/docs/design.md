# Design: Search Autocomplete System

## Chapter mapping

The chapter separates collection/building from serving. This module implements that
separation with durable SQLite event logs, a batch worker, immutable trie snapshots,
query caches, a shard map and actual HTTP query replicas/coordinator.

```text
completed searches -> append-only events -> window aggregation -> batch worker
                                                               |
                                            atomic versioned snapshot + shard map
                                                               |
                                      shard replicas: trie + top-five node caches
                                                               |
client -> HTTP coordinator -> relevant shard replica(s) -> global ranked top five
               |                        |
       current published version   persistent block rules + revision
```

## Data and consistency

Completed searches carry a caller-provided event ID, normalized query and UTC epoch
timestamp. SQLite transactions reject conflicting ID reuse and count exact retries
once. Suggestion lookups do not collect analytics; callers record completed searches
separately. Batch aggregation uses `[start, end)` bounds and deterministic tie ordering.
`Collector` optionally selects one in N events using a stable event-ID hash, keeping
retries consistent across processes. Sampling factors must stay fixed for a stream;
frequencies represent sampled events rather than estimated total traffic.

`Store.aggregate()` materializes immutable window counts with a filter revision and
event watermark. `Store.aggregation()` retrieves them after restart. Builds may
consume an existing matching aggregation. A separate WAL read transaction captures
one consistent input, while concurrent writers continue accepting new events for
later builds. In-memory databases serialize the input read because they cannot use
WAL. Trie construction and JSON encoding happen outside the Store/cache write locks.

Snapshot generation contains frequency records, balanced lexicographic shard ranges
and build-window metadata. Publication commits all shard data and the active version
in one transaction. Existing readers hold immutable tries for the prior generation
until the new generation loads. Invalid builds never switch the active pointer.
Build tickets make generation IDs monotonic, with possible gaps after failures.
A superseded build cannot overwrite a newer publication; a changed filter revision
also rejects publication. Old generations remain available for in-flight requests.

Accepted event contents are immutable. Filters are durable rule records with a
monotonic revision. Query-time rules override cached results immediately, with a
final revision fence even for cache hits. Rules changing during a lookup can cause
an unavailable response for retry. Subsequent snapshots exclude blocked terms.
Each Store caches its immutable block set by the persisted revision, observing
cross-process changes without scanning every blocked row on each warm request.
The final fence reads only the revision metadata row.
HTTP queries must carry the coordinator's version/revision. A replica that cannot
serve that combination returns unavailable; a query never silently merges generations.

`purge_blocked()` is a separate maintenance operation deleting blocked source rows
and all prepared aggregation artifacts. Hashed-query receipts preserve event-ID
deduplication across deletion/restart. Serving snapshots remain immutable; they
continue obeying live filters but can reveal retained terms after an explicit
unblock. Source removal and historical snapshot retention are separate concerns.

## Trie and request cache

Each character edge forms a prefix; terminal nodes carry query/frequency. Every node
caches the best five descendants. Normal lookup costs `O(prefix length + k)` with
prefix length bounded at 50 and `k=5`. Memory trades off against subtree traversal.
Nodes and child mappings are immutable after construction. Filtered refill uses a
best-first priority queue and cached ranking bounds to prune lower-ranked branches.
Arbitrary large block sets can still visit the entire matching subtree, with heap
overhead; filtered lookup is not guaranteed constant-time. A filtered rebuild
restores the normal cached path.

LRU entries include version, rule revision and prefix. Clearing/evicting the cache
does not touch durable data. Tries are reconstructed from serialized frequency
records rather than untrusted pickle files. All HTTP input is size bounded.
Snapshot/Trie misses use a fixed set of lock stripes to coalesce loads; expensive
loading and construction do not hold the global LRU lock. Warm readers continue
during cold builds. Cache capacities are counts, not strict byte/RSS limits.

## Shards and replicas

Sort queries by text and partition into approximately equal query-count ranges.
The shard map stores inclusive lower and exclusive upper bounds. A prefix range can
overlap multiple shards; the coordinator fans out only to overlapping ranges and
merges their top-five lists. Returning top five from each shard is sufficient for
the global top five when ranking and filtering are identical.

Shard query nodes expose bounded HTTP GET operations. Each shard can have multiple
endpoints. The coordinator tries replicas with finite timeouts, validates version,
filter revision, shard identity and lexical ownership, and returns unavailable if any required shard
has no valid replica. This guarantees correct complete rankings over availability
of partial results. HTTP examples run distinct OS processes on loopback.

The coordinator bounds all unfinished shard tasks by its concurrency limit;
timed-out I/O retains its slot until it actually exits. One request budget covers
admission and replica failover, including slow header/body streams interrupted by
socket shutdown. Metadata reads consume that budget, but SQLite/Store lock waits
and OS DNS resolution cannot be forcibly interrupted and can exceed it. Close
rejects new work, cancels pending futures and drains owned workers. HTTP server
close joins active request handlers before the CLI closes the Store.

Responses use `Cache-Control: no-store`. The book's optional one-hour browser cache
would hide new removal rules; generation-pinned URLs alone do not solve that issue.

## Layout and operating limits

`src/search_autocomplete/`: collection, worker, trie, store, service, HTTP and CLI.
`examples/`: local batch/query and multi-process network demos.
`tests/`: deterministic correctness and actual HTTP failover tests.
`tools/benchmark.py`: configurable local latency measurement.

The standard-library implementation targets Python 3.9+. SQLite is shared by local
processes; it is not distributed HA storage and must not be shared via arbitrary
network filesystems. Production deployment should replace snapshot/log/metadata
storage, distribute replicas across failure domains, and add authenticated/TLS
administration. `SnapshotWorker` builds immediately, then waits on a stop Event
between trailing-window builds (seven days by default). Transient SQLite or
publication conflicts retry after five seconds. Optional source cleanup runs before
building. Scheduling state is not a durable distributed scheduler; process restart
starts another build. Stop prevents future builds and lets an active build finish.
Historical events/snapshots/aggregations/receipts require a retention policy.
Real-time trends, Unicode and country-specific rankings remain explicit extensions.

See [chapter audit](chapter14-review.md) and [Trie review](trie-review.md).
