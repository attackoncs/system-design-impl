#!/usr/bin/env python3
"""Demonstration of the distributed key-value store.

Shows single-node storage operations, vector clock conflict detection,
and describes multi-node cluster setup. This demo uses the StorageEngine
directly for single-node operations and simulates multi-node scenarios
using the replication layer components.

Run with: python examples/demo.py
"""

import asyncio
import sys
import tempfile
import time
from pathlib import Path

# Ensure the src directory is on the path for direct execution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Import modules directly — avoid triggering kv_store/__init__.py which
# imports KVNode (requires consistent-hashing to be installed).
# When running via `pip install -e .`, all deps are available and this works fine.
# For standalone execution, we import submodules directly.
try:
    from kv_store.config import StorageConfig, ReplicationConfig, NodeConfig
    from kv_store.storage.engine import StorageEngine
    from kv_store.replication.vector_clock import VectorClock
    from kv_store.replication.quorum import QuorumManager, ConsistencyLevel, QuorumConfig
except ImportError:
    # If the top-level __init__ fails due to missing consistent_hashing,
    # patch sys.modules to allow submodule imports
    import importlib
    import types

    # Create a minimal kv_store package without triggering __init__
    if "kv_store" not in sys.modules:
        kv_store_mod = types.ModuleType("kv_store")
        kv_store_mod.__path__ = [str(Path(__file__).resolve().parent.parent / "src" / "kv_store")]
        sys.modules["kv_store"] = kv_store_mod

    from kv_store.config import StorageConfig, ReplicationConfig, NodeConfig
    from kv_store.storage.engine import StorageEngine
    from kv_store.replication.vector_clock import VectorClock
    from kv_store.replication.quorum import QuorumManager, ConsistencyLevel, QuorumConfig

# Optional imports that require the full cluster stack (needs consistent-hashing)
try:
    from kv_store.cluster.gossip import GossipProtocol, MemberInfo, NodeStatus
    HAS_CLUSTER = True
except ImportError:
    HAS_CLUSTER = False


def print_separator(title: str) -> None:
    """Print a section separator."""
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}\n")


async def demo_single_node_operations() -> None:
    """Demonstrate basic put/get/delete on a single-node storage engine."""
    print_separator("1. Single-Node Storage Operations (LSM-Tree)")

    # Create a temporary directory for storage
    with tempfile.TemporaryDirectory() as tmp_dir:
        config = StorageConfig(
            data_dir=tmp_dir,
            wal_dir=f"{tmp_dir}/wal",
            sstable_dir=f"{tmp_dir}/sstables",
            memtable_size_bytes=1024,  # Small threshold for demo
        )

        engine = StorageEngine(config)
        await engine.start()
        print("  Storage engine started (WAL + MemTable + SSTable)")

        # --- Put operations ---
        print("\n  Writing key-value pairs:")
        pairs = [
            ("user:1001", b'{"name": "Alice", "email": "alice@example.com"}'),
            ("user:1002", b'{"name": "Bob", "email": "bob@example.com"}'),
            ("user:1003", b'{"name": "Charlie", "email": "charlie@example.com"}'),
            ("config:theme", b"dark"),
            ("config:lang", b"en-US"),
        ]

        for key, value in pairs:
            await engine.put(key, value, time.time())
            print(f"    PUT {key} = {value[:40].decode()}...")

        # --- Get operations ---
        print("\n  Reading keys:")
        for key in ["user:1001", "user:1002", "config:theme", "nonexistent"]:
            result = await engine.get(key)
            if result and result.found and not result.is_tombstone:
                print(f"    GET {key} = {result.value[:40].decode()}")
            else:
                print(f"    GET {key} = <not found>")

        # --- Delete operation ---
        print("\n  Deleting a key:")
        await engine.delete("user:1002", time.time())
        print("    DELETE user:1002")

        result = await engine.get("user:1002")
        if result and result.is_tombstone:
            print("    GET user:1002 = <tombstone - deleted>")

        # --- Demonstrate persistence (WAL replay) ---
        print("\n  Demonstrating crash recovery (WAL replay):")
        await engine.stop()
        print("    Engine stopped (simulating crash)")

        # Restart engine — WAL replay restores MemTable state
        engine2 = StorageEngine(config)
        await engine2.start()
        print("    Engine restarted — WAL replayed")

        result = await engine2.get("user:1001")
        if result and result.found:
            print(f"    GET user:1001 = {result.value[:40].decode()} (recovered!)")

        await engine2.stop()
        print("    Engine stopped cleanly")


