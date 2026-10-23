# Chapter 06 consistent hashing: implemented design

The source of the coverage review is chapter 06 in the repository's System Design Interview.md. Named systems such as Dynamo and Cassandra are applications of the technique, not additional systems to implement in this chapter.

## Chapter coverage

| Topic | Implementation |
| --- | --- |
| Modulo rehashing problem | examples/chapter_comparison.py compares routing before/after expansion |
| SHA-1 space, hash ring, clockwise lookup | Full 160-bit hash; sorted virtual positions and wrapping binary search |
| Node/key mapping, addition/removal locality | ring.py and existing property tests |
| Unequal partitions and virtual nodes | Configurable per-node/default counts; distribution statistics |
| Virtual-node memory/balance tradeoff | Reproducible 1/100/200-node experiment |
| Find affected keys/ranges | migration.py topology-only affected_ranges planner |
| Scaling and hotspots | Placement only; one hot key is not split across servers |

## Responsibilities and concurrency

ring.py owns immutable topology state, RingSnapshot read views and serialized topology writers. snapshot() captures a coherent topology under a short publication lock. Retained snapshots support consistent batch routing without a per-key publication lock, and remain usable after live nodes are removed. They retain topology memory until released. Hash callables must be deterministic, thread safe and stable.

migration.py compares primary ownership across two snapshots; stats.py captures one snapshot for a distribution report, preventing mixed-version totals. hash_functions.py supplies deterministic string-to-integer functions. Existing public routing methods retain their behavior; get_nodes only selects distinct candidate nodes, it does not replicate data.

## Migration contract

```python
from consistent_hashing import ConsistentHashRing, affected_ranges
ring = ConsistentHashRing(["a", "b"])
before = ring.snapshot()
ring.add_node("c")
after = ring.snapshot()
ranges = affected_ranges(before, after)
```

Each immutable AffectedRange has start, end, previous_owner, new_owner and contains(position). Ranges are disjoint, clockwise **[start, end)**, following the existing strict-clockwise bisect_right convention. Equal key/node hashes route to the next greater position. start > end wraps; start == end denotes the whole ring. Adjacent ranges are not merged. Equal-position entries are retained and ordered by physical name and virtual index.

The planner uses the union of position boundaries, compares owners at each interval start and emits only changes. Empty-ring owners are None. Both snapshots must use the same hash callable instance; custom function equivalence cannot safely be inferred. Multiple intervening updates yield their net difference. Callers must coordinate data transfer and topology activation themselves; this is not a migration executor or a replica-set planner.

## Cost and evidence

For N ring entries and K added entries: lookup O(log N), distinct-replica scan worst-case O(N), copy/sort addition conservatively O((N+K) log(N+K)), removal conservatively O(N log N), and state memory O(N). For B union boundaries migration costs O(B log B + B log(N_before+N_after)), with O(B) output. Snapshot construction is a short reference capture. No throughput improvement is claimed without measurements.

Run `python -m pytest tests -q -p no:cacheprovider` and `python examples/chapter_comparison.py`. Full suite: **108 passed**, including 22 new cases for migration boundaries, wraparound, collisions, empty transitions, incompatible hash functions, retained views and statistics during topology deletion.

Deterministic experiment: 10 servers and 10,000 sample keys, 1/100/200 virtual nodes per server give 10/1000/2000 entries and standard-deviation/mean 1.01794/0.05789/0.06078. Adding server 11 moves 969 keys with consistent hashing versus 9066 with modulo. Finite samples do not guarantee monotonically improving balance or the book's illustrative ratios. Key balance is not traffic balance; a hot key still has one primary owner.

Membership discovery, failure detection, consensus, transfer execution and cross-machine load testing are outside this chapter. See design_cn.md for the Chinese coverage matrix and root DEEP_REVIEW_PROGRESS.md for sequential review status.
