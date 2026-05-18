# Requirements: Distributed Key-Value Store

## Overview

A distributed key-value store implemented in Python, based on the design principles from "System Design Interview" Chapter 7. The system distributes data across multiple nodes using consistent hashing, provides tunable consistency via quorum consensus, handles failures through gossip protocol and hinted handoff, and uses an LSM-tree-based storage engine (Commit Log → MemTable → SSTable) for persistence. Nodes communicate via gRPC and the system reuses the existing `consistent-hashing` library from this repository.

## Functional Requirements

### FR-1: Client API

- **FR-1.1**: The system shall provide a `put(key, value)` operation to insert or update a key-value pair.
- **FR-1.2**: The system shall provide a `get(key)` operation to retrieve the value(s) associated with a key.
- **FR-1.3**: The system shall provide a `delete(key)` operation using tombstone markers.
- **FR-1.4**: Keys shall be strings (max 256 bytes); values shall be opaque byte sequences (max 10 KB).
- **FR-1.5**: The client API shall return metadata including vector clock version and conflict status.

### FR-2: Data Partitioning (Consistent Hashing)

- **FR-2.1**: The system shall use the existing `consistent-hashing` library (`ConsistentHashRing`) for data partitioning.
- **FR-2.2**: Each key shall be mapped to a primary node via clockwise traversal on the hash ring.
- **FR-2.3**: The system shall support adding and removing nodes with automatic data redistribution.
- **FR-2.4**: Virtual nodes shall be used for balanced distribution (configurable count per physical node).

### FR-3: Data Replication

- **FR-3.1**: Each key-value pair shall be replicated to N nodes (configurable, default N=3).
- **FR-3.2**: Replicas shall be chosen as the next N distinct physical nodes clockwise on the hash ring (via `get_nodes()`).
- **FR-3.3**: Replication shall be asynchronous — the coordinator returns after W acknowledgments.
- **FR-3.4**: Replicas should be placed in distinct data centers when configured for multi-datacenter mode.

### FR-4: Tunable Consistency (Quorum Consensus)

- **FR-4.1**: The system shall support configurable N (replica count), W (write quorum), and R (read quorum) parameters.
- **FR-4.2**: A write operation shall be considered successful after receiving W acknowledgments from replicas.
- **FR-4.3**: A read operation shall query R replicas and return the most recent value based on vector clock comparison.
- **FR-4.4**: When W + R > N, strong consistency shall be guaranteed.
- **FR-4.5**: Default configuration shall be N=3, W=2, R=2 (strong consistency).
- **FR-4.6**: The system shall support eventual consistency mode (W=1, R=1) for low-latency use cases.

### FR-5: Vector Clock & Conflict Resolution

- **FR-5.1**: Each data item shall carry a vector clock as `[(server_id, version_counter), ...]`.
- **FR-5.2**: On write, the coordinator shall increment its own entry in the vector clock.
- **FR-5.3**: The system shall detect conflicts by comparing vector clocks — if neither dominates, a conflict exists.
- **FR-5.4**: When no conflict exists (one version dominates), the system shall automatically resolve by keeping the newer version.
- **FR-5.5**: When a conflict exists (sibling versions), the system shall return all conflicting versions to the client for resolution.
- **FR-5.6**: Vector clock length shall be bounded (configurable max entries, default 10); oldest entries are pruned when exceeded.

### FR-6: Gossip Protocol (Failure Detection)

- **FR-6.1**: Each node shall maintain a membership list with member IDs and heartbeat counters.
- **FR-6.2**: Each node shall periodically increment its own heartbeat counter.
- **FR-6.3**: Each node shall periodically send its membership list to a random subset of peers.
- **FR-6.4**: A node shall be marked as suspected/down if its heartbeat counter has not increased within a configurable timeout (default 5 seconds).
- **FR-6.5**: Node failure detection shall propagate through the gossip protocol to all nodes.
- **FR-6.6**: The gossip interval shall be configurable (default 1 second).

### FR-7: Sloppy Quorum & Hinted Handoff

