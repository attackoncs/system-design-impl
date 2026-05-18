"""Single-node integration tests for the StorageEngine.

Tests the full lifecycle of the LSM-tree storage engine including:
- Put/get/delete operations
- Data persistence across restarts (WAL replay)
- MemTable flush and SSTable read path
- Stress testing with many keys
- Concurrent operations via asyncio
"""

import asyncio
import time

import pytest

from kv_store.config import StorageConfig
from kv_store.storage.engine import StorageEngine


@pytest.fixture
def storage_config(tmp_path):
    """Create a StorageConfig pointing to a temporary directory."""
    return StorageConfig(
        data_dir=str(tmp_path / "data"),
        wal_dir=str(tmp_path / "data" / "wal"),
        sstable_dir=str(tmp_path / "data" / "sstables"),
        memtable_size_bytes=4 * 1024,  # 4 KB for faster flush in tests
        bloom_filter_fp_rate=0.01,
        compaction_threshold=4,
    )


@pytest.fixture
async def engine(storage_config):
    """Create and start a StorageEngine, stopping it after the test."""
    eng = StorageEngine(storage_config)
    await eng.start()
    yield eng
    await eng.stop()


class TestPutGetDeleteLifecycle:
    """Test the full put/get/delete lifecycle on a single node."""

    async def test_put_then_get(self, engine):
        """Put a value and retrieve it."""
        await engine.put("hello", b"world", time.time())
        result = await engine.get("hello")
        assert result is not None
        assert result.found is True
        assert result.value == b"world"
        assert result.is_tombstone is False

    async def test_put_overwrite(self, engine):
        """Overwriting a key returns the latest value."""
        t1 = time.time()
        await engine.put("key", b"value1", t1)
        await engine.put("key", b"value2", t1 + 1)
        result = await engine.get("key")
        assert result is not None
        assert result.value == b"value2"

    async def test_delete_marks_tombstone(self, engine):
        """Deleting a key creates a tombstone."""
        t = time.time()
        await engine.put("key", b"value", t)
        await engine.delete("key", t + 1)
        result = await engine.get("key")
        assert result is not None
        assert result.is_tombstone is True

    async def test_get_nonexistent_key(self, engine):
        """Getting a key that was never written returns None."""
        result = await engine.get("nonexistent")
        assert result is None

    async def test_put_get_delete_get(self, engine):
        """Full lifecycle: put, get, delete, get."""
        t = time.time()
        await engine.put("lifecycle", b"data", t)

        result = await engine.get("lifecycle")
        assert result is not None
        assert result.value == b"data"

        await engine.delete("lifecycle", t + 1)

        result = await engine.get("lifecycle")
        assert result is not None
        assert result.is_tombstone is True

    async def test_multiple_keys(self, engine):
        """Multiple distinct keys can be stored and retrieved."""
        t = time.time()
        keys = {f"key-{i}": f"value-{i}".encode() for i in range(20)}
        for k, v in keys.items():
            await engine.put(k, v, t)

        for k, v in keys.items():
            result = await engine.get(k)
            assert result is not None
            assert result.value == v


class TestDataPersistenceAcrossRestart:
    """Test that data persists across node restart via WAL replay."""

    async def test_data_survives_restart(self, storage_config):
        """Data written before stop is available after restart."""
        t = time.time()

        # Write data and stop
        engine1 = StorageEngine(storage_config)
        await engine1.start()
        await engine1.put("persist-key", b"persist-value", t)
        await engine1.stop()

        # Restart and verify
        engine2 = StorageEngine(storage_config)
        await engine2.start()
        result = await engine2.get("persist-key")
        assert result is not None
        assert result.value == b"persist-value"
        await engine2.stop()

    async def test_multiple_writes_survive_restart(self, storage_config):
        """Multiple writes before stop are all available after restart."""
        t = time.time()

        engine1 = StorageEngine(storage_config)
        await engine1.start()
        for i in range(10):
            await engine1.put(f"key-{i}", f"val-{i}".encode(), t + i)
        await engine1.stop()

        engine2 = StorageEngine(storage_config)
        await engine2.start()
        for i in range(10):
            result = await engine2.get(f"key-{i}")
            assert result is not None
            assert result.value == f"val-{i}".encode()
        await engine2.stop()

    async def test_delete_persists_across_restart(self, storage_config):
        """A delete (tombstone) persists across restart."""
        t = time.time()

        engine1 = StorageEngine(storage_config)
        await engine1.start()
        await engine1.put("del-key", b"del-value", t)
        await engine1.delete("del-key", t + 1)
        await engine1.stop()

        engine2 = StorageEngine(storage_config)
        await engine2.start()
        result = await engine2.get("del-key")
        assert result is not None
        assert result.is_tombstone is True
        await engine2.stop()


