"""Vector clock implementation for tracking causal ordering of events.

A vector clock is a list of (node_id, counter) pairs carried by each data item.
It enables detection of causal relationships and conflicts between versions:
- If one clock dominates another, the versions are causally ordered.
- If neither dominates, the versions are concurrent (conflict).

Covers: FR-5.1, FR-5.2, FR-5.3, FR-5.4, FR-5.5, FR-5.6
"""

from typing import Optional


class VectorClock:
    """Vector clock for tracking causal ordering of events.

    All mutation operations (increment, merge) return new VectorClock instances,
    preserving immutability of the original clock.

    The clock maintains a bounded number of entries (max_entries). When exceeded
    after an increment, the entry with the lowest counter value is pruned.
    """

    def __init__(
        self,
        entries: Optional[dict[str, int]] = None,
        max_entries: int = 10,
    ) -> None:
        """Initialize a vector clock.

        Args:
            entries: Initial clock entries as {node_id: counter}.
                     If None, creates an empty clock.
            max_entries: Maximum number of entries before pruning oldest.
        """
        self._entries: dict[str, int] = dict(entries) if entries else {}
        self._max_entries = max_entries

    def increment(self, node_id: str) -> "VectorClock":
        """Increment the counter for a node, returning a new VectorClock.

        If the node doesn't exist in the clock, it is added with counter=1.
        If max_entries is exceeded after the increment, the entry with the
        lowest counter value is pruned.

        Args:
            node_id: The node performing the write.

        Returns:
            A new VectorClock with the incremented counter.
        """
        new_entries = dict(self._entries)
        new_entries[node_id] = new_entries.get(node_id, 0) + 1

        # Prune if max_entries exceeded
        if len(new_entries) > self._max_entries:
            # Remove the entry with the lowest counter value.
            # If there's a tie, remove any one of them (min by counter).
            min_node = min(new_entries, key=lambda k: new_entries[k])
            del new_entries[min_node]

        return VectorClock(entries=new_entries, max_entries=self._max_entries)

    def merge(self, other: "VectorClock") -> "VectorClock":
        """Merge two vector clocks by taking the max counter for each node.

        Args:
            other: The other vector clock to merge with.

        Returns:
            A new VectorClock representing the merged state.
        """
        merged: dict[str, int] = dict(self._entries)
        for node_id, counter in other._entries.items():
            merged[node_id] = max(merged.get(node_id, 0), counter)
        return VectorClock(entries=merged, max_entries=self._max_entries)

    def dominates(self, other: "VectorClock") -> bool:
        """Check if this clock causally dominates (is strictly newer than) another.

        Clock A dominates Clock B if:
        - For every node in B, A has a counter >= B's counter
        - For at least one node (in A or B), A has a counter strictly > B's counter

        An empty clock does not dominate another empty clock.

        Args:
            other: The other vector clock to compare against.

        Returns:
            True if this clock dominates the other.
        """
        # If both are empty, neither dominates
        if not self._entries and not other._entries:
            return False

        has_strictly_greater = False

        # Check all nodes in other: self must have >= for each
        for node_id, other_counter in other._entries.items():
            self_counter = self._entries.get(node_id, 0)
            if self_counter < other_counter:
                return False
            if self_counter > other_counter:
                has_strictly_greater = True

        # Check nodes in self that are not in other (they contribute > 0 vs 0)
        for node_id in self._entries:
            if node_id not in other._entries:
                has_strictly_greater = True
                break

        return has_strictly_greater

    def conflicts_with(self, other: "VectorClock") -> bool:
        """Check if two clocks are concurrent (neither dominates the other).

        Two equal clocks do not conflict.

        Args:
            other: The other vector clock.

        Returns:
            True if the clocks are concurrent (conflict exists).
        """
        if self == other:
            return False
        return not self.dominates(other) and not other.dominates(self)

    def to_dict(self) -> dict[str, int]:
        """Serialize to a dictionary for storage/transmission.

        Returns:
            A copy of the internal entries dictionary.
        """
        return dict(self._entries)

    @classmethod
    def from_dict(cls, data: dict[str, int], max_entries: int = 10) -> "VectorClock":
        """Deserialize from a dictionary.

        Args:
            data: Dictionary of {node_id: counter} pairs.
            max_entries: Maximum entries before pruning.

        Returns:
            A new VectorClock instance.
        """
        return cls(entries=data, max_entries=max_entries)

    def __eq__(self, other: object) -> bool:
        """Check equality by comparing entries dictionaries."""
        if not isinstance(other, VectorClock):
            return NotImplemented
        return self._entries == other._entries

    def __repr__(self) -> str:
        """Human-readable representation."""
        return f"VectorClock({self._entries})"
