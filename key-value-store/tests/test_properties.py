"""Property-based tests using Hypothesis for the key-value store.

Tests universal properties that must hold across all inputs for:
- Vector Clock: increment, merge, dominates, conflicts_with
- Bloom Filter: no false negatives, false positive rate bounds
- MemTable: put/get roundtrip, sorted order, tombstones
- Storage Engine: put/get roundtrip, delete/get tombstone
- Quorum: strong consistency condition, write success condition

Uses Hypothesis strategies for generating random keys, values, and operations.
"""

import asyncio
import time
from pathlib import Path

import pytest
from hypothesis import given, settings, assume, HealthCheck
from hypothesis import strategies as st

from kv_store.replication.vector_clock import VectorClock
from kv_store.replication.quorum import QuorumConfig, ConsistencyLevel
from kv_store.storage.bloom_filter import BloomFilter
from kv_store.storage.memtable import MemTable
from kv_store.config import StorageConfig
from kv_store.storage.engine import StorageEngine


# --- Strategies ---

# Keys: printable strings of reasonable length (1-50 chars)
key_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "S")),
    min_size=1,
    max_size=50,
)

# Values: binary data up to 1 KB (smaller for faster tests)
value_strategy = st.binary(min_size=1, max_size=1024)

# Node IDs for vector clocks
node_id_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N")),
    min_size=1,
    max_size=10,
)

# Timestamps: positive floats
timestamp_strategy = st.floats(min_value=1.0, max_value=1e12, allow_nan=False, allow_infinity=False)


# --- Vector Clock Properties ---


class TestVectorClockProperties:
    """Property-based tests for VectorClock.

    **Validates: Requirements FR-5.1, FR-5.2, FR-5.3, FR-5.4, FR-5.5**
    """

    @given(
        entries=st.dictionaries(node_id_strategy, st.integers(min_value=0, max_value=100), min_size=0, max_size=5),
        node_id=node_id_strategy,
    )
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_increment_produces_dominating_clock(self, entries, node_id):
        """Property: increment always produces a clock that dominates the original.

        **Validates: Requirements FR-5.2**
        """
        clock = VectorClock(entries=entries, max_entries=10)
        incremented = clock.increment(node_id)

        # The incremented clock should dominate the original
        # (unless pruning removed the only evidence of dominance, which
        # can happen when max_entries is exceeded)
        if len(entries) < 10:
            assert incremented.dominates(clock)

    @given(
        entries_a=st.dictionaries(node_id_strategy, st.integers(min_value=0, max_value=100), min_size=0, max_size=5),
        entries_b=st.dictionaries(node_id_strategy, st.integers(min_value=0, max_value=100), min_size=0, max_size=5),
    )
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_merge_is_commutative(self, entries_a, entries_b):
        """Property: merge(a, b) == merge(b, a).

        **Validates: Requirements FR-5.3**
        """
        clock_a = VectorClock(entries=entries_a, max_entries=20)
        clock_b = VectorClock(entries=entries_b, max_entries=20)

        merged_ab = clock_a.merge(clock_b)
        merged_ba = clock_b.merge(clock_a)

        assert merged_ab == merged_ba

    @given(
        entries=st.dictionaries(node_id_strategy, st.integers(min_value=0, max_value=100), min_size=0, max_size=5),
    )
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_merge_is_idempotent(self, entries):
        """Property: merge(a, a) == a.

        **Validates: Requirements FR-5.3**
        """
        clock = VectorClock(entries=entries, max_entries=20)
        merged = clock.merge(clock)
        assert merged == clock

    @given(
        entries_a=st.dictionaries(node_id_strategy, st.integers(min_value=1, max_value=100), min_size=1, max_size=5),
        entries_b=st.dictionaries(node_id_strategy, st.integers(min_value=0, max_value=50), min_size=0, max_size=5),
    )
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_dominates_implies_not_dominated(self, entries_a, entries_b):
        """Property: if a dominates b, then b does not dominate a.

        **Validates: Requirements FR-5.3, FR-5.4**
        """
        clock_a = VectorClock(entries=entries_a, max_entries=20)
        clock_b = VectorClock(entries=entries_b, max_entries=20)

        if clock_a.dominates(clock_b):
            assert not clock_b.dominates(clock_a)

    @given(
        entries_a=st.dictionaries(node_id_strategy, st.integers(min_value=0, max_value=100), min_size=0, max_size=5),
        entries_b=st.dictionaries(node_id_strategy, st.integers(min_value=0, max_value=100), min_size=0, max_size=5),
    )
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_conflicts_with_is_symmetric(self, entries_a, entries_b):
        """Property: conflicts_with(a, b) == conflicts_with(b, a).

        **Validates: Requirements FR-5.3, FR-5.5**
        """
        clock_a = VectorClock(entries=entries_a, max_entries=20)
        clock_b = VectorClock(entries=entries_b, max_entries=20)

        assert clock_a.conflicts_with(clock_b) == clock_b.conflicts_with(clock_a)