- **FR-7.1**: When a target replica node is unavailable, the system shall route the request to the next healthy node on the ring (sloppy quorum).
- **FR-7.2**: The substitute node shall store the data with a hint indicating the intended recipient.
- **FR-7.3**: When the originally intended node recovers, hinted data shall be pushed back to it.
- **FR-7.4**: After successful handoff, the hint data shall be deleted from the substitute node.
- **FR-7.5**: Hinted handoff shall be attempted periodically (configurable interval, default 10 seconds).

### FR-8: Anti-Entropy (Merkle Tree)

- **FR-8.1**: Each node shall maintain a Merkle tree over its key space for inconsistency detection.
- **FR-8.2**: The key space shall be divided into configurable buckets (default 1024 buckets).
- **FR-8.3**: Nodes shall periodically compare Merkle tree roots with replicas to detect inconsistencies.
- **FR-8.4**: When inconsistencies are detected, only the differing buckets shall be synchronized.
- **FR-8.5**: The anti-entropy sync interval shall be configurable (default 60 seconds).

### FR-9: Storage Engine (LSM-Tree)

- **FR-9.1**: Write operations shall first be persisted to a commit log (WAL) for durability.
- **FR-9.2**: Data shall then be written to an in-memory MemTable (sorted by key).
- **FR-9.3**: When the MemTable reaches a configurable size threshold (default 4 MB), it shall be flushed to disk as an immutable SSTable.
- **FR-9.4**: SSTables shall store sorted key-value pairs with an index for efficient lookups.
- **FR-9.5**: The system shall support SSTable compaction to merge multiple SSTables and remove deleted entries.

### FR-10: Read Path

- **FR-10.1**: Read operations shall first check the MemTable.
- **FR-10.2**: If not found in MemTable, the system shall consult a Bloom filter to determine which SSTables may contain the key.
- **FR-10.3**: The system shall search candidate SSTables from newest to oldest.
- **FR-10.4**: Bloom filters shall have a configurable false positive rate (default 1%).

### FR-11: Node Communication (gRPC)

- **FR-11.1**: Nodes shall communicate via gRPC using Protocol Buffers for serialization.
- **FR-11.2**: The gRPC service shall support: Put, Get, Delete, Replicate, GossipExchange, HintedHandoff, MerkleTreeSync operations.
- **FR-11.3**: Each node shall run as an independent process with its own gRPC server.
- **FR-11.4**: The coordinator node shall forward requests to replica nodes via gRPC client calls.

### FR-12: Decentralized Architecture

- **FR-12.1**: The system shall have no single point of failure — every node has the same set of responsibilities.
- **FR-12.2**: Any node can serve as coordinator for any client request.
- **FR-12.3**: Nodes shall discover each other through seed nodes and gossip protocol.
- **FR-12.4**: The system shall support dynamic cluster membership (nodes joining and leaving).

## Non-Functional Requirements

### NFR-1: Performance

- **NFR-1.1**: In-memory reads (MemTable hit) shall complete in under 1ms.
- **NFR-1.2**: Disk reads (SSTable with Bloom filter) shall complete in under 10ms for single-node operations.
- **NFR-1.3**: Write operations shall complete in under 5ms for local persistence (commit log + MemTable).

### NFR-2: Durability

- **NFR-2.1**: All writes shall be persisted to the commit log before acknowledgment.
- **NFR-2.2**: Data shall survive node restarts by replaying the commit log and loading SSTables.
- **NFR-2.3**: The commit log shall be truncated after successful MemTable flush to SSTable.

### NFR-3: Scalability

- **NFR-3.1**: The system shall support horizontal scaling by adding nodes without downtime.
- **NFR-3.2**: Data redistribution on node addition/removal shall be proportional to 1/N of total data.

### NFR-4: Fault Tolerance

- **NFR-4.1**: The system shall remain available for reads and writes when up to N-W (or N-R) nodes are down.
- **NFR-4.2**: Temporary failures shall be handled transparently via sloppy quorum.
- **NFR-4.3**: Permanent failures shall be detected and repaired via anti-entropy protocol.

### NFR-5: Project Structure

- **NFR-5.1**: The project shall use standard Python packaging (pyproject.toml with hatchling).
- **NFR-5.2**: The project shall include type hints throughout.
- **NFR-5.3**: The project shall include unit tests and property-based tests using pytest + hypothesis.
- **NFR-5.4**: The project shall provide usage examples and a demo script.
- **NFR-5.5**: Python >= 3.9 shall be required.
