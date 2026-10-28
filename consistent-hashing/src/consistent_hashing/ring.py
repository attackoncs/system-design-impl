"""Consistent hashing with immutable read snapshots and serialized writers."""

from __future__ import annotations

import bisect
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
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
        return cls.from_ordered(sorted(entries), nodes)

    @classmethod
    def from_ordered(cls, entries, nodes) -> _RingState:
        """Build from internal sorted streams without sorting retained entries."""
        ordered = tuple(entries)
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
        return hash_functions.hash_position(self._hash_function, key)

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

    def iter_assignments(self, keys: Iterable[str]) -> Iterator[tuple[str, str]]:
        """Stream caller-supplied keys using this view, without retaining keys."""
        if not self._state.entries:
            raise RuntimeError("Hash ring is empty - no nodes available.")
        return ((key, self.get_node(key)) for key in keys)

    def get_keys(self, node: str, keys: Iterable[str]) -> list[str]:
        """Filter supplied keys; the ring does not store a registry of data keys."""
        if node not in self._state.nodes:
            raise KeyError(f"Node '{node}' does not exist on the ring.")
        return [key for key, owner in self.iter_assignments(keys) if owner == node]

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
    Hash callbacks must not mutate this ring or wait for topology writers;
    same-thread writer reentry raises instead of deadlocking.

    Lookup retains the original strict-clockwise rule (bisect_right): a key
    exactly at a virtual-node position moves to the next greater position.
    """

    def __init__(
        self,
        nodes: Optional[Sequence[str]] = None,
        num_virtual_nodes: int = 150,
        hash_function: Optional[Callable[[str], int]] = None,
        *,
        max_virtual_nodes: int = 1_000_000,
    ) -> None:
        self._hash_function = (
            hash_functions.sha1_hash if hash_function is None else hash_function
        )
        if not callable(self._hash_function):
            raise ValueError("hash_function must be callable")
        self._default_num_virtual_nodes = _positive_count(num_virtual_nodes)
        self._max_virtual_nodes = _positive_count(max_virtual_nodes)
        self._ensure_capacity(self._default_num_virtual_nodes)
        self._writer_lock = threading.Lock()
        self._writer_context = threading.local()
        self._state_lock = threading.Lock()
        self._state = _RingState()
        if nodes is not None and (
            isinstance(nodes, (str, bytes)) or not isinstance(nodes, Sequence)
        ):
            raise ValueError("nodes must be a sequence of node names")
        names = tuple(nodes) if nodes is not None else ()
        self._ensure_capacity(len(names) * self._default_num_virtual_nodes)
        for node in names:
            self._validate_node(node)
        if len(set(names)) != len(names):
            raise ValueError("duplicate node names")
        entries = (
            entry
            for node in names
            for entry in self._entries(node, 0, self._default_num_virtual_nodes)
        )
        with self._writing():
            self._state = _RingState.build(entries, names)

    @staticmethod
    def _validate_node(node: str) -> None:
        if not isinstance(node, str) or not node:
            raise ValueError("node must be a non-empty string")

    def _ensure_capacity(self, count: int) -> None:
        if count > self._max_virtual_nodes:
            raise ValueError("maximum virtual node capacity exceeded")

    def _entries(
        self, node: str, start: int, end: int
    ) -> tuple[tuple[int, str, int], ...]:
        return tuple(
            (self._hash(f"{node}#{index}"), node, index) for index in range(start, end)
        )

    @contextmanager
    def _writing(self) -> Iterator[None]:
        """Reject same-thread writer reentry instead of deadlocking a callback."""
        if getattr(self._writer_context, "active", False):
            raise RuntimeError("hash callbacks must not reenter topology writers")
        with self._writer_lock:
            self._writer_context.active = True
            try:
                yield
            finally:
                self._writer_context.active = False

    def _snapshot(self) -> _RingState:
        with self._state_lock:
            return self._state

    def _publish(self, state: _RingState) -> None:
        with self._state_lock:
            self._state = state

    def add_node(self, node: str, num_virtual_nodes: Optional[int] = None) -> list[int]:
        """Add a node atomically; validation/hash failures leave state intact."""
        self._validate_node(node)
        count = _positive_count(
            self._default_num_virtual_nodes
            if num_virtual_nodes is None
            else num_virtual_nodes
        )
        with self._writing():
            state = self._snapshot()
            if node in state.nodes:
                raise ValueError(f"Node '{node}' already exists on the ring.")
            self._ensure_capacity(len(state.entries) + count)
            added = self._entries(node, 0, count)
            self._publish(
                _RingState.from_ordered(
                    sorted(state.entries + tuple(sorted(added))), state.nodes + (node,)
                )
            )
        return [position for position, _, _ in added]

    def remove_node(self, node: str) -> list[int]:
        """Remove the stored entries without recomputing their hashes."""
        self._validate_node(node)
        with self._writing():
            state = self._snapshot()
            if node not in state.nodes:
                raise KeyError(f"Node '{node}' does not exist on the ring.")
            removed = sorted(
                (item for item in state.entries if item[1] == node),
                key=lambda item: item[2],
            )
            retained = (item for item in state.entries if item[1] != node)
            nodes = (name for name in state.nodes if name != node)
            self._publish(_RingState.from_ordered(retained, nodes))
        return [position for position, _, _ in removed]

    def set_virtual_nodes(self, node: str, count: int) -> None:
        """Change a weight in one publication, preserving retained positions.

        Increasing only appends indices; decreasing only removes highest indices.
        Readers see the complete old or new topology, never a remove/add gap.
        Hash callbacks must not call topology writers on this ring.
        """
        self._validate_node(node)
        count = _positive_count(count)
        with self._writing():
            state = self._snapshot()
            if node not in state.nodes:
                raise KeyError(f"Node '{node}' does not exist on the ring.")
            old_count = sum(entry[1] == node for entry in state.entries)
            self._ensure_capacity(len(state.entries) - old_count + count)
            if old_count == count:
                return
            if count > old_count:
                added = self._entries(node, old_count, count)
                entries = sorted(state.entries + tuple(sorted(added)))
            else:
                entries = (
                    entry
                    for entry in state.entries
                    if entry[1] != node or entry[2] < count
                )
            self._publish(_RingState.from_ordered(entries, state.nodes))

    def snapshot(self) -> RingSnapshot:
        """Capture one read-only topology for batch routing and migration analysis."""
        return RingSnapshot(self._snapshot(), self._hash_function)

    def get_node(self, key: str) -> str:
        """Return an owner using one captured topology."""
        return self.snapshot().get_node(key)

    def get_nodes(self, key: str, count: int) -> list[str]:
        """Return distinct clockwise owners from one topology."""
        return self.snapshot().get_nodes(key, count)

    def iter_assignments(self, keys: Iterable[str]) -> Iterator[tuple[str, str]]:
        """Capture topology now, then stream one coherent batch when consumed."""
        return self.snapshot().iter_assignments(keys)

    def get_keys(self, node: str, keys: Iterable[str]) -> list[str]:
        """Return the supplied keys belonging to node on one topology snapshot."""
        return self.snapshot().get_keys(node, keys)

    @property
    def nodes(self) -> list[str]:
        """Return a copy of the physical node names in insertion order."""
        return list(self._snapshot().nodes)

    @property
    def total_virtual_nodes(self) -> int:
        """Return the number of virtual entries, including collisions."""
        return len(self._snapshot().entries)

    def _hash(self, key: str) -> int:
        return hash_functions.hash_position(self._hash_function, key)