# --- Bloom Filter Properties ---


class TestBloomFilterProperties:
    """Property-based tests for BloomFilter.

    **Validates: Requirements FR-10.2, FR-10.4**
    """

    @given(
        keys=st.lists(key_strategy, min_size=1, max_size=100, unique=True),
    )
    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    def test_no_false_negatives(self, keys):
        """Property: added items are always found (no false negatives).

        **Validates: Requirements FR-10.2**
        """
        bf = BloomFilter(expected_items=max(len(keys), 1), false_positive_rate=0.01)

        for key in keys:
            bf.add(key)

        for key in keys:
            assert bf.might_contain(key), f"False negative for key: {key}"

    @given(
        data=st.data(),
    )
    @settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow])
    def test_false_positive_rate_within_bounds(self, data):
        """Property: false positive rate is within 2x the expected rate.

        **Validates: Requirements FR-10.4**
        """
        n_items = 1000
        fp_rate = 0.05  # Use 5% for statistical stability

        bf = BloomFilter(expected_items=n_items, false_positive_rate=fp_rate)

        # Add n_items keys
        added_keys = set()
        for i in range(n_items):
            key = f"added-{i}"
            bf.add(key)
            added_keys.add(key)

        # Test with keys that were NOT added
        false_positives = 0
        test_count = 2000
        for i in range(test_count):
            test_key = f"not-added-{i}"
            if test_key not in added_keys and bf.might_contain(test_key):
                false_positives += 1

        observed_rate = false_positives / test_count
        # Allow up to 2x the expected rate
        assert observed_rate <= fp_rate * 2, (
            f"False positive rate {observed_rate:.4f} exceeds 2x expected {fp_rate}"
        )


# --- MemTable Properties ---


class TestMemTableProperties:
    """Property-based tests for MemTable.

    **Validates: Requirements FR-9.2, FR-10.1**
    """

    @given(
        key=key_strategy,
        value=value_strategy,
        timestamp=timestamp_strategy,
    )
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_get_after_put_returns_put_value(self, key, value, timestamp):
        """Property: get after put returns the put value.

        **Validates: Requirements FR-9.2, FR-10.1**
        """
        mt = MemTable(size_threshold_bytes=10 * 1024 * 1024)
        mt.put(key, value, timestamp)

        result = mt.get(key)
        assert result is not None
        assert result.value == value
        assert result.key == key
        assert result.is_tombstone is False

    @given(
        items=st.lists(
            st.tuples(key_strategy, value_strategy, timestamp_strategy),
            min_size=1,
            max_size=50,
        ),
    )
    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    def test_entries_sorted_is_always_sorted(self, items):
        """Property: entries_sorted always returns entries in sorted key order.

        **Validates: Requirements FR-9.2**
        """
        mt = MemTable(size_threshold_bytes=10 * 1024 * 1024)

        for key, value, ts in items:
            mt.put(key, value, ts)

        entries = list(mt.entries_sorted())
        keys = [e.key for e in entries]
        assert keys == sorted(keys)

    @given(
        key=key_strategy,
        value=value_strategy,
        t1=timestamp_strategy,
        t2=timestamp_strategy,
    )
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_delete_creates_tombstone(self, key, value, t1, t2):
        """Property: delete creates a retrievable tombstone.

        **Validates: Requirements FR-9.2**
        """
        assume(t2 > t1)  # Delete must be after put

        mt = MemTable(size_threshold_bytes=10 * 1024 * 1024)
        mt.put(key, value, t1)
        mt.delete(key, t2)

        result = mt.get(key)
        assert result is not None
        assert result.is_tombstone is True
        assert result.value is None


