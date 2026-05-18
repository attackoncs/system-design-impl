"""Tests for the CompactionManager.

Tests cover:
- Merging SSTables with non-overlapping keys
- Merging with duplicate keys (keeps newest timestamp)
- Tombstone removal after grace period
- Compaction triggering at threshold
- Output SSTable is sorted
"""

import asyncio
import time

import pytest

from kv_store.storage.compaction import CompactionManager, CompactionStats
from kv_store.storage.memtable import MemTable
from kv_store.storage.sstable import SSTable


@pytest.fixture
def sstable_dir(tmp_path):
    """Create a temporary directory for SSTables."""
    d = tmp_path / "sstables"
    d.mkdir()
    return str(d)


async def _create_sstable(
    sstable_dir: str, entries: list[tuple[str, bytes | None, float, bool]], level: int = 0, suffix: str = ""
) -> SSTable:
    """Helper to create an SSTable from a list of (key, value, timestamp, is_tombstone) tuples."""
    memtable = MemTable(size_threshold_bytes=1024 * 1024)
    for key, value, timestamp, is_tombstone in entries:
        if is_tombstone:
            memtable.delete(key, timestamp)
        else:
            memtable.put(key, value, timestamp)

    file_path = f"{sstable_dir}/test_{suffix or id(entries)}.sst"
    return await SSTable.create_from_memtable(memtable, file_path, level=level)


@pytest.mark.asyncio
async def test_merge_non_overlapping_keys(sstable_dir):
    """Merging two SSTables with non-overlapping keys produces a sorted union."""
    sst1 = await _create_sstable(
        sstable_dir,
        [("a", b"val_a", 1.0, False), ("b", b"val_b", 1.0, False)],
        level=0,
        suffix="sst1",
    )
    sst2 = await _create_sstable(
        sstable_dir,
        [("c", b"val_c", 1.0, False), ("d", b"val_d", 1.0, False)],
        level=0,
        suffix="sst2",
    )

    manager = CompactionManager(sstable_dir, compaction_threshold=4)
    result = await manager.compact([sst1, sst2], level=0)

    # Verify all keys are present and sorted
    entries = list(result.entries())
    keys = [e.key for e in entries]
    assert keys == ["a", "b", "c", "d"]
    assert entries[0].value == b"val_a"
    assert entries[2].value == b"val_c"

    # Output should be at level 1
    assert result.metadata.level == 1


@pytest.mark.asyncio
async def test_merge_duplicate_keys_keeps_newest(sstable_dir):
    """When merging SSTables with duplicate keys, the newest timestamp wins."""
    sst1 = await _create_sstable(
        sstable_dir,
        [("key1", b"old_value", 1.0, False), ("key2", b"val2", 2.0, False)],
        level=0,
        suffix="dup1",
    )
    sst2 = await _create_sstable(
        sstable_dir,
        [("key1", b"new_value", 5.0, False), ("key3", b"val3", 3.0, False)],
        level=0,
        suffix="dup2",
    )

    manager = CompactionManager(sstable_dir, compaction_threshold=4)
    result = await manager.compact([sst1, sst2], level=0)

    entries = list(result.entries())
    keys = [e.key for e in entries]
    assert keys == ["key1", "key2", "key3"]

    # key1 should have the newer value
    key1_entry = next(e for e in entries if e.key == "key1")
    assert key1_entry.value == b"new_value"
    assert key1_entry.timestamp == 5.0


@pytest.mark.asyncio
async def test_tombstones_removed_after_grace_period(sstable_dir):
    """Tombstones older than the grace period are removed during compaction."""
    old_time = time.time() - 100000  # well past grace period
    recent_time = time.time()

    sst1 = await _create_sstable(
        sstable_dir,
        [
            ("alive_key", b"value", recent_time, False),
            ("old_dead_key", None, old_time, True),  # expired tombstone
        ],
        level=0,
        suffix="tomb1",
    )
    sst2 = await _create_sstable(
        sstable_dir,
        [
            ("recent_dead_key", None, recent_time, True),  # recent tombstone, keep it
        ],
        level=0,
        suffix="tomb2",
    )

    # Use a short grace period so old tombstones are removed
    manager = CompactionManager(sstable_dir, compaction_threshold=4, tombstone_grace_seconds=3600)
    result = await manager.compact([sst1, sst2], level=0)

    entries = list(result.entries())
    keys = [e.key for e in entries]

    # old_dead_key should be removed (tombstone expired)
    assert "old_dead_key" not in keys
    # recent_dead_key should still be present (tombstone not expired)
    assert "recent_dead_key" in keys
    # alive_key should be present
    assert "alive_key" in keys

    # Check stats
    stats = manager.get_compaction_stats()
    assert stats.tombstones_removed == 1


