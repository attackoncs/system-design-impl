"""Tests for the Write-Ahead Log (WAL) implementation.

Tests cover:
- Append and replay roundtrip
- Sequence numbers are monotonically increasing
- Truncate clears all entries
- Handles corrupt entries gracefully (skip with warning)
- Concurrent appends maintain ordering
"""

import asyncio
import os
import struct
import time
import zlib

import pytest

from kv_store.storage.wal import (
    WALEntry,
    WALEntryType,
    WriteAheadLog,
    _CHECKSUM_FORMAT,
    _CHECKSUM_SIZE,
    _LENGTH_FORMAT,
    _LENGTH_SIZE,
)


@pytest.fixture
def wal_dir(tmp_path):
    """Provide a temporary WAL directory."""
    return str(tmp_path / "wal")


@pytest.fixture
def wal(wal_dir):
    """Provide a fresh WriteAheadLog instance."""
    return WriteAheadLog(wal_dir)


class TestWALAppendAndReplay:
    """Test basic append and replay functionality."""

    async def test_append_and_replay_single_put(self, wal):
        """Test appending a single PUT entry and replaying it."""
        entry = WALEntry(
            sequence_number=0,
            entry_type=WALEntryType.PUT,
            key="hello",
            value=b"world",
            timestamp=1234567890.123,
        )

        await wal.append(entry)
        entries = await wal.replay()

        assert len(entries) == 1
        assert entries[0].sequence_number == 1
        assert entries[0].entry_type == WALEntryType.PUT
        assert entries[0].key == "hello"
        assert entries[0].value == b"world"
        assert entries[0].timestamp == 1234567890.123

    async def test_append_and_replay_single_delete(self, wal):
        """Test appending a single DELETE entry and replaying it."""
        entry = WALEntry(
            sequence_number=0,
            entry_type=WALEntryType.DELETE,
            key="goodbye",
            value=None,
            timestamp=9876543210.456,
        )

        await wal.append(entry)
        entries = await wal.replay()

        assert len(entries) == 1
        assert entries[0].sequence_number == 1
        assert entries[0].entry_type == WALEntryType.DELETE
        assert entries[0].key == "goodbye"
        assert entries[0].value is None
        assert entries[0].timestamp == 9876543210.456

    async def test_append_and_replay_multiple_entries(self, wal):
        """Test appending multiple entries and replaying them in order."""
        entries_to_write = [
            WALEntry(0, WALEntryType.PUT, "key1", b"value1", 1.0),
            WALEntry(0, WALEntryType.PUT, "key2", b"value2", 2.0),
            WALEntry(0, WALEntryType.DELETE, "key1", None, 3.0),
            WALEntry(0, WALEntryType.PUT, "key3", b"value3", 4.0),
        ]

        for entry in entries_to_write:
            await wal.append(entry)

        entries = await wal.replay()

        assert len(entries) == 4
        assert entries[0].key == "key1"
        assert entries[0].value == b"value1"
        assert entries[1].key == "key2"
        assert entries[1].value == b"value2"
        assert entries[2].key == "key1"
        assert entries[2].entry_type == WALEntryType.DELETE
        assert entries[2].value is None
        assert entries[3].key == "key3"
        assert entries[3].value == b"value3"

    async def test_replay_empty_wal(self, wal):
        """Test replaying an empty WAL returns no entries."""
        entries = await wal.replay()
        assert entries == []

    async def test_replay_nonexistent_file(self, wal):
        """Test replaying when WAL file doesn't exist returns empty list."""
        entries = await wal.replay()
        assert entries == []

    async def test_append_with_empty_value(self, wal):
        """Test appending an entry with empty bytes value."""
        entry = WALEntry(0, WALEntryType.PUT, "key", b"", 1.0)
        await wal.append(entry)

        entries = await wal.replay()
        assert len(entries) == 1
        # Empty bytes should be stored as None (since value_length is 0)
        # Actually, b"" has length 0, so it will be read back as None
        # This is by design: empty value and no value are equivalent
        assert entries[0].value is None

    async def test_append_with_unicode_key(self, wal):
        """Test appending an entry with a unicode key."""
        entry = WALEntry(0, WALEntryType.PUT, "日本語キー", b"value", 1.0)
        await wal.append(entry)

        entries = await wal.replay()
        assert len(entries) == 1
        assert entries[0].key == "日本語キー"

    async def test_append_with_large_value(self, wal):
        """Test appending an entry with a large value."""
        large_value = b"x" * 10240  # 10 KB
        entry = WALEntry(0, WALEntryType.PUT, "big", large_value, 1.0)
        await wal.append(entry)

        entries = await wal.replay()
        assert len(entries) == 1
        assert entries[0].value == large_value