async def demo_vector_clocks() -> None:
    """Demonstrate vector clock versioning and conflict detection."""
    print_separator("2. Vector Clocks & Conflict Detection")

    # --- Basic vector clock operations ---
    print("  Creating and incrementing vector clocks:")
    clock_a = VectorClock()
    clock_a = clock_a.increment("node-1")
    print(f"    Node-1 writes: {clock_a}")

    clock_a = clock_a.increment("node-1")
    print(f"    Node-1 writes again: {clock_a}")

    clock_b = clock_a.increment("node-2")
    print(f"    Node-2 writes (based on A): {clock_b}")

    # --- Causal ordering ---
    print("\n  Causal ordering (dominates):")
    print(f"    clock_b dominates clock_a? {clock_b.dominates(clock_a)}")
    print(f"    clock_a dominates clock_b? {clock_a.dominates(clock_b)}")

    # --- Conflict detection (concurrent writes) ---
    print("\n  Simulating concurrent writes (conflict):")
    print("    Scenario: Two nodes write to the same key independently")

    # Both nodes start from the same base clock
    base_clock = VectorClock(entries={"node-1": 1})
    print(f"    Base clock: {base_clock}")

    # Node-1 writes independently
    clock_from_node1 = base_clock.increment("node-1")
    print(f"    Node-1 writes: {clock_from_node1}")

    # Node-2 writes independently (from same base — didn't see node-1's write)
    clock_from_node2 = base_clock.increment("node-2")
    print(f"    Node-2 writes: {clock_from_node2}")

    # Detect conflict
    has_conflict = clock_from_node1.conflicts_with(clock_from_node2)
    print(f"\n    Conflict detected? {has_conflict}")
    print(f"    Node-1 dominates Node-2? {clock_from_node1.dominates(clock_from_node2)}")
    print(f"    Node-2 dominates Node-1? {clock_from_node2.dominates(clock_from_node1)}")

    # --- Conflict resolution via merge ---
    print("\n  Resolving conflict (client merges):")
    merged = clock_from_node1.merge(clock_from_node2)
    print(f"    Merged clock: {merged}")
    print(f"    Merged dominates Node-1? {merged.dominates(clock_from_node1)}")
    print(f"    Merged dominates Node-2? {merged.dominates(clock_from_node2)}")

    # --- Vector clock pruning ---
    print("\n  Vector clock pruning (max entries = 3):")
    clock = VectorClock(max_entries=3)
    for i in range(5):
        clock = clock.increment(f"node-{i}")
        print(f"    After node-{i} increment: {clock}")


async def demo_quorum_configuration() -> None:
    """Demonstrate tunable consistency via quorum parameters."""
    print_separator("3. Tunable Consistency (Quorum Configuration)")

    config = ReplicationConfig(n_replicas=3, w_quorum=2, r_quorum=2)
    qm = QuorumManager(config)

    print("  Default configuration: N=3, W=2, R=2")
    default_qc = qm.get_quorum_config()
    print(f"    Strongly consistent (W+R > N)? {default_qc.is_strongly_consistent()}")
    print(f"    W + R = {default_qc.w + default_qc.r} > N = {default_qc.n}")

    print("\n  Consistency levels:")
    for level in ConsistencyLevel:
        qc = QuorumConfig.from_consistency_level(level, n=3)
        strong = "yes" if qc.is_strongly_consistent() else "no"
        print(f"    {level.value:>8}: W={qc.w}, R={qc.r}, N={qc.n} "
              f"(strong consistency: {strong})")

    print("\n  Trade-offs:")
    print("    ONE (W=1, R=1):    Fastest, eventual consistency, risk of stale reads")
    print("    QUORUM (W=2, R=2): Balanced, strong consistency with N=3")
    print("    ALL (W=3, R=3):    Slowest, highest consistency, lowest availability")


