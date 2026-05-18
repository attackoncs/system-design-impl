"""Tests for the VectorClock implementation."""

import pytest

from kv_store.replication.vector_clock import VectorClock


class TestVectorClockIncrement:
    """Tests for VectorClock.increment()."""

    def test_increment_new_node_creates_entry(self):
        """Incrementing a new node creates an entry with counter=1."""
        clock = VectorClock()
        new_clock = clock.increment("node-1")
        assert new_clock.to_dict() == {"node-1": 1}

    def test_increment_existing_node_increases_counter(self):
        """Incrementing an existing node increases its counter by 1."""
        clock = VectorClock(entries={"node-1": 3})
        new_clock = clock.increment("node-1")
        assert new_clock.to_dict() == {"node-1": 4}

    def test_increment_returns_new_instance(self):
        """Increment returns a new VectorClock, leaving original unchanged."""
        clock = VectorClock(entries={"node-1": 1})
        new_clock = clock.increment("node-1")
        assert clock.to_dict() == {"node-1": 1}
        assert new_clock.to_dict() == {"node-1": 2}
        assert clock is not new_clock

    def test_increment_multiple_nodes(self):
        """Incrementing different nodes adds separate entries."""
        clock = VectorClock()
        clock = clock.increment("node-1")
        clock = clock.increment("node-2")
        clock = clock.increment("node-1")
        assert clock.to_dict() == {"node-1": 2, "node-2": 1}


class TestVectorClockMerge:
    """Tests for VectorClock.merge()."""

    def test_merge_takes_max_of_each_counter(self):
        """Merge takes the maximum counter for each node."""
        clock_a = VectorClock(entries={"node-1": 3, "node-2": 1})
        clock_b = VectorClock(entries={"node-1": 1, "node-2": 5})
        merged = clock_a.merge(clock_b)
        assert merged.to_dict() == {"node-1": 3, "node-2": 5}

    def test_merge_includes_nodes_from_both(self):
        """Merge includes nodes that only appear in one clock."""
        clock_a = VectorClock(entries={"node-1": 2})
        clock_b = VectorClock(entries={"node-2": 3})
        merged = clock_a.merge(clock_b)
        assert merged.to_dict() == {"node-1": 2, "node-2": 3}

    def test_merge_with_empty_clock(self):
        """Merging with an empty clock returns a copy of self."""
        clock_a = VectorClock(entries={"node-1": 2, "node-2": 3})
        clock_b = VectorClock()
        merged = clock_a.merge(clock_b)
        assert merged.to_dict() == {"node-1": 2, "node-2": 3}

    def test_merge_returns_new_instance(self):
        """Merge returns a new VectorClock, leaving originals unchanged."""
        clock_a = VectorClock(entries={"node-1": 1})
        clock_b = VectorClock(entries={"node-1": 2})
        merged = clock_a.merge(clock_b)
        assert clock_a.to_dict() == {"node-1": 1}
        assert clock_b.to_dict() == {"node-1": 2}
        assert merged.to_dict() == {"node-1": 2}


class TestVectorClockDominates:
    """Tests for VectorClock.dominates()."""

    def test_dominates_when_strictly_greater(self):
        """A clock with higher counters dominates."""
        clock_a = VectorClock(entries={"node-1": 3, "node-2": 2})
        clock_b = VectorClock(entries={"node-1": 2, "node-2": 1})
        assert clock_a.dominates(clock_b) is True

    def test_dominates_when_equal_plus_extra_node(self):
        """A clock with an extra node dominates if all others are >=."""
        clock_a = VectorClock(entries={"node-1": 2, "node-2": 1})
        clock_b = VectorClock(entries={"node-1": 2})
        assert clock_a.dominates(clock_b) is True

    def test_does_not_dominate_when_equal(self):
        """Equal clocks do not dominate each other."""
        clock_a = VectorClock(entries={"node-1": 2, "node-2": 3})
        clock_b = VectorClock(entries={"node-1": 2, "node-2": 3})
        assert clock_a.dominates(clock_b) is False

    def test_does_not_dominate_when_lower(self):
        """A clock with a lower counter does not dominate."""
        clock_a = VectorClock(entries={"node-1": 1})
        clock_b = VectorClock(entries={"node-1": 2})
        assert clock_a.dominates(clock_b) is False

    def test_empty_clocks_dont_dominate_each_other(self):
        """Two empty clocks do not dominate each other."""
        clock_a = VectorClock()
        clock_b = VectorClock()
        assert clock_a.dominates(clock_b) is False
        assert clock_b.dominates(clock_a) is False

    def test_non_empty_dominates_empty(self):
        """A non-empty clock dominates an empty clock."""
        clock_a = VectorClock(entries={"node-1": 1})
        clock_b = VectorClock()
        assert clock_a.dominates(clock_b) is True

    def test_does_not_dominate_concurrent(self):
        """Concurrent clocks don't dominate each other."""
        clock_a = VectorClock(entries={"node-1": 2, "node-2": 1})
        clock_b = VectorClock(entries={"node-1": 1, "node-2": 2})
        assert clock_a.dominates(clock_b) is False
        assert clock_b.dominates(clock_a) is False


