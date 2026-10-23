"""Consistent hashing with immutable read snapshots and serialized writers."""

from __future__ import annotations

import bisect
from dataclasses import dataclass
import threading
from typing import Callable, Optional

from consistent_hashing import hash_functions


@dataclass(frozen=True)
class _RingState:
    """One coherent topology; published states are never modified."""

    entries: tuple[tuple[int, str, int], ...] = ()
    positions: tuple[int, ...] = ()
    nodes: tuple[str, ...] = ()

    @classmethod
    def build(cls, entries, nodes) -> _RingState:
        ordered = tuple(sorted(entries))
        return cls(ordered, tuple(item[0] for item in ordered), tuple(nodes))


def _positive_count(value: int) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("virtual node count must be a positive integer")
    return value


@dataclass(frozen=True)
class RingSnapshot:
    """Stable read view; retain it to route an entire batch against one topology.

    The captured hash callable must remain deterministic and thread safe.
    Holding a snapshot retains its topology memory until the view is released.
    """

    _state: _RingState
    _hash_function: Callable[[str], int]

    def snapshot(self) -> RingSnapshot:
        """Already a stable view; allow statistics to consume either interface."""
        return self

    @property
    def nodes(self) -> list[str]:
        return list(self._state.nodes)

    @property
    def total_virtual_nodes(self) -> int:
        return len(self._state.entries)

    def _hash(self, key: str) -> int:
        value = self._hash_function(key)
        if type(value) is not int:
            raise ValueError("hash function must return an integer")
        return value

    def _start(self, state: _RingState, key: str) -> int:
        if not state.entries:
            raise RuntimeError("Hash ring is empty - no nodes available.")
        return bisect.bisect_right(state.positions, self._hash(key)) % len(
            state.entries
        )

    def get_node(self, key: str) -> str:
        """Return the owner from a single coherent topology snapshot."""
        state = self._state
        return state.entries[self._start(state, key)][1]

    def get_nodes(self, key: str, count: int) -> list[str]:
        """Return up to count distinct clockwise owners; zero returns []."""
        if type(count) is not int or count < 0:
            raise ValueError("count must be a non-negative integer")
        if count == 0:
            return []
        state = self._state
        start = self._start(state, key)
        target = min(count, len(state.nodes))
        result = []
        seen = set()
        for offset in range(len(state.entries)):
            node = state.entries[(start + offset) % len(state.entries)][1]
            if node not in seen:
                seen.add(node)
                result.append(node)
                if len(result) == target:
                    break
        return result


class ConsistentHashRing:
    """Map keys to nodes using stable virtual-node positions.

    Writers build a replacement topology under a writer lock. Readers acquire
    only a short publication lock to capture a snapshot, then search it without
    blocking writers. Custom hash functions must be deterministic and thread
    safe. Equal positions are ordered by node name and virtual-node index;
    removing one colliding node cannot remove another node's entry.

    Lookup retains the original strict-clockwise rule (bisect_right): a key
    exactly at a virtual-node position moves to the next greater position.
    """

    def __init__(
        self,
        nodes: Optional[list[str]] = None,
        num_virtual_nodes: int = 150,
        hash_function: Optional[Callable[[str], int]] = None,
    ) -> None:
        self._hash_function = hash_function or hash_functions.sha1_hash
        self._default_num_virtual_nodes = _positive_count(num_virtual_nodes)
        self._writer_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._state = _RingState()
        for node in nodes or ():
            self.add_node(node)

    def _snapshot(self) -> _RingState:
        with self._state_lock:
            return self._state

    def _publish(self, state: _RingState) -> None:
        with self._state_lock:
            self._state = state

    def add_node(self, node: str, num_virtual_nodes: Optional[int] = None) -> list[int]:
        """Add a node atomically; validation/hash failures leave state intact."""
        if not isinstance(node, str) or not node:
            raise ValueError("node must be a non-empty string")
        count = _positive_count(
            self._default_num_virtual_nodes
            if num_virtual_nodes is None
            else num_virtual_nodes
        )
        with self._writer_lock:
            state = self._snapshot()
            if node in state.nodes:
                raise ValueError(f"Node '{node}' already exists on the ring.")
            added = tuple(
                (self._hash(f"{node}#{index}"), node, index) for index in range(count)
            )
            self._publish(
                _RingState.build(state.entries + added, state.nodes + (node,))
            )
        return [position for position, _, _ in added]

    def remove_node(self, node: str) -> list[int]:
        """Remove the stored entries without recomputing their hashes."""
        with self._writer_lock:
            state = self._snapshot()
            if node not in state.nodes:
                raise KeyError(f"Node '{node}' does not exist on the ring.")
            removed = sorted(
                (item for item in state.entries if item[1] == node),
                key=lambda item: item[2],
            )
            retained = (item for item in state.entries if item[1] != node)
            nodes = (name for name in state.nodes if name != node)
            self._publish(_RingState.build(retained, nodes))
        return [position for position, _, _ in removed]

    def snapshot(self) -> RingSnapshot:
        """Capture one read-only topology for batch routing and migration analysis."""
        return RingSnapshot(self._snapshot(), self._hash_function)

    def get_node(self, key: str) -> str:
        """Return an owner using one captured topology."""
        return self.snapshot().get_node(key)

    def get_nodes(self, key: str, count: int) -> list[str]:
        """Return distinct clockwise owners from one topology."""
        return self.snapshot().get_nodes(key, count)

    @property
    def nodes(self) -> list[str]:
        """Return a copy of the physical node names in insertion order."""
        return list(self._snapshot().nodes)

    @property
    def total_virtual_nodes(self) -> int:
        """Return the number of virtual entries, including collisions."""
        return len(self._snapshot().entries)

    def _hash(self, key: str) -> int:
        value = self._hash_function(key)
        if type(value) is not int:
            raise ValueError("hash function must return an integer")
        return value
