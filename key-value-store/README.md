# Distributed Key-Value Store — Chapter 7

A Dynamo-inspired learning implementation with real asynchronous gRPC replication,
causal siblings/tombstones, quorum responses, gossip, durable hinted handoff,
Merkle repair and WAL/MemTable/SSTable storage. [中文](README_CN.md).

The latest review passes **417 tests**, including actual loopback RPC, fault and
restart cases. [Chapter coverage and remaining work](docs/chapter07-review.md),
[design](docs/design.md), [raw measurements](benchmarks/REPORT.md).
Automatic load-based provisioning and full multi-datacenter operation remain
unfinished; this is not a claim that every deployment requirement is complete.

## Install and run

Python3.10+. Install the local sibling explicitly before this project:

```sh
pip install -e ../consistent-hashing
pip install -e ".[dev]"
python -m pytest tests -q
python examples/demo.py
python examples/repair_demo.py
python tools/benchmark.py --output benchmarks/local.json
```

Verified on Python3.13.9, grpcio/grpcio-tools1.84.0, protobuf7.36.2.
Bindings require grpcio>=1.84.0 and protobuf>=7.35.1,<8; regenerate with
`python tools/generate_proto.py`. Generated bindings contain no handwritten logic.
On Windows use UTF-8 output (`PYTHONIOENCODING=utf-8`) for the original component
demo's Unicode text. It illustrates components; repair_demo uses actual gRPC.

The review disables unrelated pytest plugin autoload and explicitly loads
pytest_asyncio.plugin. Its existing default cache/temp folders are inaccessible,
so it uses no:cacheprovider and a fresh workspace --basetemp. With-Werror both
asyncio_default_fixture_loop_scope and asyncio_default_test_loop_scope are set to
function. No project tests or warnings were skipped/filtered in the final run.

## Single-node API

Separate **all three storage paths** for every node; data_dir alone does not
rewrite wal_dir/sstable_dir defaults. A single node also needs N=W=R=1.

```python
import asyncio
from pathlib import Path
from kv_store import KVNode, NodeConfig
from kv_store.config import StorageConfig, NetworkConfig, ReplicationConfig

async def main():
    data = Path("./data/single")
    node = KVNode(NodeConfig(
        node_id="single",
        storage=StorageConfig(str(data), str(data / "wal"), str(data / "sstables")),
        network=NetworkConfig("127.0.0.1", 0),
        replication=ReplicationConfig(1, 1, 1),
    ))
    await node.start()
    try:
        written = await node.put("hello", b"world")
        read = await node.get("hello")
        assert read.found and read.value == b"world"
        await node.delete("hello", read.vector_clock)
    finally:
        await node.stop()

asyncio.run(main())
```

Values are bytes, up to10KiB; an empty byte value is distinct from deletion.
Keys are nonempty UTF-8 strings up to256bytes. Conflicting causal versions return
siblings, including tombstones. Merge all sibling clocks and pass that context
with a resolving write. A failed quorum/cancelled request can still have effects;
there is no transaction rollback or business idempotency key.

## Multi-node use

Create nodes with unique IDs and independent directories, listen on reachable
host:port addresses, and set ClusterConfig.seed_nodes to real seed addresses.
Default replication is N=3/W=2/R=2, requiring enough distinct physical replicas.
Wait for membership convergence before relying on a stable placement set.
KVClient(ClientConfig(seed_nodes=[...])) is an async context manager supporting
put/get/delete. KVNode supports explicit ConsistencyLevel values; clients use
strings such as "all". See [the owned loopback lab](tools/lab.py).

NodeConfig.node_virtual_nodes configures capacity weighting; node_data_centers
prefers different configured failure domains. Every node must share these maps.
Unknown nodes are treated as one unknown domain. This selection algorithm is
not evidence of WAN or datacenter outage tolerance.

W+R>N means quorum intersection for a stable replica set; it does **not** prove
linearizability with causal conflicts, sloppy quorum or membership changes.
Writes fan out concurrently and remaining bounded RPC tasks continue after quorum.
The coordinator reserves capacity before its first await, including same-key
lock waiters; its admission budget is256 replica-task units.

## Repair and durability changes

- Merkle roots keep the old hash format, updating only one path of O(log B)
  internal nodes. Bucket digest work still depends on the bucket's keys.
- MerkleInspect compares at most64 heap nodes per RPC. Differing buckets use
  sorted cursor pages with at most128 records and a byte budget capped at2MiB
  and the configured channel size. One oversized record fails explicitly.
- Pages are pinned to an expected remote root. Mutation returns ABORTED and the
  next round retries safely. Pull finishes before push; topology is pinned locally.
  This is eventual, retryable repair, not a global snapshot or topology consensus.
- Local commits finish WAL/memtable/Merkle work before cancellation escapes.
  File publication writes/fsyncs a temporary file, then atomically replaces the
  final SSTable before truncating WAL; compaction retires old inputs afterwards.
  POSIX directory sync is used where supported; Python has no portable Windows
  directory fsync, and no physical power-loss experiment was performed.
- Internal storage timestamps progress per key despite wall-clock rollback.
  Reads/rebuild choose the greatest per-key timestamp across candidate layers;
  physical creation times do not determine the winner. This costs extra reads.
- Hints are frozen before SQL locking; delivery ACK deletes only the exact
  captured row/payload, protecting against SQLite row-ID reuse.

The actual two-node example transfers196608 application bytes through a65536-byte
message limit in5pages, preserves a tombstone and converges. Its next round only
compares one root and transfers no data. See [recorded output](benchmarks/repair-2026-10-27.json).

## Boundaries and compatibility

All nodes using the new repair path must upgrade the two added RPC methods.
Legacy MerkleTreeSync remains for compatibility but still has a whole-response
message limit and is no longer used by runtime repair. Disk envelope format1 is
unchanged, with stricter validation: exact fields, no duplicate JSON fields,
finite0..1e12 timestamps and protobuf-compatible signed64-bit counters.
Records are limited to1MiB/256siblings. Old raw-value directories require explicit
export/import; they cannot recover causal metadata never saved by old versions.
Corrupt final SSTables now fail startup instead of being silently ignored.
Keep damaged files for recovery. Tombstones have no automatic safe GC.

SSTable objects currently cache their whole files, startup/rebuild scans data,
and compaction is not streaming. The tree indexes all locally stored records;
nodes with different replica responsibilities can keep comparing nonshared buckets.
Virtual nodes do not split one hot key. There is no automatic provisioning/drain
barrier, topology epoch protocol, TLS/authentication or real cross-region failover.
See [the coverage review](docs/chapter07-review.md) for exact remaining work.
