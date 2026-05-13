"""Unit tests for MemoryStorage backend."""

import threading
import time

import pytest

from rate_limiter.storage.memory import MemoryStorage


class TestMemoryStorageGet:
    """Tests for MemoryStorage.get method."""

    def test_get_nonexistent_key(self):
        """Getting a key that was never set returns None."""
        storage = MemoryStorage()
        assert storage.get("nonexistent") is None

    def test_set_and_get(self):
        """Basic set followed by get returns the stored value."""
        storage = MemoryStorage()
        storage.set("key1", "value1")
        assert storage.get("key1") == "value1"


class TestMemoryStorageTTL:
    """Tests for TTL expiration behavior."""

    def test_set_with_ttl_not_expired(self):
        """Value is accessible before TTL expires."""
        storage = MemoryStorage()
        storage.set("key1", "value1", ttl=10)
        assert storage.get("key1") == "value1"

    def test_set_with_ttl_expired(self):
        """Value returns None after TTL has elapsed."""
        storage = MemoryStorage()
        storage.set("key1", "value1", ttl=1)
        time.sleep(1.1)
        assert storage.get("key1") is None


class TestMemoryStorageIncrement:
    """Tests for MemoryStorage.increment method."""

    def test_increment_new_key(self):
        """Incrementing a non-existent key starts from 0 and returns 1."""
        storage = MemoryStorage()
        result = storage.increment("counter")
        assert result == 1

    def test_increment_existing_key(self):
        """Incrementing an existing key increases the value correctly."""
        storage = MemoryStorage()
        storage.increment("counter")
        storage.increment("counter")
        result = storage.increment("counter")
        assert result == 3

    def test_increment_with_ttl(self):
        """Increment sets TTL on the key, which expires after the duration."""
        storage = MemoryStorage()
        storage.increment("counter", ttl=1)
        assert storage.get("counter") == "1"
        time.sleep(1.1)
        assert storage.get("counter") is None


class TestMemoryStorageThreadSafety:
    """Tests for thread safety of MemoryStorage."""

    def test_thread_safety(self):
        """Multiple threads incrementing the same key produce correct total."""
        storage = MemoryStorage()
        num_threads = 10
        increments_per_thread = 100

        def worker():
            for _ in range(increments_per_thread):
                storage.increment("shared_counter")

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        expected = num_threads * increments_per_thread
        assert storage.get("shared_counter") == str(expected)


class TestMemoryStorageExecuteAtomic:
    """Tests for MemoryStorage.execute_atomic method."""

    def test_execute_atomic(self):
        """A callable passed to execute_atomic runs under the lock."""
        storage = MemoryStorage()
        storage.set("key1", "10")

        def atomic_op(keys, args, store):
            val = int(store._get_raw(keys[0]) or "0")
            store._set_raw(keys[0], str(val + int(args[0])))
            return val + int(args[0])

        result = storage.execute_atomic(atomic_op, ["key1"], ["5"])
        assert result == 15
        assert storage.get("key1") == "15"

    def test_execute_atomic_non_callable(self):
        """Passing a non-callable to execute_atomic raises TypeError."""
        storage = MemoryStorage()
        with pytest.raises(TypeError):
            storage.execute_atomic("not_a_callable", ["key1"], ["arg1"])