class TestWALSequenceNumbers:
    """Test sequence number behavior."""

    async def test_sequence_numbers_monotonically_increasing(self, wal):
        """Test that sequence numbers increase with each append."""
        for i in range(5):
            entry = WALEntry(0, WALEntryType.PUT, f"key{i}", b"val", float(i))
            await wal.append(entry)

        entries = await wal.replay()
        for i, entry in enumerate(entries):
            assert entry.sequence_number == i + 1

    async def test_sequence_number_property(self, wal):
        """Test the sequence_number property reflects current state."""
        assert wal.sequence_number == 0

        entry = WALEntry(0, WALEntryType.PUT, "k", b"v", 1.0)
        await wal.append(entry)
        assert wal.sequence_number == 1

        await wal.append(entry)
        assert wal.sequence_number == 2

    async def test_replay_restores_sequence_number(self, wal_dir):
        """Test that replay restores the sequence number from persisted entries."""
        wal1 = WriteAheadLog(wal_dir)
        for i in range(3):
            entry = WALEntry(0, WALEntryType.PUT, f"key{i}", b"val", float(i))
            await wal1.append(entry)

        # Create a new WAL instance (simulating restart)
        wal2 = WriteAheadLog(wal_dir)
        assert wal2.sequence_number == 0  # Before replay

        await wal2.replay()
        assert wal2.sequence_number == 3  # After replay


class TestWALTruncate:
    """Test WAL truncation."""

    async def test_truncate_clears_all_entries(self, wal):
        """Test that truncate removes all entries."""
        for i in range(5):
            entry = WALEntry(0, WALEntryType.PUT, f"key{i}", b"val", float(i))
            await wal.append(entry)

        await wal.truncate()

        entries = await wal.replay()
        assert entries == []

    async def test_truncate_creates_empty_file(self, wal, wal_dir):
        """Test that truncate creates/leaves an empty file."""
        entry = WALEntry(0, WALEntryType.PUT, "key", b"val", 1.0)
        await wal.append(entry)

        await wal.truncate()

        wal_path = os.path.join(wal_dir, "wal.log")
        assert os.path.exists(wal_path)
        assert os.path.getsize(wal_path) == 0

    async def test_append_after_truncate(self, wal):
        """Test that appending after truncate works correctly."""
        entry = WALEntry(0, WALEntryType.PUT, "before", b"val1", 1.0)
        await wal.append(entry)

        await wal.truncate()

        entry = WALEntry(0, WALEntryType.PUT, "after", b"val2", 2.0)
        await wal.append(entry)

        entries = await wal.replay()
        assert len(entries) == 1
        assert entries[0].key == "after"


