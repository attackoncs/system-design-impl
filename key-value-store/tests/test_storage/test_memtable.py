"""Tests for the MemTable implementation."""

import pytest

from kv_store.storage.memtable import MemTable, MemTableEntry


class TestMemTablePutAndGet:
    """Test put and get roundtrip."""

    def test_put_and_get_single_key(self):
        mt = MemTable()
        mt.put("key1", b"value1", timestamp=1.0)

        entry = mt.get("key1")
        assert entry is not None
        assert entry.key == "key1"
        assert entry.value == b"value1"
        assert entry.timestamp == 1.0
        assert entry.is_tombstone is False

    def test_get_nonexistent_key_returns_none(self):
        mt = MemTable()
        assert mt.get("missing") is None

    def test_put_multiple_keys(self):
        mt = MemTable()
        mt.put("a", b"val_a", timestamp=1.0)
        mt.put("b", b"val_b", timestamp=2.0)
        mt.put("c", b"val_c", timestamp=3.0)

        assert mt.get("a").value == b"val_a"
        assert mt.get("b").value == b"val_b"
        assert mt.get("c").value == b"val_c"

    def test_put_overwrites_with_newer_timestamp(self):
        mt = MemTable()
        mt.put("key1", b"old_value", timestamp=1.0)
        mt.put("key1", b"new_value", timestamp=2.0)

        entry = mt.get("key1")
        assert entry.value == b"new_value"
        assert entry.timestamp == 2.0

    def test_put_ignores_older_timestamp(self):
        mt = MemTable()
        mt.put("key1", b"new_value", timestamp=2.0)
        mt.put("key1", b"old_value", timestamp=1.0)

        entry = mt.get("key1")
        assert entry.value == b"new_value"
        assert entry.timestamp == 2.0

    def test_put_accepts_equal_timestamp(self):
        mt = MemTable()
        mt.put("key1", b"first", timestamp=1.0)
        mt.put("key1", b"second", timestamp=1.0)

        entry = mt.get("key1")
        assert entry.value == b"second"


class TestMemTableDelete:
    """Test delete creates tombstone."""

    def test_delete_creates_tombstone(self):
        mt = MemTable()
        mt.put("key1", b"value1", timestamp=1.0)
        mt.delete("key1", timestamp=2.0)

        entry = mt.get("key1")
        assert entry is not None
        assert entry.is_tombstone is True
        assert entry.value is None
        assert entry.timestamp == 2.0

    def test_delete_nonexistent_key_creates_tombstone(self):
        mt = MemTable()
        mt.delete("key1", timestamp=1.0)

        entry = mt.get("key1")
        assert entry is not None
        assert entry.is_tombstone is True
        assert entry.value is None

    def test_delete_ignored_with_older_timestamp(self):
        mt = MemTable()
        mt.put("key1", b"value1", timestamp=2.0)
        mt.delete("key1", timestamp=1.0)

        entry = mt.get("key1")
        assert entry.value == b"value1"
        assert entry.is_tombstone is False

    def test_put_after_delete_with_newer_timestamp(self):
        mt = MemTable()
        mt.put("key1", b"value1", timestamp=1.0)
        mt.delete("key1", timestamp=2.0)
        mt.put("key1", b"value2", timestamp=3.0)

        entry = mt.get("key1")
        assert entry.value == b"value2"
        assert entry.is_tombstone is False
        assert entry.timestamp == 3.0


class TestMemTableIsFull:
    """Test is_full triggers at threshold."""

    def test_empty_memtable_is_not_full(self):
        mt = MemTable(size_threshold_bytes=1024)
        assert mt.is_full() is False

    def test_memtable_becomes_full(self):
        # Use a small threshold so we can trigger it easily
        mt = MemTable(size_threshold_bytes=100)
        # Each entry is key_len + value_len + 64 overhead
        # "key" = 3 bytes, b"x" * 50 = 50 bytes => 3 + 50 + 64 = 117 bytes
        mt.put("key", b"x" * 50, timestamp=1.0)
        assert mt.is_full() is True

    def test_memtable_not_full_below_threshold(self):
        mt = MemTable(size_threshold_bytes=200)
        # "k" = 1 byte, b"v" = 1 byte => 1 + 1 + 64 = 66 bytes
        mt.put("k", b"v", timestamp=1.0)
        assert mt.is_full() is False


