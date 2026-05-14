# Design: Consistent Hashing

## Architecture Overview

The consistent hashing module follows a simple layered design with the hash ring as the core data structure, supporting configurable hash functions and virtual nodes for balanced distribution.

```
┌─────────────────────────────────────────────────┐
│           Public API                             │
│   (ConsistentHashRing class)                    │
├─────────────────────────────────────────────────┤
│           Hash Ring Data Structure               │
│   (Sorted virtual node positions + bisect)      │
├─────────────────────────────────────────────────┤
│           Hash Function Layer                    │
│   (SHA-1, MD5, SHA-256, custom)                 │
├─────────────────────────────────────────────────┤
│           Statistics & Utilities                  │
│   (Distribution analysis, visualization)        │
└─────────────────────────────────────────────────┘
```

## Project Structure

```
consistent-hashing/
├── pyproject.toml
├── README.md
├── docs/
│   ├── requirements.md
│   ├── design.md
│   └── tasks.md
├── src/
│   └── consistent_hashing/
│       ├── __init__.py          # Public API exports
│       ├── ring.py              # ConsistentHashRing class
│       ├── hash_functions.py    # Hash function implementations
│       └── stats.py             # Distribution statistics utilities
├── tests/
│   ├── __init__.py
│   ├── test_ring.py             # Unit tests for hash ring
│   ├── test_hash_functions.py   # Unit tests for hash functions
│   ├── test_stats.py            # Unit tests for statistics
│   └── test_properties.py      # Property-based tests (Hypothesis)
└── examples/
    └── demo.py                  # Demonstration script
```

## Component Design

### 1. Hash Function Interface

```python
from typing import Protocol, Callable
import hashlib


class HashFunction(Protocol):
    """Protocol for hash functions used by the ring."""

    def __call__(self, key: str) -> int:
        """Hash a string key to an integer position on the ring."""
        ...


def sha1_hash(key: str) -> int:
    """Default hash function using SHA-1, returning full 160-bit integer."""
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return int(digest, 16)


def md5_hash(key: str) -> int:
    """MD5-based hash function, returning 128-bit integer."""
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return int(digest, 16)


def sha256_hash(key: str) -> int:
    """SHA-256-based hash function, returning 256-bit integer."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return int(digest, 16)
```

### 2. ConsistentHashRing Class

```python
from bisect import bisect_right, insort
from dataclasses import dataclass, field
from typing import Optional, Callable


@dataclass
class NodeInfo:
    """Metadata for a physical server node."""
    name: str
    num_virtual_nodes: int
    weight: float = 1.0


class ConsistentHashRing:
    """Consistent hash ring with virtual nodes.

    Uses a sorted list of (position, node_name) tuples and binary search
    for O(log N) key lookups.
    """

    def __init__(
        self,
        nodes: Optional[list[str]] = None,
        num_virtual_nodes: int = 150,
        hash_function: Optional[Callable[[str], int]] = None,
    ):
        """Initialize the hash ring.

        Args:
            nodes: Initial list of server node identifiers.
            num_virtual_nodes: Default number of virtual nodes per server.
            hash_function: Hash function mapping str -> int. Defaults to SHA-1.
        """
        self._hash_function = hash_function or sha1_hash
        self._default_num_virtual_nodes = num_virtual_nodes

        # Sorted list of hash positions for binary search
        self._sorted_positions: list[int] = []

        # Map from hash position to physical node name
        self._position_to_node: dict[int, str] = {}

        # Map from physical node name to its metadata
        self._nodes: dict[str, NodeInfo] = {}

        # Add initial nodes
        if nodes:
            for node in nodes:
                self.add_node(node)

    def add_node(self, node: str, num_virtual_nodes: Optional[int] = None) -> list[int]:
        """Add a server node to the ring.

        Args:
            node: Server identifier string.
            num_virtual_nodes: Override virtual node count for this server.

        Returns:
            List of hash positions where virtual nodes were placed.

        Raises:
            ValueError: If node already exists on the ring.
        """
        ...

    def remove_node(self, node: str) -> list[int]:
        """Remove a server node and all its virtual nodes from the ring.

        Args:
            node: Server identifier to remove.

        Returns:
            List of hash positions that were removed.

        Raises:
            KeyError: If node does not exist on the ring.
        """
        ...

    def get_node(self, key: str) -> str:
        """Get the server responsible for a given key.

        Hashes the key and traverses clockwise to find the first server.

        Args:
            key: The key to look up.

        Returns:
            The server node identifier responsible for this key.

        Raises:
            RuntimeError: If the ring is empty (no nodes).
        """
        ...

    def get_nodes(self, key: str, count: int) -> list[str]:
        """Get multiple distinct servers for a key (for replication).

        Traverses clockwise from the key's position, collecting distinct
        physical servers (skipping virtual nodes of already-collected servers).

        Args:
            key: The key to look up.
            count: Number of distinct servers to return.

        Returns:
            List of distinct server identifiers (up to count or total servers).

        Raises:
            RuntimeError: If the ring is empty.
        """
        ...

    def get_redistribution_on_add(self, node: str, num_virtual_nodes: Optional[int] = None) -> dict[str, list[str]]:
        """Preview which keys would move if a node were added.

        Does NOT modify the ring. Returns a mapping of
        {source_server: [keys_that_would_move_away]}.

        Note: This requires tracking keys externally; this method works
        with a provided key set.
        """
        ...

    def get_redistribution_on_remove(self, node: str) -> dict[str, list[str]]:
        """Preview which keys would move if a node were removed.

        Does NOT modify the ring.
        """
        ...

    @property
    def nodes(self) -> list[str]:
        """List of all physical node names on the ring."""
        return list(self._nodes.keys())

    @property
    def total_virtual_nodes(self) -> int:
        """Total number of virtual nodes on the ring."""
        return len(self._sorted_positions)

    def _hash(self, key: str) -> int:
        """Hash a key to a ring position."""
        return self._hash_function(key)

    def _virtual_node_key(self, node: str, index: int) -> str:
        """Generate the string to hash for a virtual node.

        Format: "{node}#{index}"
        """
        return f"{node}#{index}"
```

