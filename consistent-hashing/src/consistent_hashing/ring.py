"""Consistent hash ring implementation.

Provides the ConsistentHashRing class which distributes keys across
server nodes using virtual nodes and clockwise traversal on a hash ring.
Uses a sorted list of positions with binary search for O(log N) lookups.
"""

from bisect import bisect_right, insort
from typing import Optional, Callable

from consistent_hashing.hash_functions import sha1_hash


class ConsistentHashRing:
    """Consistent hash ring with virtual nodes.

    Uses a sorted list of hash positions and binary search (bisect)
    for O(log N) key lookups. Each physical server is mapped to multiple
    virtual nodes on the ring for balanced distribution.
    """

    def __init__(
        self,
        nodes: Optional[list[str]] = None,
        num_virtual_nodes: int = 150,
        hash_function: Optional[Callable[[str], int]] = None,
    ):
        """Initialize the hash ring.

        Args:
            nodes: Initial list of server node identifiers to add.
            num_virtual_nodes: Default number of virtual nodes per server.
            hash_function: Hash function mapping str -> int. Defaults to SHA-1.
        """
        self._hash_function = hash_function or sha1_hash
        self._default_num_virtual_nodes = num_virtual_nodes

        # Sorted list of hash positions for binary search
        self._sorted_positions: list[int] = []

        # Map from hash position to physical node name
        self._position_to_node: dict[int, str] = {}

        # Map from physical node name to its virtual node count
        self._nodes: dict[str, int] = {}

        # Add initial nodes
        if nodes:
            for node in nodes:
                self.add_node(node)

    def add_node(self, node: str, num_virtual_nodes: Optional[int] = None) -> list[int]:
        """Add a server node to the ring.

        Generates virtual nodes using the format "{node}#{index}" and
        places them on the ring at positions determined by the hash function.

        Args:
            node: Server identifier string.
            num_virtual_nodes: Override virtual node count for this server.
                Uses the ring's default if not specified.

        Returns:
            List of hash positions where virtual nodes were placed.

        Raises:
            ValueError: If node already exists on the ring.
        """
        if node in self._nodes:
            raise ValueError(f"Node '{node}' already exists on the ring.")

        vn_count = (
            num_virtual_nodes
            if num_virtual_nodes is not None
            else self._default_num_virtual_nodes
        )
        self._nodes[node] = vn_count
        positions: list[int] = []

        for i in range(vn_count):
            virtual_key = self._virtual_node_key(node, i)
            position = self._hash(virtual_key)
            insort(self._sorted_positions, position)
            self._position_to_node[position] = node
            positions.append(position)

        return positions

    def remove_node(self, node: str) -> list[int]:
        """Remove a server node and all its virtual nodes from the ring.

        Args:
            node: Server identifier to remove.

        Returns:
            List of hash positions that were removed.

        Raises:
            KeyError: If node does not exist on the ring.
        """
        if node not in self._nodes:
            raise KeyError(f"Node '{node}' does not exist on the ring.")

        vn_count = self._nodes[node]
        positions: list[int] = []

        for i in range(vn_count):
            virtual_key = self._virtual_node_key(node, i)
            position = self._hash(virtual_key)
            self._sorted_positions.remove(position)
            del self._position_to_node[position]
            positions.append(position)

        del self._nodes[node]
        return positions

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
        if not self._sorted_positions:
            raise RuntimeError("Hash ring is empty - no nodes available.")

        hash_value = self._hash(key)

        # Find the first position >= hash_value (clockwise traversal)
        idx = bisect_right(self._sorted_positions, hash_value)

        # Wrap around if past the last position
        if idx == len(self._sorted_positions):
            idx = 0

        position = self._sorted_positions[idx]
        return self._position_to_node[position]

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
        if not self._sorted_positions:
            raise RuntimeError("Hash ring is empty - no nodes available.")

        hash_value = self._hash(key)
        idx = bisect_right(self._sorted_positions, hash_value)

        # Wrap around if past the last position
        if idx == len(self._sorted_positions):
            idx = 0

        result: list[str] = []
        seen: set[str] = set()
        num_positions = len(self._sorted_positions)
        max_servers = min(count, len(self._nodes))

        for i in range(num_positions):
            pos_idx = (idx + i) % num_positions
            position = self._sorted_positions[pos_idx]
            node = self._position_to_node[position]

            if node not in seen:
                result.append(node)
                seen.add(node)
                if len(result) == max_servers:
                    break

        return result

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
