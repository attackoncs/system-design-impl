"""Tests for the SSTable implementation.

Tests cover:
- Creating an SSTable from a MemTable and reading back entries
- Getting correct values for existing keys
- Getting None for non-existent keys
- Bloom filter integration (may_contain)
- Entries iterator returns sorted order
- Metadata is populated correctly
- Tombstone entry handling
"""

import asyncio
import time

import pytest

from kv_store.storage.memtable import MemTable
from kv_store.storage.sstable import SSTable, SSTableEntry, SSTableMetadata


@pytest.fixture
def populated_memtable():
    """Create a MemTable with several entries for testing."""
    mt = MemTable()
    ts = time.time()
    mt.put("apple", b"red", ts)
    mt.put("banana", b"yellow", ts + 1)
    mt.put("cherry", b"dark red", ts + 2)
    mt.put("date", b"brown", ts + 3)
    mt.put("elderberry", b"purple", ts + 4)
    mt.put("fig", b"green", ts + 5)
    mt.put("grape", b"purple", ts + 6)
    mt.put("honeydew", b"green", ts + 7)
    return mt


@pytest.fixture
def memtable_with_tombstone():
    """Create a MemTable with a tombstone entry."""
    mt = MemTable()
    ts = time.time()
    mt.put("alive", b"value1", ts)
    mt.put("deleted", b"value2", ts + 1)
    mt.delete("deleted", ts + 2)
    mt.put("also_alive", b"value3", ts + 3)
    return mt


@pytest.fixture
async def sstable_path(tmp_path, populated_memtable):
    """Create an SSTable file and return its path."""
    file_path = str(tmp_path / "test.sst")
    await SSTable.create_from_memtable(populated_memtable, file_path, level=0)
    return file_path


class TestSSTableCreation:
    """Tests for SSTable creation from MemTable."""

    async def test_create_from_memtable(self, tmp_path, populated_memtable):
        """Test that an SSTable can be created from a MemTable."""
        file_path = str(tmp_path / "test.sst")
        sstable = await SSTable.create_from_memtable(populated_memtable, file_path, level=0)
        assert sstable is not None
        assert sstable.metadata.entry_count == 8

    async def test_create_from_memtable_creates_file(self, tmp_path, populated_memtable):
        """Test that create_from_memtable actually creates a file on disk."""
        file_path = str(tmp_path / "subdir" / "test.sst")
        await SSTable.create_from_memtable(populated_memtable, file_path, level=0)
        from pathlib import Path
        assert Path(file_path).exists()

    async def test_create_from_empty_memtable_raises(self, tmp_path):
        """Test that creating from an empty MemTable raises ValueError."""
        mt = MemTable()
        file_path = str(tmp_path / "empty.sst")
        with pytest.raises(ValueError, match="empty"):
            await SSTable.create_from_memtable(mt, file_path, level=0)

    async def test_read_back_all_entries(self, tmp_path, populated_memtable):
        """Test that all entries can be read back from the SSTable."""
        file_path = str(tmp_path / "test.sst")
        sstable = await SSTable.create_from_memtable(populated_memtable, file_path, level=0)

        entries = list(sstable.entries())
        assert len(entries) == 8
        keys = [e.key for e in entries]
        assert keys == sorted(keys)


