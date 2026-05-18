"""Tests for the LSM-tree StorageEngine.

Tests cover:
- Put and get roundtrip
- Delete returns tombstone
- Data survives restart (WAL replay)
- MemTable flush creates SSTable
- Read after flush finds data in SSTable
- Key/value size validation
"""

import os
import time

import pytest

from kv_store.config import StorageConfig
from kv_store.storage.engine import StorageEngine


@pytest.fixture
def storage_config(tmp_path):
    """Provide a StorageConfig pointing at a temporary directory."""
    return StorageConfig(
        data_dir=str(tmp_path / "data"),
        wal_dir=str(tmp_path / "data" / "wal"),
        sstable_dir=str(tmp_path / "data" / "sstables"),
        memtable_size_bytes=4 * 1024 * 1024,  # 4 MB
        bloom_filter_fp_rate=0.01,
        compaction_threshold=4,
    )


@pytest.fixture
async def engine(storage_config):
    """Provide a started StorageEngine, stopped after the test."""
    eng = StorageEngine(storage_config)
    await eng.start()
    yield eng
    await eng.stop()


class TestPutAndGetRoundtrip:
    """Test basic put/get operations."""

    async def test_put_and_get_single_key(self, engine):
        """Put a key-value pair and retrieve it."""
        await engine.put("hello", b"world", 1.0)

        result = await engine.get("hello")

        assert result is not None
        assert result.key == "hello"
        assert result.value == b"world"
        assert result.timestamp == 1.0
        assert result.is_tombstone is False
        assert result.found is True

    async def test_put_and_get_multiple_keys(self, engine):
        """Put multiple keys and retrieve each one."""
        await engine.put("key1", b"value1", 1.0)
        await engine.put("key2", b"value2", 2.0)
        await engine.put("key3", b"value3", 3.0)

        r1 = await engine.get("key1")
        r2 = await engine.get("key2")
        r3 = await engine.get("key3")

        assert r1 is not None and r1.value == b"value1"
        assert r2 is not None and r2.value == b"value2"
        assert r3 is not None and r3.value == b"value3"

    async def test_get_nonexistent_key_returns_none(self, engine):
        """Getting a key that was never written returns None."""
        result = await engine.get("nonexistent")
        assert result is None

    async def test_put_overwrites_previous_value(self, engine):
        """A newer put for the same key overwrites the old value."""
        await engine.put("key", b"old_value", 1.0)
        await engine.put("key", b"new_value", 2.0)

        result = await engine.get("key")

        assert result is not None
        assert result.value == b"new_value"
        assert result.timestamp == 2.0


class TestDeleteReturnsTombstone:
    """Test that delete writes a tombstone marker."""

    async def test_delete_existing_key_returns_tombstone(self, engine):
        """Deleting an existing key makes get return a tombstone."""
        await engine.put("to_delete", b"value", 1.0)
        await engine.delete("to_delete", 2.0)

        result = await engine.get("to_delete")

        assert result is not None
        assert result.is_tombstone is True
        assert result.found is True
        assert result.timestamp == 2.0

    async def test_delete_nonexistent_key_creates_tombstone(self, engine):
        """Deleting a key that never existed still creates a tombstone."""
        await engine.delete("never_existed", 1.0)

        result = await engine.get("never_existed")

        assert result is not None
        assert result.is_tombstone is True
        assert result.found is True


class TestDataSurvivesRestart:
    """Test that data persists across engine restarts via WAL replay."""

    async def test_data_survives_restart(self, storage_config):
        """Data written before stop is available after a fresh start (WAL replay)."""
        # Write data with first engine instance
        engine1 = StorageEngine(storage_config)
        await engine1.start()
        await engine1.put("persist_key", b"persist_value", 100.0)
        await engine1.put("another_key", b"another_value", 200.0)
        # Stop without flushing memtable (data is in WAL)
        engine1._started = False  # bypass the flush in stop()

        # Start a new engine instance — should replay WAL
        engine2 = StorageEngine(storage_config)
        await engine2.start()

        result1 = await engine2.get("persist_key")
        result2 = await engine2.get("another_key")

        assert result1 is not None
        assert result1.value == b"persist_value"
        assert result1.timestamp == 100.0

        assert result2 is not None
        assert result2.value == b"another_value"
        assert result2.timestamp == 200.0

        await engine2.stop()

    async def test_delete_survives_restart(self, storage_config):
        """A delete (tombstone) persists across restarts via WAL replay."""
        engine1 = StorageEngine(storage_config)
        await engine1.start()
        await engine1.put("key", b"value", 1.0)
        await engine1.delete("key", 2.0)
        engine1._started = False  # bypass flush in stop()

        engine2 = StorageEngine(storage_config)
        await engine2.start()

        result = await engine2.get("key")

        assert result is not None
        assert result.is_tombstone is True
        assert result.timestamp == 2.0

        await engine2.stop()