class TestMemtableFlushAndSSTableRead:
    """Test that memtable flush and SSTable read path work correctly."""

    async def test_flush_creates_sstable(self, storage_config):
        """Writing enough data triggers a flush and data is still readable."""
        engine = StorageEngine(storage_config)
        await engine.start()

        t = time.time()
        # Write enough data to trigger flush (memtable threshold is 4 KB)
        # Each entry is roughly key + value + overhead (~100 bytes)
        # Need ~40+ entries to exceed 4 KB
        for i in range(60):
            key = f"flush-key-{i:04d}"
            value = b"x" * 50
            await engine.put(key, value, t + i)

        # Verify all data is still readable (some from SSTable, some from MemTable)
        for i in range(60):
            key = f"flush-key-{i:04d}"
            result = await engine.get(key)
            assert result is not None, f"Key {key} not found after flush"
            assert result.value == b"x" * 50

        await engine.stop()

    async def test_data_readable_after_flush_and_restart(self, storage_config):
        """Data flushed to SSTable is readable after restart without WAL."""
        engine1 = StorageEngine(storage_config)
        await engine1.start()

        t = time.time()
        # Write enough to trigger flush
        for i in range(60):
            key = f"sstable-key-{i:04d}"
            value = f"sstable-val-{i}".encode()
            await engine1.put(key, value, t + i)

        await engine1.stop()

        # Restart - data should be in SSTables
        engine2 = StorageEngine(storage_config)
        await engine2.start()

        for i in range(60):
            key = f"sstable-key-{i:04d}"
            result = await engine2.get(key)
            assert result is not None, f"Key {key} not found after restart"
            assert result.value == f"sstable-val-{i}".encode()

        await engine2.stop()


class TestStress:
    """Stress tests with a large number of keys."""

    async def test_large_number_of_keys(self, storage_config):
        """Write and read back 500 keys."""
        engine = StorageEngine(storage_config)
        await engine.start()

        t = time.time()
        num_keys = 500
        for i in range(num_keys):
            key = f"stress-{i:06d}"
            value = f"data-{i}".encode()
            await engine.put(key, value, t + i * 0.001)

        # Verify all keys
        for i in range(num_keys):
            key = f"stress-{i:06d}"
            result = await engine.get(key)
            assert result is not None, f"Key {key} not found"
            assert result.value == f"data-{i}".encode()

        await engine.stop()

    async def test_overwrite_same_key_many_times(self, storage_config):
        """Overwriting the same key many times returns the latest value."""
        engine = StorageEngine(storage_config)
        await engine.start()

        t = time.time()
        for i in range(100):
            await engine.put("hot-key", f"version-{i}".encode(), t + i)

        result = await engine.get("hot-key")
        assert result is not None
        assert result.value == b"version-99"

        await engine.stop()


class TestConcurrentOperations:
    """Test concurrent operations via asyncio."""

    async def test_concurrent_puts(self, storage_config):
        """Multiple concurrent puts don't corrupt data."""
        engine = StorageEngine(storage_config)
        await engine.start()

        t = time.time()

        async def write_batch(prefix: str, count: int):
            for i in range(count):
                await engine.put(f"{prefix}-{i}", f"{prefix}-val-{i}".encode(), t + i)

        # Run multiple batches concurrently
        await asyncio.gather(
            write_batch("a", 50),
            write_batch("b", 50),
            write_batch("c", 50),
        )

        # Verify all data
        for prefix in ["a", "b", "c"]:
            for i in range(50):
                result = await engine.get(f"{prefix}-{i}")
                assert result is not None, f"Key {prefix}-{i} not found"
                assert result.value == f"{prefix}-val-{i}".encode()

        await engine.stop()

    async def test_concurrent_reads_and_writes(self, storage_config):
        """Concurrent reads and writes don't cause errors."""
        engine = StorageEngine(storage_config)
        await engine.start()

        t = time.time()

        # Pre-populate some data
        for i in range(20):
            await engine.put(f"rw-{i}", f"initial-{i}".encode(), t)

        async def reader():
            for i in range(20):
                await engine.get(f"rw-{i}")

        async def writer():
            for i in range(20, 40):
                await engine.put(f"rw-{i}", f"new-{i}".encode(), t + 1)

        # Run readers and writers concurrently
        await asyncio.gather(
            reader(),
            reader(),
            writer(),
        )

        # Verify new writes are visible
        for i in range(20, 40):
            result = await engine.get(f"rw-{i}")
            assert result is not None

        await engine.stop()