class TestSSTableGet:
    """Tests for SSTable key lookup."""

    async def test_get_existing_key(self, tmp_path, populated_memtable):
        """Test that get returns the correct value for an existing key."""
        file_path = str(tmp_path / "test.sst")
        sstable = await SSTable.create_from_memtable(populated_memtable, file_path, level=0)

        entry = await sstable.get("banana")
        assert entry is not None
        assert entry.key == "banana"
        assert entry.value == b"yellow"
        assert entry.is_tombstone is False

    async def test_get_nonexistent_key(self, tmp_path, populated_memtable):
        """Test that get returns None for a non-existent key."""
        file_path = str(tmp_path / "test.sst")
        sstable = await SSTable.create_from_memtable(populated_memtable, file_path, level=0)

        entry = await sstable.get("zebra")
        assert entry is None

    async def test_get_key_not_in_range(self, tmp_path, populated_memtable):
        """Test that get returns None for a key outside the min/max range."""
        file_path = str(tmp_path / "test.sst")
        sstable = await SSTable.create_from_memtable(populated_memtable, file_path, level=0)

        # Key before min_key
        entry = await sstable.get("aaa")
        assert entry is None

        # Key after max_key
        entry = await sstable.get("zzz")
        assert entry is None

    async def test_get_first_key(self, tmp_path, populated_memtable):
        """Test getting the first key in the SSTable."""
        file_path = str(tmp_path / "test.sst")
        sstable = await SSTable.create_from_memtable(populated_memtable, file_path, level=0)

        entry = await sstable.get("apple")
        assert entry is not None
        assert entry.key == "apple"
        assert entry.value == b"red"

    async def test_get_last_key(self, tmp_path, populated_memtable):
        """Test getting the last key in the SSTable."""
        file_path = str(tmp_path / "test.sst")
        sstable = await SSTable.create_from_memtable(populated_memtable, file_path, level=0)

        entry = await sstable.get("honeydew")
        assert entry is not None
        assert entry.key == "honeydew"
        assert entry.value == b"green"


class TestSSTableBloomFilter:
    """Tests for Bloom filter integration."""

    async def test_may_contain_existing_key(self, tmp_path, populated_memtable):
        """Test that may_contain returns True for existing keys."""
        file_path = str(tmp_path / "test.sst")
        sstable = await SSTable.create_from_memtable(populated_memtable, file_path, level=0)

        # All inserted keys must return True (no false negatives)
        assert sstable.may_contain("apple") is True
        assert sstable.may_contain("banana") is True
        assert sstable.may_contain("cherry") is True

    async def test_may_contain_nonexistent_key(self, tmp_path, populated_memtable):
        """Test that may_contain can return False for non-existent keys."""
        file_path = str(tmp_path / "test.sst")
        sstable = await SSTable.create_from_memtable(populated_memtable, file_path, level=0)

        # With a good bloom filter, most non-existent keys should return False
        # Test a batch and verify at least some return False
        false_count = 0
        for i in range(100):
            if not sstable.may_contain(f"nonexistent_key_{i}"):
                false_count += 1

        # With 8 items and 1% FP rate, most should be False
        assert false_count > 80


class TestSSTableEntries:
    """Tests for the entries iterator."""

    async def test_entries_sorted_order(self, tmp_path, populated_memtable):
        """Test that entries iterator returns keys in sorted order."""
        file_path = str(tmp_path / "test.sst")
        sstable = await SSTable.create_from_memtable(populated_memtable, file_path, level=0)

        entries = list(sstable.entries())
        keys = [e.key for e in entries]
        assert keys == sorted(keys)

    async def test_entries_values_correct(self, tmp_path, populated_memtable):
        """Test that entries have correct values."""
        file_path = str(tmp_path / "test.sst")
        sstable = await SSTable.create_from_memtable(populated_memtable, file_path, level=0)

        entries = list(sstable.entries())
        entry_map = {e.key: e for e in entries}

        assert entry_map["apple"].value == b"red"
        assert entry_map["banana"].value == b"yellow"
        assert entry_map["grape"].value == b"purple"