class TestMemtableFlushCreatesSSTable:
    """Test that flushing the MemTable creates an SSTable file on disk."""

    async def test_flush_creates_sstable_file(self, tmp_path):
        """When MemTable exceeds threshold, a flush creates an SSTable file."""
        config = StorageConfig(
            data_dir=str(tmp_path / "data"),
            wal_dir=str(tmp_path / "data" / "wal"),
            sstable_dir=str(tmp_path / "data" / "sstables"),
            memtable_size_bytes=100,  # Very small threshold to trigger flush
            bloom_filter_fp_rate=0.01,
            compaction_threshold=4,
        )

        engine = StorageEngine(config)
        await engine.start()

        # Write enough data to exceed the 100-byte memtable threshold
        await engine.put("key1", b"x" * 50, 1.0)
        await engine.put("key2", b"y" * 50, 2.0)

        # Check that an SSTable file was created
        sstable_dir = tmp_path / "data" / "sstables"
        sst_files = list(sstable_dir.glob("*.sst"))
        assert len(sst_files) >= 1

        await engine.stop()

    async def test_flush_clears_memtable(self, tmp_path):
        """After flush, the MemTable is cleared (size resets)."""
        config = StorageConfig(
            data_dir=str(tmp_path / "data"),
            wal_dir=str(tmp_path / "data" / "wal"),
            sstable_dir=str(tmp_path / "data" / "sstables"),
            memtable_size_bytes=100,  # Small threshold
            bloom_filter_fp_rate=0.01,
            compaction_threshold=4,
        )

        engine = StorageEngine(config)
        await engine.start()

        # Write enough to trigger flush
        await engine.put("key1", b"x" * 50, 1.0)
        await engine.put("key2", b"y" * 50, 2.0)

        # After flush, memtable should be empty or very small
        assert len(engine._memtable) == 0 or engine._memtable.size_bytes < 100

        await engine.stop()


class TestReadAfterFlush:
    """Test that data is still readable after being flushed to SSTable."""

    async def test_read_after_flush_finds_data_in_sstable(self, tmp_path):
        """Data flushed to SSTable is still retrievable via get."""
        config = StorageConfig(
            data_dir=str(tmp_path / "data"),
            wal_dir=str(tmp_path / "data" / "wal"),
            sstable_dir=str(tmp_path / "data" / "sstables"),
            memtable_size_bytes=100,  # Small threshold to trigger flush
            bloom_filter_fp_rate=0.01,
            compaction_threshold=4,
        )

        engine = StorageEngine(config)
        await engine.start()

        # Write data that will be flushed
        await engine.put("flushed_key", b"flushed_value", 1.0)
        await engine.put("another_flushed", b"x" * 80, 2.0)

        # Verify data is still accessible after flush
        result = await engine.get("flushed_key")
        assert result is not None
        assert result.value == b"flushed_value"
        assert result.timestamp == 1.0

        result2 = await engine.get("another_flushed")
        assert result2 is not None
        assert result2.value == b"x" * 80

        await engine.stop()

    async def test_read_after_flush_with_fresh_engine(self, tmp_path):
        """Data in SSTables is found by a fresh engine instance (no WAL)."""
        config = StorageConfig(
            data_dir=str(tmp_path / "data"),
            wal_dir=str(tmp_path / "data" / "wal"),
            sstable_dir=str(tmp_path / "data" / "sstables"),
            memtable_size_bytes=100,  # Small threshold
            bloom_filter_fp_rate=0.01,
            compaction_threshold=4,
        )

        # Write and flush data
        engine1 = StorageEngine(config)
        await engine1.start()
        await engine1.put("sst_key", b"sst_value", 1.0)
        await engine1.put("sst_key2", b"y" * 80, 2.0)
        await engine1.stop()  # stop flushes remaining memtable

        # Start fresh engine — WAL is truncated after flush, data is in SSTable
        engine2 = StorageEngine(config)
        await engine2.start()

        result = await engine2.get("sst_key")
        assert result is not None
        assert result.value == b"sst_value"

        result2 = await engine2.get("sst_key2")
        assert result2 is not None
        assert result2.value == b"y" * 80

        await engine2.stop()


class TestKeyValueSizeValidation:
    """Test key and value size limit enforcement."""

    async def test_key_at_max_size_accepted(self, engine):
        """A key exactly at 256 bytes UTF-8 is accepted."""
        max_key = "k" * 256  # 256 ASCII chars = 256 bytes UTF-8
        await engine.put(max_key, b"value", 1.0)

        result = await engine.get(max_key)
        assert result is not None
        assert result.value == b"value"

    async def test_key_exceeding_max_size_rejected(self, engine):
        """A key exceeding 256 bytes UTF-8 raises ValueError."""
        oversized_key = "k" * 257  # 257 bytes
        with pytest.raises(ValueError, match="Key exceeds maximum size"):
            await engine.put(oversized_key, b"value", 1.0)

    async def test_value_at_max_size_accepted(self, engine):
        """A value exactly at 10 KB is accepted."""
        max_value = b"v" * (10 * 1024)  # 10 KB
        await engine.put("key", max_value, 1.0)

        result = await engine.get("key")
        assert result is not None
        assert result.value == max_value

    async def test_value_exceeding_max_size_rejected(self, engine):
        """A value exceeding 10 KB raises ValueError."""
        oversized_value = b"v" * (10 * 1024 + 1)  # 10 KB + 1 byte
        with pytest.raises(ValueError, match="Value exceeds maximum size"):
            await engine.put("key", oversized_value, 1.0)

    async def test_delete_validates_key_size(self, engine):
        """Delete also validates key size."""
        oversized_key = "k" * 257
        with pytest.raises(ValueError, match="Key exceeds maximum size"):
            await engine.delete(oversized_key, 1.0)

    async def test_unicode_key_size_measured_in_bytes(self, engine):
        """Key size is measured in UTF-8 bytes, not characters."""
        # Each '日' character is 3 bytes in UTF-8
        # 86 characters * 3 bytes = 258 bytes > 256 limit
        oversized_unicode_key = "日" * 86
        with pytest.raises(ValueError, match="Key exceeds maximum size"):
            await engine.put(oversized_unicode_key, b"value", 1.0)

        # 85 characters * 3 bytes = 255 bytes <= 256 limit
        valid_unicode_key = "日" * 85
        await engine.put(valid_unicode_key, b"value", 1.0)
        result = await engine.get(valid_unicode_key)
        assert result is not None
        assert result.value == b"value"