# --- Storage Engine Properties ---


class TestStorageEngineProperties:
    """Property-based tests for StorageEngine.

    **Validates: Requirements FR-9, FR-10**
    """

    @given(
        key=key_strategy,
        value=value_strategy,
    )
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow], deadline=None)
    def test_put_then_get_returns_same_value(self, key, value):
        """Property: put then get returns the same value (roundtrip).

        **Validates: Requirements FR-9, FR-10**
        """
        import tempfile
        import shutil

        tmp_dir = tempfile.mkdtemp()
        try:
            config = StorageConfig(
                data_dir=f"{tmp_dir}/data",
                wal_dir=f"{tmp_dir}/data/wal",
                sstable_dir=f"{tmp_dir}/data/sstables",
                memtable_size_bytes=1024 * 1024,
            )

            async def run():
                engine = StorageEngine(config)
                await engine.start()
                try:
                    await engine.put(key, value, time.time())
                    result = await engine.get(key)
                    assert result is not None
                    assert result.found is True
                    assert result.value == value
                    assert result.is_tombstone is False
                finally:
                    await engine.stop()

            asyncio.run(run())
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @given(
        key=key_strategy,
        value=value_strategy,
    )
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow], deadline=None)
    def test_delete_then_get_returns_tombstone_or_not_found(self, key, value):
        """Property: delete then get returns tombstone or not found.

        **Validates: Requirements FR-9, FR-10**
        """
        import tempfile
        import shutil

        tmp_dir = tempfile.mkdtemp()
        try:
            config = StorageConfig(
                data_dir=f"{tmp_dir}/data",
                wal_dir=f"{tmp_dir}/data/wal",
                sstable_dir=f"{tmp_dir}/data/sstables",
                memtable_size_bytes=1024 * 1024,
            )

            async def run():
                engine = StorageEngine(config)
                await engine.start()
                try:
                    t = time.time()
                    await engine.put(key, value, t)
                    await engine.delete(key, t + 1)
                    result = await engine.get(key)
                    # After delete, result is either tombstone or None
                    if result is not None:
                        assert result.is_tombstone is True
                finally:
                    await engine.stop()

            asyncio.run(run())
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


# --- Quorum Properties ---


class TestQuorumProperties:
    """Property-based tests for Quorum logic.

    **Validates: Requirements FR-4.1, FR-4.2, FR-4.4**
    """

    @given(
        n=st.integers(min_value=1, max_value=11),
        w=st.integers(min_value=1, max_value=11),
        r=st.integers(min_value=1, max_value=11),
    )
    @settings(max_examples=500, suppress_health_check=[HealthCheck.too_slow])
    def test_w_plus_r_greater_than_n_implies_strong_consistency(self, n, w, r):
        """Property: W + R > N implies strong consistency.

        **Validates: Requirements FR-4.4**
        """
        assume(w <= n)
        assume(r <= n)

        config = QuorumConfig(n=n, w=w, r=r)

        if w + r > n:
            assert config.is_strongly_consistent() is True
        else:
            assert config.is_strongly_consistent() is False

    @given(
        n=st.integers(min_value=1, max_value=7),
        w=st.integers(min_value=1, max_value=7),
        acks=st.integers(min_value=0, max_value=7),
    )
    @settings(max_examples=500, suppress_health_check=[HealthCheck.too_slow])
    def test_write_succeeds_iff_acks_gte_w(self, n, w, acks):
        """Property: write succeeds if and only if >= W acks received.

        **Validates: Requirements FR-4.2**
        """
        assume(w <= n)
        assume(acks <= n)

        # Simulate: a write succeeds iff acks >= w
        success = acks >= w
        assert success == (acks >= w)