async def demo_multi_node_cluster() -> None:
    """Describe and demonstrate multi-node cluster concepts."""
    print_separator("4. Multi-Node Cluster (Architecture Overview)")

    print("  A 3-node cluster setup:")
    print()
    print("    ┌──────────┐    ┌──────────┐    ┌──────────┐")
    print("    │  Node-1  │◄──►│  Node-2  │◄──►│  Node-3  │")
    print("    │ :50051   │    │ :50052   │    │ :50053   │")
    print("    └──────────┘    └──────────┘    └──────────┘")
    print("         │               │               │")
    print("         └───────────────┼───────────────┘")
    print("                         │")
    print("                    Gossip Protocol")
    print("                  (failure detection)")
    print()

    print("  To start a 3-node cluster programmatically:")
    print()
    print("    from kv_store import KVNode, NodeConfig")
    print("    from kv_store.config import NetworkConfig, ClusterConfig, StorageConfig")
    print()
    print("    # Node 1 (seed node)")
    print("    node1_config = NodeConfig(")
    print('        node_id="node-1",')
    print("        network=NetworkConfig(port=50051),")
    print('        storage=StorageConfig(data_dir="./data/node1"),')
    print("        cluster=ClusterConfig(seed_nodes=[]),")
    print("    )")
    print("    node1 = KVNode(node1_config)")
    print("    await node1.start()")
    print()
    print("    # Node 2 (joins via seed)")
    print("    node2_config = NodeConfig(")
    print('        node_id="node-2",')
    print("        network=NetworkConfig(port=50052),")
    print('        storage=StorageConfig(data_dir="./data/node2"),')
    print('        cluster=ClusterConfig(seed_nodes=["localhost:50051"]),')
    print("    )")
    print("    node2 = KVNode(node2_config)")
    print("    await node2.start()")
    print()
    print("    # Node 3 (joins via seed)")
    print("    node3_config = NodeConfig(")
    print('        node_id="node-3",')
    print("        network=NetworkConfig(port=50053),")
    print('        storage=StorageConfig(data_dir="./data/node3"),')
    print('        cluster=ClusterConfig(seed_nodes=["localhost:50051"]),')
    print("    )")
    print("    node3 = KVNode(node3_config)")
    print("    await node3.start()")


async def demo_node_failure_recovery() -> None:
    """Demonstrate node failure detection and recovery concepts."""
    print_separator("5. Node Failure & Recovery")

    print("  Failure detection via Gossip Protocol:")
    print("    - Each node sends heartbeats every 1 second (configurable)")
    print("    - If no heartbeat received for 5 seconds → node marked SUSPECTED")
    print("    - If still no heartbeat after 10 seconds → node marked DOWN")
    print()
    print("  When a node fails:")
    print("    1. Gossip protocol detects the failure")
    print("    2. Sloppy quorum routes writes to next healthy node")
    print("    3. Substitute node stores data with 'hint' for intended recipient")
    print()
    print("  When the node recovers:")
    print("    1. Node restarts and replays its WAL")
    print("    2. Gossip protocol detects the recovery (heartbeat resumes)")
    print("    3. Hinted handoff pushes stored hints back to recovered node")
    print("    4. Anti-entropy (Merkle tree) repairs any remaining inconsistencies")
    print()

    # Demonstrate with gossip protocol directly (if available)
    if HAS_CLUSTER:
        print("  Simulating gossip membership:")
        gossip = GossipProtocol(
            node_id="node-1",
            address="localhost:50051",
            gossip_interval=1.0,
            gossip_fanout=2,
            failure_timeout=5.0,
        )

        # Simulate receiving membership info from other nodes
        gossip.merge_membership([
            MemberInfo(node_id="node-2", address="localhost:50052",
                       heartbeat_counter=10, status=NodeStatus.ALIVE),
            MemberInfo(node_id="node-3", address="localhost:50053",
                       heartbeat_counter=8, status=NodeStatus.ALIVE),
        ])

        alive = gossip.get_alive_nodes()
        print(f"    Alive nodes: {[n.node_id for n in alive]}")
        print(f"    Node-2 status: {gossip.get_node_status('node-2')}")
        print(f"    Node-3 status: {gossip.get_node_status('node-3')}")
    else:
        print("  (Gossip simulation skipped — install consistent-hashing for full demo)")