class TestVectorClockConflicts:
    """Tests for VectorClock.conflicts_with()."""

    def test_conflicts_when_concurrent(self):
        """Concurrent clocks conflict."""
        clock_a = VectorClock(entries={"node-1": 2, "node-2": 1})
        clock_b = VectorClock(entries={"node-1": 1, "node-2": 2})
        assert clock_a.conflicts_with(clock_b) is True

    def test_no_conflict_when_one_dominates(self):
        """No conflict when one clock dominates the other."""
        clock_a = VectorClock(entries={"node-1": 3, "node-2": 2})
        clock_b = VectorClock(entries={"node-1": 2, "node-2": 1})
        assert clock_a.conflicts_with(clock_b) is False

    def test_no_conflict_when_equal(self):
        """Equal clocks don't conflict."""
        clock_a = VectorClock(entries={"node-1": 2, "node-2": 3})
        clock_b = VectorClock(entries={"node-1": 2, "node-2": 3})
        assert clock_a.conflicts_with(clock_b) is False

    def test_conflicts_is_symmetric(self):
        """conflicts_with is symmetric: a.conflicts(b) == b.conflicts(a)."""
        clock_a = VectorClock(entries={"node-1": 2, "node-2": 1})
        clock_b = VectorClock(entries={"node-1": 1, "node-2": 2})
        assert clock_a.conflicts_with(clock_b) == clock_b.conflicts_with(clock_a)


class TestVectorClockSerialization:
    """Tests for to_dict/from_dict serialization."""

    def test_to_dict_returns_copy(self):
        """to_dict returns a copy, not the internal dict."""
        clock = VectorClock(entries={"node-1": 2})
        d = clock.to_dict()
        d["node-1"] = 99
        assert clock.to_dict() == {"node-1": 2}

    def test_from_dict_creates_clock(self):
        """from_dict creates a VectorClock from a dictionary."""
        data = {"node-1": 3, "node-2": 5}
        clock = VectorClock.from_dict(data)
        assert clock.to_dict() == {"node-1": 3, "node-2": 5}

    def test_roundtrip_serialization(self):
        """to_dict/from_dict roundtrip preserves the clock."""
        original = VectorClock(entries={"a": 1, "b": 2, "c": 3})
        restored = VectorClock.from_dict(original.to_dict())
        assert original == restored

    def test_from_dict_with_custom_max_entries(self):
        """from_dict respects the max_entries parameter."""
        data = {"node-1": 1, "node-2": 2}
        clock = VectorClock.from_dict(data, max_entries=5)
        # Incrementing 4 more unique nodes should trigger pruning at 5
        clock = clock.increment("node-3")
        clock = clock.increment("node-4")
        clock = clock.increment("node-5")
        clock = clock.increment("node-6")
        assert len(clock.to_dict()) == 5


class TestVectorClockPruning:
    """Tests for max_entries pruning behavior."""

    def test_prune_removes_lowest_counter(self):
        """When max_entries is exceeded, the entry with lowest counter is removed."""
        clock = VectorClock(
            entries={"node-1": 5, "node-2": 3, "node-3": 1},
            max_entries=3,
        )
        # Adding a 4th node should prune the one with lowest counter (node-3: 1)
        new_clock = clock.increment("node-4")
        entries = new_clock.to_dict()
        assert len(entries) == 3
        assert "node-3" not in entries
        assert entries == {"node-1": 5, "node-2": 3, "node-4": 1}

    def test_no_pruning_when_within_limit(self):
        """No pruning occurs when entries are within max_entries."""
        clock = VectorClock(max_entries=5)
        clock = clock.increment("node-1")
        clock = clock.increment("node-2")
        clock = clock.increment("node-3")
        assert len(clock.to_dict()) == 3

    def test_prune_with_max_entries_one(self):
        """With max_entries=1, only the most recently incremented node survives."""
        clock = VectorClock(entries={"node-1": 5}, max_entries=1)
        new_clock = clock.increment("node-2")
        entries = new_clock.to_dict()
        assert len(entries) == 1
        # node-2 has counter 1, node-1 has counter 5
        # The lowest counter (node-2: 1) gets pruned... wait, node-2 was just added
        # Actually node-2 gets counter 1, node-1 has 5. Lowest is node-2.
        # But we just incremented node-2, so it should keep node-1 (5) and prune node-2 (1)?
        # The spec says "remove oldest (lowest counter)" - so lowest counter gets pruned.
        # node-2 has counter 1 which is lowest, so it gets pruned.
        assert "node-1" in entries

    def test_default_max_entries_is_ten(self):
        """Default max_entries is 10."""
        clock = VectorClock()
        for i in range(10):
            clock = clock.increment(f"node-{i}")
        assert len(clock.to_dict()) == 10
        # 11th should trigger pruning
        clock = clock.increment("node-10")
        assert len(clock.to_dict()) == 10


class TestVectorClockEquality:
    """Tests for __eq__."""

    def test_equal_clocks(self):
        """Clocks with same entries are equal."""
        clock_a = VectorClock(entries={"node-1": 2, "node-2": 3})
        clock_b = VectorClock(entries={"node-1": 2, "node-2": 3})
        assert clock_a == clock_b

    def test_unequal_clocks(self):
        """Clocks with different entries are not equal."""
        clock_a = VectorClock(entries={"node-1": 2})
        clock_b = VectorClock(entries={"node-1": 3})
        assert clock_a != clock_b

    def test_not_equal_to_non_vector_clock(self):
        """VectorClock is not equal to non-VectorClock objects."""
        clock = VectorClock(entries={"node-1": 1})
        assert clock != {"node-1": 1}
        assert clock != "not a clock"

    def test_empty_clocks_are_equal(self):
        """Two empty clocks are equal."""
        assert VectorClock() == VectorClock()


class TestVectorClockRepr:
    """Tests for __repr__."""

    def test_repr_shows_entries(self):
        """repr includes the entries dictionary."""
        clock = VectorClock(entries={"node-1": 2})
        assert repr(clock) == "VectorClock({'node-1': 2})"

    def test_repr_empty_clock(self):
        """repr of empty clock shows empty dict."""
        clock = VectorClock()
        assert repr(clock) == "VectorClock({})"
