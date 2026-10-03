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

Snapshot generation contains frequency records, balanced lexicographic shard ranges
and build-window metadata. Publication commits all shard data and the active version
in one transaction. Existing readers hold immutable tries for the prior generation
until the new generation loads. Invalid builds never switch the active pointer.
Generation IDs are monotonic; old generations remain available for in-flight requests.

The event log remains append-only. Filters are durable rule records with a monotonic
revision. Physical exclusion happens in a subsequent snapshot; historical logs remain
available for auditing/rebuild. Query-time rules override cached results immediately.
HTTP queries must carry the coordinator's version/revision. A replica that cannot
serve that combination returns unavailable; a query never silently merges generations.

## Trie and request cache

Each character edge forms a prefix; terminal nodes carry query/frequency. Every node
caches the best five descendants. Normal lookup costs `O(prefix length + k)` with
prefix length bounded at 50 and `k=5`. Memory trades off against subtree traversal.
Filtering a cached candidate may require traversing the affected subtree to refill
the top five; the bounded LRU response cache amortizes that path. A later snapshot
physically excludes blocked terms, restoring the normal lookup path. Worst-case
filtered fallback is linear in subtree size and is stated explicitly.

LRU entries include version, rule revision and prefix. Clearing/evicting the cache
does not touch durable data. Tries are reconstructed from serialized frequency
records rather than untrusted pickle files. All HTTP input is size bounded.

## Shards and replicas

Sort queries by text and partition into approximately equal query-count ranges.
The shard map stores inclusive lower and exclusive upper bounds. A prefix range can
overlap multiple shards; the coordinator fans out only to overlapping ranges and
merges their top-five lists. Returning top five from each shard is sufficient for
the global top five when ranking and filtering are identical.

Shard query nodes expose bounded HTTP GET operations. Each shard can have multiple
endpoints. The coordinator tries replicas with finite timeouts, validates version,
filter revision and shard identity, and returns unavailable if any required shard
has no valid replica. This guarantees correct complete rankings over availability
of partial results. HTTP examples run distinct OS processes on loopback.

## Layout and operating limits

`src/search_autocomplete/`: trie, store, service, HTTP transport and CLI.
`examples/`: local batch/query and multi-process network demos.
`tests/`: deterministic correctness and actual HTTP failover tests.
`tools/benchmark.py`: configurable local latency measurement.

The standard-library implementation targets Python 3.9+. SQLite is shared by local
processes; it is not distributed HA storage and must not be shared via arbitrary
network filesystems. Production deployment should replace snapshot/log/metadata
storage, distribute replicas across failure domains, and add authenticated/TLS
administration. Weekly scheduling is an operator invocation of the batch CLI;
real-time trends, Unicode and country-specific rankings remain explicit extensions.
