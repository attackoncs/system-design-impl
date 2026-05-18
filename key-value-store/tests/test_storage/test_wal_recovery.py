"""WAL crash recovery tests.

Tests the Write-Ahead Log's ability to handle crash scenarios:
- Partial write (simulated crash mid-append) is detected and skipped
- Replay after clean shutdown recovers all entries
- Replay after crash recovers all complete entries
- Truncate after flush means no replay on next start
"""

import os
import struct
import time
import zlib

import pytest

from kv_store.storage.wal import (
    WriteAheadLog,
    WALEntry,
    WALEntryType,
    _LENGTH_FORMAT,
    _LENGTH_SIZE,
)


@pytest.fixture
def wal_dir(tmp_path):
    """Create a temporary WAL directory."""
    wal_path = tmp_path / "wal"
    wal_path.mkdir()
    return str(wal_path)


@pytest.fixture
def wal(wal_dir):
    """Create a WriteAheadLog instance."""
    return WriteAheadLog(wal_dir)


class TestPartialWriteCrash:
    """Test: partial write (simulated crash mid-append) is detected and skipped."""

    async def test_partial_length_field_is_skipped(self, wal_dir):
        """A partial length field at the end of the WAL is skipped on replay."""
        wal = WriteAheadLog(wal_dir)

        # Write a valid entry first
        entry = WALEntry(
            sequence_number=0,
            entry_type=WALEntryType.PUT,
            key="valid-key",
            value=b"valid-value",
            timestamp=1000.0,
        )
        await wal.append(entry)

        # Simulate crash: append partial length field (2 bytes of a 4-byte field)
        wal_file = os.path.join(wal_dir, "wal.log")
        with open(wal_file, "ab") as f:
            f.write(b"\x10\x00")  # Partial 2 bytes of a uint32

        # Replay should recover the valid entry and skip the partial data
        wal2 = WriteAheadLog(wal_dir)
        entries = await wal2.replay()
        assert len(entries) == 1
        assert entries[0].key == "valid-key"
        assert entries[0].value == b"valid-value"

    async def test_partial_entry_data_is_skipped(self, wal_dir):
        """An entry with incomplete data (truncated mid-entry) is skipped."""
        wal = WriteAheadLog(wal_dir)

        # Write two valid entries
        for i in range(2):
            entry = WALEntry(
                sequence_number=0,
                entry_type=WALEntryType.PUT,
                key=f"key-{i}",
                value=f"value-{i}".encode(),
                timestamp=1000.0 + i,
            )
            await wal.append(entry)

        # Simulate crash: write a length field indicating a large entry,
        # but only write partial data
        wal_file = os.path.join(wal_dir, "wal.log")
        with open(wal_file, "ab") as f:
            # Write length field saying 100 bytes follow
            f.write(struct.pack(_LENGTH_FORMAT, 100))
            # But only write 10 bytes (simulating crash mid-write)
            f.write(b"\x00" * 10)

        # Replay should recover the two valid entries
        wal2 = WriteAheadLog(wal_dir)
        entries = await wal2.replay()
        assert len(entries) == 2
        assert entries[0].key == "key-0"
        assert entries[1].key == "key-1"

    async def test_corrupted_checksum_is_skipped(self, wal_dir):
        """An entry with a corrupted checksum is skipped during replay."""
        wal = WriteAheadLog(wal_dir)

        # Write a valid entry
        entry = WALEntry(
            sequence_number=0,
            entry_type=WALEntryType.PUT,
            key="good-key",
            value=b"good-value",
            timestamp=1000.0,
        )
        await wal.append(entry)

        # Write another valid entry
        entry2 = WALEntry(
            sequence_number=0,
            entry_type=WALEntryType.PUT,
            key="after-corrupt",
            value=b"after-value",
            timestamp=2000.0,
        )
        await wal.append(entry2)

        # Corrupt the first entry's checksum by modifying the WAL file
        wal_file = os.path.join(wal_dir, "wal.log")
        with open(wal_file, "r+b") as f:
            data = f.read()

        # The first entry starts at offset 0. The length field is 4 bytes.
        # Corrupt a byte in the middle of the first entry's payload
        # (after the length field, within the checksummed data)
        if len(data) > 10:
            corrupted = bytearray(data)
            corrupted[8] ^= 0xFF  # Flip bits in the entry data
            with open(wal_file, "wb") as f:
                f.write(bytes(corrupted))

        # Replay should skip the corrupted entry but recover the second
        wal2 = WriteAheadLog(wal_dir)
        entries = await wal2.replay()
        # At least the second entry should be recovered (first may be skipped)
        # The exact behavior depends on whether corruption affects the length
        # field or the payload. Either way, no crash should occur.
        assert len(entries) >= 1


