# Consistent Hashing — Chapter 6

<!-- LEARNING_GUIDE_START -->
## Code reading guide

[Learn through a concrete example: architecture, state changes and annotated source (Chinese)](docs/learning-guide_cn.md).
<!-- LEARNING_GUIDE_END -->

Hash-ring placement with virtual nodes, immutable read views, atomic weight
changes and streaming migration selection. Runtime dependencies: standard library
only. [中文](README_CN.md) · [chapter review](docs/chapter06-review.md) ·
[design](docs/design.md) · [measurements](benchmarks/REPORT.md).

The chapter's core algorithms were already implemented. This review adds missing
key filtering, one-pass batch APIs, safe weight updates, resource validation and
faster bulk construction/range planning. **154 tests pass**, including the
existing Hypothesis properties; an independent four-process probe verifies
deterministic primary/replica selection.

## Run

```sh
pip install -e ".[dev]"
python -m pytest tests -q
python examples/demo.py
python examples/chapter_comparison.py
python tools/process_probe.py --output benchmarks/process-local.json
python tools/benchmark.py --output benchmarks/local.json
```

Python3.9+ source syntax, executed on Python3.13.9. Optional dev dependencies are
pytest and Hypothesis. In the review environment, inaccessible existing pytest
cache/temp directories require `-p no:cacheprovider` and a fresh `--basetemp`
inside `.test-tmp`; unrelated plugin autoload is disabled. All154 tests run with
`-W error`, without skips or warning filters.

## Routing and snapshots

```python
from consistent_hashing import ConsistentHashRing, compute_distribution

ring = ConsistentHashRing(["web-1", "web-2", "web-3"], num_virtual_nodes=150,
                          max_virtual_nodes=1_000_000)
print(ring.get_node("user:42"))
print(ring.get_nodes("user:42", count=2))  # distinct candidate servers

view = ring.snapshot()
stream = ring.iter_assignments(f"key-{i}" for i in range(1000))
ring.add_node("web-4")
print(list(stream))  # topology captured at method call, before the addition
print(view.get_keys("web-1", ["user:42", "user:43"]))
print(compute_distribution(view, (f"key-{i}" for i in range(1000))))
```

SHA-1 is the default, with the full160-bit integer space. MD5 and SHA-256 use
their full128/256-bit spaces. A custom callable receives strings and returns
exact Python integers (not bool). It must remain deterministic, thread safe and
stable for retained snapshots; it must not mutate this ring or wait for its
writers. Custom integer positions may be signed; no modulus is imposed.

Virtual nodes hash `node#0` through `node#(count-1)`. Entries retain collisions,
ordered by position, physical name and virtual index. Lookup preserves the
historical **strict-clockwise** convention: `bisect_right` selects the first
position strictly greater than the key, then wraps. Exact equality skips every
entry at that position. Changing to inclusive lookup would change routing.

Readers capture topology under a short publication lock, then search immutable
tuples. A retained view remains valid after live node removal. Views/iterators
retain topology memory until released; no data keys are stored in the ring.
An empty ring raises on routing/batch routing, while `get_nodes(key, 0)` returns[].

## Membership and weights

```python
ring.add_node("large", num_virtual_nodes=300)
before = ring.snapshot()
ring.set_virtual_nodes("large", 600)
ring.remove_node("web-2")
```

Construction accepts a sequence of unique nonempty node names; strings/sets are
rejected as ambiguous containers. Bulk construction hashes all entries and sorts
once. Addition, removal and weight changes publish one complete replacement
under the writer lock. Hash/capacity failures leave the old state intact.

Increasing a weight adds only new virtual indices; decreasing removes highest
indices. Retained positions are never rehashed, and there is no remove/add gap.
Setting the existing weight is a no-op. Counts must be positive integers, and
total entries are capped by `max_virtual_nodes` (default1,000,000); capacity is
checked before hashing and again against the current writer state. This is a
per-topology limit, not a bound on all retained historical snapshots.

## Find affected ranges and keys

```python
from consistent_hashing import affected_ranges, iter_migrations

before = ring.snapshot()
ring.add_node("new")
after = ring.snapshot()
for interval in affected_ranges(before, after):
    print(interval.start, interval.end, interval.previous_owner, interval.new_owner)
for item in iter_migrations(before, after, candidates_from_storage):
    print(item.key, item.previous_owner, item.new_owner)
```

Both inputs must be explicit snapshots using the same hash callable instance.
Intervals are disjoint **[start,end)** under strict-clockwise routing; start>end
wraps, equal bounds mean the full ring, and None owners represent an empty ring.
Adjacent intervals are retained. Planning merges position boundaries and walks
both ordered topologies; it performs no key scan or hash calls.

`iter_migrations` plans immediately, then hashes each supplied candidate once and
binary-searches affected intervals. It streams records without building two full
assignment maps. Duplicate candidates are retained; no-change plans consume none.
`get_keys(node, candidates)` filters supplied keys on one snapshot and raises for
unknown nodes; it cannot discover stored keys without an external index or scan.

`compute_distribution` pins a topology and accepts one-pass iterables, counting
duplicate samples. `compute_redistribution(..., strict=True)` checks complete
assignment maps and counts explicit None-to/from-owner changes; its defaultFalse
preserves legacy behavior of ignoring missing/unassigned pairs. Its moved map
counts distinct keys. Caller-supplied maps must remain stable during analysis.

## Guarantees and costs

Adding a node only moves primary keys to that node; removing one only moves keys
owned by it. Equal-capacity uniform-hash expectations are about1/(N+1) on addition
to N servers and1/N on removal, **not hard bounds** for finite samples. Virtual
nodes improve placement balance statistically; a single hot key still has one
primary owner, and key balance is not request/byte balance.

Let V be total entries, K the changed node's entries and B union boundaries:

| Operation | Cost, excluding the application hash function |
|---|---|
| Single owner | O(log V); live access also captures a view |
| Replica candidates | O(log V + V) worst case, skips duplicate physical nodes |
| Bulk construction | O(V log V), one sort |
| Add/grow weight | copy plus sorting changed entries / merging two ordered runs |
| Remove | O(V + K log K), including index-ordered return positions |
| Shrink weight | O(V) |
| Range plan | two-run boundary merge plus O(V_before+V_after+B) sweep |
| Migration candidates | plan plus O(keys * log(max(2, changed intervals))) |

The optimized ordered-run merges use CPython's adaptive built-in sort, avoiding
Python heap iteration. A conservative sort upper bound remains O(V log V) on
other implementations. State memory is O(V); writers and retained snapshots can
temporarily hold multiple versions.

Measurements show faster bulk construction and planning, with slightly slower
lookups/additions; see [raw comparisons](benchmarks/REPORT.md). Thread safety does
not imply multicore throughput scaling. No physical migration, replication,
membership discovery, consensus or failure detector is provided.