class TestSSTableMetadata:
    """Tests for SSTable metadata."""

    async def test_metadata_populated(self, tmp_path, populated_memtable):
        """Test that metadata is correctly populated."""
        file_path = str(tmp_path / "test.sst")
        sstable = await SSTable.create_from_memtable(populated_memtable, file_path, level=0)

        meta = sstable.metadata
        assert meta.file_path == file_path
        assert meta.level == 0
        assert meta.min_key == "apple"
        assert meta.max_key == "honeydew"
        assert meta.entry_count == 8
        assert meta.size_bytes > 0
        assert meta.created_at > 0

    async def test_metadata_level(self, tmp_path, populated_memtable):
        """Test that the level is correctly stored."""
        file_path = str(tmp_path / "test.sst")
        sstable = await SSTable.create_from_memtable(populated_memtable, file_path, level=3)

        assert sstable.metadata.level == 3


class TestSSTableTombstones:
    """Tests for tombstone handling."""

    async def test_tombstone_entry(self, tmp_path, memtable_with_tombstone):
        """Test that tombstone entries are correctly stored and retrieved."""
        file_path = str(tmp_path / "test.sst")
        sstable = await SSTable.create_from_memtable(memtable_with_tombstone, file_path, level=0)

        entry = await sstable.get("deleted")
        assert entry is not None
        assert entry.key == "deleted"
        assert entry.is_tombstone is True
        assert entry.value is None

    async def test_non_tombstone_entries_alongside(self, tmp_path, memtable_with_tombstone):
        """Test that non-tombstone entries work alongside tombstones."""
        file_path = str(tmp_path / "test.sst")
        sstable = await SSTable.create_from_memtable(memtable_with_tombstone, file_path, level=0)

        entry = await sstable.get("alive")
        assert entry is not None
        assert entry.value == b"value1"
        assert entry.is_tombstone is False

        entry = await sstable.get("also_alive")
        assert entry is not None
        assert entry.value == b"value3"
        assert entry.is_tombstone is False


class TestSSTableOpenExisting:
    """Tests for opening an existing SSTable file."""

    async def test_open_existing_file(self, tmp_path, populated_memtable):
        """Test that an existing SSTable file can be opened."""
        file_path = str(tmp_path / "test.sst")
        await SSTable.create_from_memtable(populated_memtable, file_path, level=0)

        # Open the same file again
        sstable2 = SSTable(file_path)
        assert sstable2.metadata.entry_count == 8

    def test_open_nonexistent_file_raises(self, tmp_path):
        """Test that opening a non-existent file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            SSTable(str(tmp_path / "nonexistent.sst"))

    def test_open_invalid_file_raises(self, tmp_path):
        """Test that opening an invalid file raises ValueError."""
        bad_file = tmp_path / "bad.sst"
        bad_file.write_bytes(b"this is not an sstable")
        with pytest.raises(ValueError):
            SSTable(str(bad_file))


class TestSSTableLargeDataset:
    """Tests with larger datasets to exercise sparse index."""

    async def test_many_entries_sparse_index(self, tmp_path):
        """Test with enough entries to have multiple sparse index entries."""
        mt = MemTable()
        ts = time.time()
        # Insert 100 entries to ensure multiple index entries (every 16th)
        for i in range(100):
            key = f"key_{i:04d}"
            mt.put(key, f"value_{i}".encode(), ts + i)

        file_path = str(tmp_path / "large.sst")
        sstable = await SSTable.create_from_memtable(mt, file_path, level=0)

        # Verify all entries can be retrieved
        for i in range(100):
            key = f"key_{i:04d}"
            entry = await sstable.get(key)
            assert entry is not None, f"Failed to get {key}"
            assert entry.value == f"value_{i}".encode()

    async def test_many_entries_metadata(self, tmp_path):
        """Test metadata with many entries."""
        mt = MemTable()
        ts = time.time()
        for i in range(50):
            key = f"key_{i:04d}"
            mt.put(key, f"value_{i}".encode(), ts + i)

        file_path = str(tmp_path / "large.sst")
        sstable = await SSTable.create_from_memtable(mt, file_path, level=1)

        assert sstable.metadata.entry_count == 50
        assert sstable.metadata.min_key == "key_0000"
        assert sstable.metadata.max_key == "key_0049"
        assert sstable.metadata.level == 1