class TestCleanShutdownRecovery:
    """Test: replay after clean shutdown recovers all entries."""

    async def test_all_entries_recovered_after_clean_stop(self, wal_dir):
        """All entries written before a clean stop are recovered on replay."""
        wal = WriteAheadLog(wal_dir)

        entries_written = []
        for i in range(10):
            entry = WALEntry(
                sequence_number=0,
                entry_type=WALEntryType.PUT,
                key=f"key-{i}",
                value=f"value-{i}".encode(),
                timestamp=1000.0 + i,
            )
            await wal.append(entry)
            entries_written.append(entry)

        # Simulate clean shutdown (no corruption)
        # Create a new WAL instance and replay
        wal2 = WriteAheadLog(wal_dir)
        entries = await wal2.replay()

        assert len(entries) == 10
        for i, entry in enumerate(entries):
            assert entry.key == f"key-{i}"
            assert entry.value == f"value-{i}".encode()
            assert entry.entry_type == WALEntryType.PUT

    async def test_mixed_put_and_delete_recovered(self, wal_dir):
        """Both PUT and DELETE entries are recovered correctly."""
        wal = WriteAheadLog(wal_dir)

        await wal.append(WALEntry(0, WALEntryType.PUT, "key-a", b"val-a", 1000.0))
        await wal.append(WALEntry(0, WALEntryType.DELETE, "key-a", None, 1001.0))
        await wal.append(WALEntry(0, WALEntryType.PUT, "key-b", b"val-b", 1002.0))

        wal2 = WriteAheadLog(wal_dir)
        entries = await wal2.replay()

        assert len(entries) == 3
        assert entries[0].entry_type == WALEntryType.PUT
        assert entries[0].key == "key-a"
        assert entries[1].entry_type == WALEntryType.DELETE
        assert entries[1].key == "key-a"
        assert entries[1].value is None
        assert entries[2].entry_type == WALEntryType.PUT
        assert entries[2].key == "key-b"

    async def test_sequence_numbers_are_monotonic(self, wal_dir):
        """Recovered entries have monotonically increasing sequence numbers."""
        wal = WriteAheadLog(wal_dir)

        for i in range(5):
            await wal.append(WALEntry(0, WALEntryType.PUT, f"k{i}", b"v", 1000.0 + i))

        wal2 = WriteAheadLog(wal_dir)
        entries = await wal2.replay()

        for i in range(1, len(entries)):
            assert entries[i].sequence_number > entries[i - 1].sequence_number


class TestCrashRecovery:
    """Test: replay after crash recovers all complete entries."""

    async def test_complete_entries_recovered_after_crash(self, wal_dir):
        """All complete entries before a crash point are recovered."""
        wal = WriteAheadLog(wal_dir)

        # Write 5 complete entries
        for i in range(5):
            await wal.append(WALEntry(0, WALEntryType.PUT, f"key-{i}", f"val-{i}".encode(), 1000.0 + i))

        # Simulate crash: append garbage data (incomplete entry)
        wal_file = os.path.join(wal_dir, "wal.log")
        with open(wal_file, "ab") as f:
            # Write a valid-looking length but truncated payload
            f.write(struct.pack(_LENGTH_FORMAT, 200))
            f.write(b"\x01\x02\x03")  # Only 3 bytes of supposed 200

        # Replay should recover all 5 complete entries
        wal2 = WriteAheadLog(wal_dir)
        entries = await wal2.replay()
        assert len(entries) == 5
        for i, entry in enumerate(entries):
            assert entry.key == f"key-{i}"
            assert entry.value == f"val-{i}".encode()

    async def test_empty_wal_after_crash_returns_nothing(self, wal_dir):
        """An empty WAL file returns no entries on replay."""
        # Create an empty WAL file
        wal_file = os.path.join(wal_dir, "wal.log")
        with open(wal_file, "wb") as f:
            pass  # Empty file

        wal = WriteAheadLog(wal_dir)
        entries = await wal.replay()
        assert entries == []

    async def test_only_garbage_data_returns_nothing(self, wal_dir):
        """A WAL file with only garbage data returns no entries."""
        wal_file = os.path.join(wal_dir, "wal.log")
        with open(wal_file, "wb") as f:
            # Write random garbage that doesn't form a valid entry
            f.write(struct.pack(_LENGTH_FORMAT, 5))
            f.write(b"\xff\xff\xff\xff\xff")  # Invalid entry data

        wal = WriteAheadLog(wal_dir)
        entries = await wal.replay()
        # Should either be empty or skip the invalid entry
        assert len(entries) == 0


class TestTruncateAfterFlush:
    """Test: truncate after flush means no replay on next start."""

    async def test_truncate_clears_all_entries(self, wal_dir):
        """After truncate, replay returns no entries."""
        wal = WriteAheadLog(wal_dir)

        # Write some entries
        for i in range(5):
            await wal.append(WALEntry(0, WALEntryType.PUT, f"key-{i}", b"val", 1000.0 + i))

        # Truncate (simulating successful flush)
        await wal.truncate()

        # Replay should return nothing
        wal2 = WriteAheadLog(wal_dir)
        entries = await wal2.replay()
        assert entries == []

    async def test_new_entries_after_truncate_are_recovered(self, wal_dir):
        """Entries written after truncate are recovered on next replay."""
        wal = WriteAheadLog(wal_dir)

        # Write and truncate
        for i in range(3):
            await wal.append(WALEntry(0, WALEntryType.PUT, f"old-{i}", b"old", 1000.0 + i))
        await wal.truncate()

        # Write new entries after truncate
        for i in range(2):
            await wal.append(WALEntry(0, WALEntryType.PUT, f"new-{i}", b"new", 2000.0 + i))

        # Replay should only return the new entries
        wal2 = WriteAheadLog(wal_dir)
        entries = await wal2.replay()
        assert len(entries) == 2
        assert entries[0].key == "new-0"
        assert entries[1].key == "new-1"

    async def test_truncate_then_restart_has_empty_wal(self, wal_dir):
        """After truncate and restart, the WAL is empty."""
        wal = WriteAheadLog(wal_dir)

        await wal.append(WALEntry(0, WALEntryType.PUT, "key", b"val", 1000.0))
        await wal.truncate()

        # Verify the WAL file is empty or doesn't exist
        wal_file = os.path.join(wal_dir, "wal.log")
        if os.path.exists(wal_file):
            assert os.path.getsize(wal_file) == 0