class TestMemTableEntriesSorted:
    """Test entries_sorted returns keys in order."""

    def test_entries_sorted_returns_sorted_keys(self):
        mt = MemTable()
        mt.put("cherry", b"3", timestamp=1.0)
        mt.put("apple", b"1", timestamp=2.0)
        mt.put("banana", b"2", timestamp=3.0)

        entries = list(mt.entries_sorted())
        keys = [e.key for e in entries]
        assert keys == ["apple", "banana", "cherry"]

    def test_entries_sorted_empty_memtable(self):
        mt = MemTable()
        entries = list(mt.entries_sorted())
        assert entries == []

    def test_entries_sorted_includes_tombstones(self):
        mt = MemTable()
        mt.put("b", b"val", timestamp=1.0)
        mt.delete("a", timestamp=2.0)

        entries = list(mt.entries_sorted())
        assert len(entries) == 2
        assert entries[0].key == "a"
        assert entries[0].is_tombstone is True
        assert entries[1].key == "b"
        assert entries[1].is_tombstone is False


class TestMemTableSizeTracking:
    """Test size tracking is approximately correct."""

    def test_size_starts_at_zero(self):
        mt = MemTable()
        assert mt.size_bytes == 0

    def test_size_increases_on_put(self):
        mt = MemTable()
        mt.put("key", b"value", timestamp=1.0)
        # "key" = 3 bytes, b"value" = 5 bytes, overhead = 64
        assert mt.size_bytes == 3 + 5 + 64

    def test_size_adjusts_on_overwrite(self):
        mt = MemTable()
        mt.put("key", b"short", timestamp=1.0)
        size_after_first = mt.size_bytes

        mt.put("key", b"a_longer_value", timestamp=2.0)
        # Old: 3 + 5 + 64 = 72
        # New: 3 + 14 + 64 = 81
        expected = 3 + len(b"a_longer_value") + 64
        assert mt.size_bytes == expected

    def test_size_increases_on_delete(self):
        mt = MemTable()
        mt.delete("key", timestamp=1.0)
        # "key" = 3 bytes, value = 0 (tombstone), overhead = 64
        assert mt.size_bytes == 3 + 0 + 64

    def test_size_adjusts_on_delete_after_put(self):
        mt = MemTable()
        mt.put("key", b"value", timestamp=1.0)
        mt.delete("key", timestamp=2.0)
        # Tombstone: "key" = 3 bytes, value = 0, overhead = 64
        assert mt.size_bytes == 3 + 0 + 64


class TestMemTableClear:
    """Test clear resets the MemTable."""

    def test_clear_removes_all_entries(self):
        mt = MemTable()
        mt.put("a", b"1", timestamp=1.0)
        mt.put("b", b"2", timestamp=2.0)

        mt.clear()

        assert mt.get("a") is None
        assert mt.get("b") is None
        assert len(mt) == 0

    def test_clear_resets_size(self):
        mt = MemTable()
        mt.put("key", b"value", timestamp=1.0)
        assert mt.size_bytes > 0

        mt.clear()
        assert mt.size_bytes == 0

    def test_clear_resets_is_full(self):
        mt = MemTable(size_threshold_bytes=100)
        mt.put("key", b"x" * 50, timestamp=1.0)
        assert mt.is_full() is True

        mt.clear()
        assert mt.is_full() is False


class TestMemTableLen:
    """Test __len__ returns entry count."""

    def test_len_empty(self):
        mt = MemTable()
        assert len(mt) == 0

    def test_len_after_puts(self):
        mt = MemTable()
        mt.put("a", b"1", timestamp=1.0)
        mt.put("b", b"2", timestamp=2.0)
        assert len(mt) == 2

    def test_len_overwrite_does_not_increase(self):
        mt = MemTable()
        mt.put("a", b"1", timestamp=1.0)
        mt.put("a", b"2", timestamp=2.0)
        assert len(mt) == 1

    def test_len_after_delete(self):
        mt = MemTable()
        mt.put("a", b"1", timestamp=1.0)
        mt.delete("a", timestamp=2.0)
        # Delete replaces the entry with a tombstone, count stays 1
        assert len(mt) == 1