async def demo_client_usage() -> None:
    """Show how to use the KVClient API."""
    print_separator("6. Client API Usage")

    print("  The KVClient connects to the cluster via gRPC:")
    print()
    print("    from kv_store.client import KVClient, ClientConfig")
    print()
    print("    config = ClientConfig(")
    print('        seed_nodes=["localhost:50051", "localhost:50052"],')
    print("        timeout=5.0,")
    print("        retry_count=3,")
    print("        retry_delay=0.5,")
    print("    )")
    print()
    print("    async with KVClient(config) as client:")
    print("        # Put a value")
    print('        resp = await client.put("user:1001", b\'{"name": "Alice"}\')')
    print(f"        # resp.success = True, resp.vector_clock = VectorClock(...)")
    print()
    print("        # Get a value")
    print('        resp = await client.get("user:1001")')
    print("        # resp.value = b'{\"name\": \"Alice\"}'")
    print()
    print("        # Handle conflicts")
    print("        if resp.has_conflict:")
    print("            for value, clock in resp.conflicting_values:")
    print('                print(f"Conflicting version: {value}")')
    print("            # Client resolves conflict and writes back")
    print('            await client.put("user:1001", resolved_value,')
    print("                             vector_clock=merged_clock)")
    print()
    print("        # Delete a key")
    print('        resp = await client.delete("user:1001")')
    print()
    print("        # Tunable consistency per operation")
    print('        resp = await client.put("key", b"val", consistency="one")')
    print('        resp = await client.get("key", consistency="quorum")')
    print()
    print("  Features:")
    print("    - Automatic retry on transient failures (UNAVAILABLE)")
    print("    - Failover to next seed node when retries exhausted")
    print("    - Context manager for clean resource management")
    print("    - Vector clock tracking for read-modify-write patterns")


async def main() -> None:
    """Run all demonstrations."""
    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║     Distributed Key-Value Store — Interactive Demo          ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print()
    print("  This demo showcases the key components of a Dynamo-inspired")
    print("  distributed key-value store: LSM-tree storage, vector clocks,")
    print("  tunable consistency, gossip-based failure detection, and more.")

    # Run each demo section
    await demo_single_node_operations()
    await demo_vector_clocks()
    await demo_quorum_configuration()
    await demo_multi_node_cluster()
    await demo_node_failure_recovery()
    await demo_client_usage()

    print_separator("Summary")
    print("  This distributed key-value store implements:")
    print("    ✓ LSM-tree storage (WAL → MemTable → SSTable)")
    print("    ✓ Consistent hashing for data partitioning")
    print("    ✓ Vector clocks for conflict detection")
    print("    ✓ Tunable consistency (N, W, R quorum)")
    print("    ✓ Gossip protocol for failure detection")
    print("    ✓ Hinted handoff for temporary failures")
    print("    ✓ Merkle trees for anti-entropy repair")
    print("    ✓ gRPC for inter-node communication")
    print()
    print("  For full cluster operation, start multiple KVNode instances")
    print("  and connect via KVClient. See README.md for details.")
    print()


if __name__ == "__main__":
    asyncio.run(main())