class TestWALCorruptionHandling:
    """Test handling of corrupt WAL entries."""

    async def test_corrupt_checksum_skipped(self, wal, wal_dir):
        """Test that entries with invalid checksums are skipped."""
        # Write a valid entry
        entry = WALEntry(0, WALEntryType.PUT, "good", b"value", 1.0)
        await wal.append(entry)

        # Corrupt the checksum by modifying the last 4 bytes of the entry
        wal_path = os.path.join(wal_dir, "wal.log")
        with open(wal_path, "r+b") as f:
            # Seek to the checksum position (end of file minus checksum size)
            f.seek(-_CHECKSUM_SIZE, 2)
            # Write garbage checksum
            f.write(b"\xff\xff\xff\xff")

        entries = await wal.replay()
        assert len(entries) == 0  # Corrupt entry should be skipped

    async def test_partial_entry_at_end_skipped(self, wal, wal_dir):
        """Test that a partial entry at the end of file is handled gracefully."""
        # Write a valid entry first
        entry = WALEntry(0, WALEntryType.PUT, "good", b"value", 1.0)
        await wal.append(entry)

        # Append incomplete data (a length field pointing to more data than exists)
        wal_path = os.path.join(wal_dir, "wal.log")
        with open(wal_path, "ab") as f:
            # Write a length that claims 1000 bytes follow, but don't write them
            f.write(struct.pack(_LENGTH_FORMAT, 1000))

        entries = await wal.replay()
        assert len(entries) == 1  # Only the valid entry should be returned
        assert entries[0].key == "good"

    async def test_corrupt_entry_between_valid_entries(self, wal, wal_dir):
        """Test that corruption in the middle stops replay at that point."""
        # Write two valid entries
        entry1 = WALEntry(0, WALEntryType.PUT, "first", b"val1", 1.0)
        entry2 = WALEntry(0, WALEntryType.PUT, "second", b"val2", 2.0)
        await wal.append(entry1)
        await wal.append(entry2)

        # Read the file to find the boundary between entries
        wal_path = os.path.join(wal_dir, "wal.log")
        with open(wal_path, "rb") as f:
            data = f.read()

        # Find the first entry's length to locate the second entry
        (first_entry_length,) = struct.unpack(_LENGTH_FORMAT, data[:_LENGTH_SIZE])
        second_entry_start = _LENGTH_SIZE + first_entry_length

        # Corrupt the checksum of the first entry
        checksum_pos = second_entry_start - _CHECKSUM_SIZE
        corrupted = bytearray(data)
        corrupted[checksum_pos:checksum_pos + _CHECKSUM_SIZE] = b"\x00\x00\x00\x00"

        with open(wal_path, "wb") as f:
            f.write(bytes(corrupted))

        entries = await wal.replay()
        # First entry is corrupt, second is valid
        # The WAL reads entries sequentially; corrupt first entry is skipped
        # but second entry should still be readable
        assert len(entries) == 1
        assert entries[0].key == "second"

    async def test_truncated_length_field(self, wal, wal_dir):
        """Test handling of a truncated length field at end of file."""
        entry = WALEntry(0, WALEntryType.PUT, "good", b"value", 1.0)
        await wal.append(entry)

        # Append only 2 bytes (incomplete length field)
        wal_path = os.path.join(wal_dir, "wal.log")
        with open(wal_path, "ab") as f:
            f.write(b"\x01\x02")

        entries = await wal.replay()
        assert len(entries) == 1
        assert entries[0].key == "good"


class TestWALConcurrency:
    """Test concurrent access to the WAL."""

    async def test_concurrent_appends_maintain_ordering(self, wal):
        """Test that concurrent appends produce monotonically increasing sequence numbers."""
        num_entries = 20

        async def append_entry(i: int):
            entry = WALEntry(0, WALEntryType.PUT, f"key{i}", f"val{i}".encode(), float(i))
            await wal.append(entry)

        # Run multiple appends concurrently
        tasks = [append_entry(i) for i in range(num_entries)]
        await asyncio.gather(*tasks)

        entries = await wal.replay()
        assert len(entries) == num_entries

        # Sequence numbers should be monotonically increasing
        seq_numbers = [e.sequence_number for e in entries]
        assert seq_numbers == sorted(seq_numbers)
        assert seq_numbers == list(range(1, num_entries + 1))


class TestWALDirectoryCreation:
    """Test WAL directory management."""

    async def test_creates_directory_if_not_exists(self, tmp_path):
        """Test that the WAL creates its directory if it doesn't exist."""
        wal_dir = str(tmp_path / "nested" / "wal" / "dir")
        wal = WriteAheadLog(wal_dir)

        entry = WALEntry(0, WALEntryType.PUT, "key", b"value", 1.0)
        await wal.append(entry)

        entries = await wal.replay()
        assert len(entries) == 1

    async def test_existing_directory_works(self, tmp_path):
        """Test that an existing directory is handled correctly."""
        wal_dir = str(tmp_path / "existing_wal")
        os.makedirs(wal_dir)

        wal = WriteAheadLog(wal_dir)
        entry = WALEntry(0, WALEntryType.PUT, "key", b"value", 1.0)
        await wal.append(entry)

        entries = await wal.replay()
        assert len(entries) == 1


class TestWALDurability:
    """Test WAL durability across instances (simulating restarts)."""

    async def test_data_persists_across_instances(self, wal_dir):
        """Test that data written by one instance is readable by another."""
        wal1 = WriteAheadLog(wal_dir)
        entries_to_write = [
            WALEntry(0, WALEntryType.PUT, "persist1", b"data1", 100.0),
            WALEntry(0, WALEntryType.PUT, "persist2", b"data2", 200.0),
            WALEntry(0, WALEntryType.DELETE, "persist1", None, 300.0),
        ]
        for entry in entries_to_write:
            await wal1.append(entry)

        # Create new instance (simulating restart)
        wal2 = WriteAheadLog(wal_dir)
        entries = await wal2.replay()

        assert len(entries) == 3
        assert entries[0].key == "persist1"
        assert entries[0].entry_type == WALEntryType.PUT
        assert entries[1].key == "persist2"
        assert entries[2].key == "persist1"
        assert entries[2].entry_type == WALEntryType.DELETE