### 3. Key Lookup Algorithm (Binary Search)

```python
def get_node(self, key: str) -> str:
    """O(log N) lookup using bisect."""
    if not self._sorted_positions:
        raise RuntimeError("Hash ring is empty — no nodes available.")

    hash_value = self._hash(key)

    # Find the first position >= hash_value (clockwise traversal)
    idx = bisect_right(self._sorted_positions, hash_value)

    # Wrap around if past the last position
    if idx == len(self._sorted_positions):
        idx = 0

    position = self._sorted_positions[idx]
    return self._position_to_node[position]
```

### 4. Statistics Module

```python
from dataclasses import dataclass


@dataclass
class DistributionStats:
    """Statistics about key distribution across servers."""
    total_keys: int
    num_servers: int
    keys_per_server: dict[str, int]
    mean: float
    std_dev: float
    min_keys: int
    max_keys: int
    balance_ratio: float  # std_dev / mean (lower is better)


def compute_distribution(ring: "ConsistentHashRing", keys: list[str]) -> DistributionStats:
    """Compute distribution statistics for a set of keys on the ring.

    Args:
        ring: The consistent hash ring.
        keys: List of keys to analyze.

    Returns:
        DistributionStats with per-server counts and aggregate metrics.
    """
    ...


def compute_redistribution(
    keys: list[str],
    assignment_before: dict[str, str],
    assignment_after: dict[str, str],
) -> dict[str, dict[str, list[str]]]:
    """Compute which keys moved between two assignments.

    Args:
        keys: All keys.
        assignment_before: {key: server} mapping before change.
        assignment_after: {key: server} mapping after change.

    Returns:
        {"moved": {key: {"from": old_server, "to": new_server}}, "total_moved": int}
    """
    ...
```

### 5. Server Add/Remove with Minimal Redistribution

**Adding a node:**
1. Generate K virtual node keys: `"{node}#{i}"` for i in range(K)
2. Hash each to get positions
3. Insert positions into sorted list (using `insort`)
4. Map each position to the physical node
5. Only keys between the new position and the previous position (counter-clockwise) are affected

**Removing a node:**
1. Look up all positions for the node
2. Remove each position from sorted list and position-to-node map
3. Keys that were assigned to the removed node now fall through to the next clockwise node

### 6. Virtual Node Naming Convention

Virtual nodes are generated using the format `"{node}#{index}"`:
- Server "web-1" with 3 virtual nodes → hash("web-1#0"), hash("web-1#1"), hash("web-1#2")
- This ensures deterministic, reproducible placement regardless of insertion order

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Data structure | Sorted list + bisect | O(log N) lookup, simple implementation, Python stdlib |
| Hash function | SHA-1 default | 160-bit space provides excellent distribution; configurable for flexibility |
| Virtual node format | `"{node}#{index}"` | Simple, deterministic, avoids collisions between nodes |
| Default virtual nodes | 150 per server | Good balance between distribution uniformity and memory usage |
| Lookup direction | Clockwise (ascending) | Standard convention from the original paper |
| Wrap-around | Modular (first node after end) | Ensures all keys map to a server regardless of position |
| Per-server virtual nodes | Supported via weight | Allows heterogeneous server capacities |
| Statistics | Separate module | Keeps core ring logic clean; stats are optional |
| Property-based testing | Hypothesis library | Verifies invariants across random inputs (determinism, minimal redistribution) |

## Error Handling

- **Empty ring**: `get_node()` and `get_nodes()` raise `RuntimeError` with descriptive message.
- **Duplicate node**: `add_node()` raises `ValueError` if node already exists.
- **Missing node**: `remove_node()` raises `KeyError` if node doesn't exist.
- **Invalid count**: `get_nodes(key, count)` returns up to `min(count, total_physical_nodes)` servers without error.
- **Hash collisions**: If two virtual nodes hash to the same position, the later one overwrites (extremely unlikely with SHA-1's 160-bit space, but handled gracefully).