@pytest.mark.asyncio
async def test_compaction_triggers_at_threshold(sstable_dir):
    """maybe_compact triggers compaction when SSTables at a level exceed threshold."""
    # Create 4 SSTables at level 0 (threshold is 4)
    sstables = []
    for i in range(4):
        sst = await _create_sstable(
            sstable_dir,
            [(f"key_{i}", f"val_{i}".encode(), float(i), False)],
            level=0,
            suffix=f"thresh_{i}",
        )
        sstables.append(sst)

    manager = CompactionManager(sstable_dir, compaction_threshold=4)
    result = await manager.maybe_compact(sstables)

    # Compaction should have occurred
    assert result is not None
    assert result.metadata.level == 1

    entries = list(result.entries())
    assert len(entries) == 4


@pytest.mark.asyncio
async def test_no_compaction_below_threshold(sstable_dir):
    """maybe_compact returns None when no level exceeds the threshold."""
    sstables = []
    for i in range(3):  # below threshold of 4
        sst = await _create_sstable(
            sstable_dir,
            [(f"key_{i}", f"val_{i}".encode(), float(i), False)],
            level=0,
            suffix=f"below_{i}",
        )
        sstables.append(sst)

    manager = CompactionManager(sstable_dir, compaction_threshold=4)
    result = await manager.maybe_compact(sstables)

    assert result is None


@pytest.mark.asyncio
async def test_output_sstable_is_sorted(sstable_dir):
    """The output SSTable from compaction has entries in sorted key order."""
    # Create SSTables with interleaved keys
    sst1 = await _create_sstable(
        sstable_dir,
        [("banana", b"b", 1.0, False), ("date", b"d", 1.0, False)],
        level=0,
        suffix="sort1",
    )
    sst2 = await _create_sstable(
        sstable_dir,
        [("apple", b"a", 1.0, False), ("cherry", b"c", 1.0, False)],
        level=0,
        suffix="sort2",
    )

    manager = CompactionManager(sstable_dir, compaction_threshold=4)
    result = await manager.compact([sst1, sst2], level=0)

    entries = list(result.entries())
    keys = [e.key for e in entries]
    assert keys == sorted(keys)
    assert keys == ["apple", "banana", "cherry", "date"]


@pytest.mark.asyncio
async def test_compaction_stats(sstable_dir):
    """get_compaction_stats returns correct statistics after compaction."""
    sst1 = await _create_sstable(
        sstable_dir,
        [("a", b"val_a", 1.0, False), ("b", b"val_b", 1.0, False)],
        level=0,
        suffix="stats1",
    )
    sst2 = await _create_sstable(
        sstable_dir,
        [("c", b"val_c", 1.0, False), ("d", b"val_d", 1.0, False)],
        level=0,
        suffix="stats2",
    )

    manager = CompactionManager(sstable_dir, compaction_threshold=4)
    result = await manager.compact([sst1, sst2], level=0)

    stats = manager.get_compaction_stats()
    assert stats.input_sstables == 2
    assert stats.output_sstable == 1
    assert stats.keys_merged == 4
    assert stats.tombstones_removed == 0
    assert stats.bytes_before > 0
    assert stats.bytes_after > 0


@pytest.mark.asyncio
async def test_compaction_empty_sstable_list(sstable_dir):
    """maybe_compact with empty list returns None."""
    manager = CompactionManager(sstable_dir, compaction_threshold=4)
    result = await manager.maybe_compact([])
    assert result is None


@pytest.mark.asyncio
async def test_compaction_preserves_recent_tombstones(sstable_dir):
    """Recent tombstones within grace period are preserved in compacted output."""
    now = time.time()

    sst = await _create_sstable(
        sstable_dir,
        [
            ("key1", b"value1", now - 10, False),
            ("key2", None, now - 5, True),  # recent tombstone
        ],
        level=0,
        suffix="recent_tomb",
    )

    manager = CompactionManager(
        sstable_dir, compaction_threshold=4, tombstone_grace_seconds=3600
    )
    result = await manager.compact([sst], level=0)

    entries = list(result.entries())
    keys = [e.key for e in entries]
    assert "key2" in keys

    key2_entry = next(e for e in entries if e.key == "key2")
    assert key2_entry.is_tombstone is True


@pytest.mark.asyncio
async def test_maybe_compact_selects_lowest_level(sstable_dir):
    """maybe_compact compacts the lowest level that exceeds the threshold."""
    # Create 4 SSTables at level 0 and 2 at level 1
    level0_sstables = []
    for i in range(4):
        sst = await _create_sstable(
            sstable_dir,
            [(f"l0_key_{i}", f"val_{i}".encode(), float(i), False)],
            level=0,
            suffix=f"l0_{i}",
        )
        level0_sstables.append(sst)

    level1_sstables = []
    for i in range(2):
        sst = await _create_sstable(
            sstable_dir,
            [(f"l1_key_{i}", f"val_{i}".encode(), float(i), False)],
            level=1,
            suffix=f"l1_{i}",
        )
        level1_sstables.append(sst)

    all_sstables = level0_sstables + level1_sstables
    manager = CompactionManager(sstable_dir, compaction_threshold=4)
    result = await manager.maybe_compact(all_sstables)

    # Should compact level 0 (4 SSTables) into level 1
    assert result is not None
    assert result.metadata.level == 1
    entries = list(result.entries())
    assert len(entries) == 4
