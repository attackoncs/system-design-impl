"""Write-Ahead Log (WAL) implementation for crash recovery.

All writes are persisted to the WAL before being applied to the MemTable.
On recovery, the WAL is replayed to reconstruct the MemTable state.
The WAL is truncated after a successful MemTable flush to SSTable.

Binary format for each entry:
    [length: 4 bytes, uint32] - total length of entry excluding this field
    [sequence_number: 8 bytes, uint64]
    [entry_type: 1 byte] - 0=PUT, 1=DELETE
    [key_length: 4 bytes, uint32]
    [key: variable, UTF-8 encoded]
    [value_length: 4 bytes, uint32] - 0 for DELETE
    [value: variable bytes] - empty for DELETE
    [timestamp: 8 bytes, double]
    [checksum: 4 bytes, CRC32]

Covers: FR-9.1, NFR-2.1, NFR-2.2, NFR-2.3
"""

import asyncio
import logging
import os
import struct
import zlib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# WAL file name
_WAL_FILENAME = "wal.log"

# Binary format constants
_LENGTH_FORMAT = "<I"  # uint32 for entry length
_LENGTH_SIZE = struct.calcsize(_LENGTH_FORMAT)

_HEADER_FORMAT = "<QB"  # uint64 sequence_number + uint8 entry_type
_HEADER_SIZE = struct.calcsize(_HEADER_FORMAT)

_KEY_LENGTH_FORMAT = "<I"  # uint32 for key length
_KEY_LENGTH_SIZE = struct.calcsize(_KEY_LENGTH_FORMAT)

_VALUE_LENGTH_FORMAT = "<I"  # uint32 for value length
_VALUE_LENGTH_SIZE = struct.calcsize(_VALUE_LENGTH_FORMAT)

_TIMESTAMP_FORMAT = "<d"  # double for timestamp
_TIMESTAMP_SIZE = struct.calcsize(_TIMESTAMP_FORMAT)

_CHECKSUM_FORMAT = "<I"  # uint32 for CRC32 checksum
_CHECKSUM_SIZE = struct.calcsize(_CHECKSUM_FORMAT)


class WALEntryType(Enum):
    """Type of WAL entry."""
    PUT = "PUT"
    DELETE = "DELETE"


# Mapping between entry type and byte representation
_ENTRY_TYPE_TO_BYTE = {
    WALEntryType.PUT: 0,
    WALEntryType.DELETE: 1,
}

_BYTE_TO_ENTRY_TYPE = {v: k for k, v in _ENTRY_TYPE_TO_BYTE.items()}


@dataclass
class WALEntry:
    """A single entry in the write-ahead log."""
    sequence_number: int
    entry_type: WALEntryType
    key: str
    value: Optional[bytes]  # None for DELETE entries
    timestamp: float


class WriteAheadLog:
    """Append-only write-ahead log for crash recovery.

    All writes are persisted to the WAL before being applied to the MemTable.
    On recovery, the WAL is replayed to reconstruct the MemTable state.
    The WAL is truncated after a successful MemTable flush to SSTable.

    Covers: FR-9.1, NFR-2.1, NFR-2.2, NFR-2.3
    """

    def __init__(self, wal_dir: str):
        """Initialize WAL with the given directory path.

        Args:
            wal_dir: Directory to store WAL segment files.
        """
        self._wal_dir = Path(wal_dir)
        self._wal_path = self._wal_dir / _WAL_FILENAME
        self._sequence_number = 0
        self._lock = asyncio.Lock()
        self._fd: Optional[int] = None

        # Ensure the WAL directory exists
        self._wal_dir.mkdir(parents=True, exist_ok=True)

    @property
    def sequence_number(self) -> int:
        """Current sequence number (monotonically increasing)."""
        return self._sequence_number

    async def append(self, entry: WALEntry) -> None:
        """Append an entry to the WAL and fsync to disk.

        The entry is serialized to the binary format and written atomically.
        The sequence number is assigned internally and set on the entry.

        Args:
            entry: The WAL entry to persist.

        Raises:
            IOError: If the write or fsync fails.
        """
        async with self._lock:
            self._sequence_number += 1
            entry.sequence_number = self._sequence_number

            data = self._serialize_entry(entry)

            # Use asyncio.to_thread for file I/O to avoid blocking the event loop.
            # The lock ensures serialization so concurrent appends don't interleave.
            await asyncio.to_thread(self._write_and_sync, data)


    async def replay(self) -> list[WALEntry]:
        """Replay all entries from the current WAL segment.

        Used during node startup to recover MemTable state.
        Entries with invalid checksums are skipped with a warning.

        Returns:
            List of WAL entries in sequence order.
        """
        if not self._wal_path.exists():
            return []

        entries = await asyncio.to_thread(self._read_all_entries)

        # Update sequence number to the highest seen
        if entries:
            self._sequence_number = max(e.sequence_number for e in entries)

        return entries

    async def truncate(self) -> None:
        """Truncate the WAL after successful MemTable flush.

        Creates a new empty segment file, effectively discarding all entries.
        """
        async with self._lock:
            await asyncio.to_thread(self._truncate_file)

    def _serialize_entry(self, entry: WALEntry) -> bytes:
        """Serialize a WAL entry to binary format.

        Format:
            [length][sequence_number][entry_type][key_length][key]
            [value_length][value][timestamp][checksum]

        The checksum covers everything from sequence_number to timestamp (inclusive).
        The length field stores the total size of everything after it.
        """
        key_bytes = entry.key.encode("utf-8")
        value_bytes = entry.value if entry.value is not None else b""

        entry_type_byte = _ENTRY_TYPE_TO_BYTE[entry.entry_type]

        # Build the payload (everything between length and checksum, inclusive of checksum)
        # First build the checksummed portion (sequence_number through timestamp)
        checksummed_data = struct.pack(_HEADER_FORMAT, entry.sequence_number, entry_type_byte)
        checksummed_data += struct.pack(_KEY_LENGTH_FORMAT, len(key_bytes))
        checksummed_data += key_bytes
        checksummed_data += struct.pack(_VALUE_LENGTH_FORMAT, len(value_bytes))
        checksummed_data += value_bytes
        checksummed_data += struct.pack(_TIMESTAMP_FORMAT, entry.timestamp)

        # Compute CRC32 checksum
        checksum = zlib.crc32(checksummed_data) & 0xFFFFFFFF
        checksum_bytes = struct.pack(_CHECKSUM_FORMAT, checksum)

        # Total entry payload (after the length field)
        payload = checksummed_data + checksum_bytes

        # Prepend the length
        length_bytes = struct.pack(_LENGTH_FORMAT, len(payload))

        return length_bytes + payload

    def _write_and_sync(self, data: bytes) -> None:
        """Write data to the WAL file and fsync for durability.

        This runs in a thread to avoid blocking the event loop.
        Uses a persistent file descriptor for consistent writes.
        Since appends are serialized by the asyncio lock, we open in
        write mode and seek to end before each write.
        """
        if self._fd is None:
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            # On Windows, O_BINARY is required to prevent newline translation
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            self._fd = os.open(str(self._wal_path), flags, 0o644)
        # Ensure all bytes are written (os.write may do partial writes)
        view = memoryview(data)
        total_written = 0
        while total_written < len(data):
            written = os.write(self._fd, view[total_written:])
            if written == 0:
                raise IOError("os.write returned 0 bytes written")
            total_written += written
        os.fsync(self._fd)

    def _read_all_entries(self) -> list[WALEntry]:
        """Read and deserialize all valid entries from the WAL file.

        Entries with invalid checksums or incomplete data are skipped.
        """
        entries: list[WALEntry] = []

        try:
            with open(self._wal_path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return entries

        offset = 0
        total_size = len(data)

        while offset < total_size:
            # Read the length field
            if offset + _LENGTH_SIZE > total_size:
                logger.warning(
                    "WAL: incomplete length field at offset %d, stopping replay",
                    offset,
                )
                break

            (entry_length,) = struct.unpack(
                _LENGTH_FORMAT, data[offset: offset + _LENGTH_SIZE]
            )
            offset += _LENGTH_SIZE

            # Check if we have enough data for the full entry
            if offset + entry_length > total_size:
                logger.warning(
                    "WAL: incomplete entry at offset %d (expected %d bytes, "
                    "have %d), stopping replay",
                    offset - _LENGTH_SIZE,
                    entry_length,
                    total_size - offset,
                )
                break

            entry_data = data[offset: offset + entry_length]
            offset += entry_length

            # Parse the entry
            entry = self._deserialize_entry(entry_data)
            if entry is not None:
                entries.append(entry)

        return entries

    def _deserialize_entry(self, entry_data: bytes) -> Optional[WALEntry]:
        """Deserialize a single WAL entry from binary data.

        Returns None if the checksum is invalid.

        Args:
            entry_data: The raw bytes of the entry (excluding the length prefix).

        Returns:
            A WALEntry if valid, None if corrupted.
        """
        # The checksummed portion is everything except the last 4 bytes (checksum)
        if len(entry_data) < _CHECKSUM_SIZE:
            logger.warning("WAL: entry too short to contain checksum")
            return None

        checksummed_data = entry_data[:-_CHECKSUM_SIZE]
        stored_checksum_bytes = entry_data[-_CHECKSUM_SIZE:]

        # Verify checksum
        computed_checksum = zlib.crc32(checksummed_data) & 0xFFFFFFFF
        (stored_checksum,) = struct.unpack(_CHECKSUM_FORMAT, stored_checksum_bytes)

        if computed_checksum != stored_checksum:
            logger.warning(
                "WAL: checksum mismatch (computed=0x%08x, stored=0x%08x), "
                "skipping entry",
                computed_checksum,
                stored_checksum,
            )
            return None

        # Parse the checksummed data
        pos = 0

        # sequence_number + entry_type
        if pos + _HEADER_SIZE > len(checksummed_data):
            logger.warning("WAL: entry too short for header")
            return None

        sequence_number, entry_type_byte = struct.unpack(
            _HEADER_FORMAT, checksummed_data[pos: pos + _HEADER_SIZE]
        )
        pos += _HEADER_SIZE

        # Validate entry type
        if entry_type_byte not in _BYTE_TO_ENTRY_TYPE:
            logger.warning("WAL: unknown entry type byte: %d", entry_type_byte)
            return None

        entry_type = _BYTE_TO_ENTRY_TYPE[entry_type_byte]

        # key_length
        if pos + _KEY_LENGTH_SIZE > len(checksummed_data):
            logger.warning("WAL: entry too short for key length")
            return None

        (key_length,) = struct.unpack(
            _KEY_LENGTH_FORMAT, checksummed_data[pos: pos + _KEY_LENGTH_SIZE]
        )
        pos += _KEY_LENGTH_SIZE

        # key
        if pos + key_length > len(checksummed_data):
            logger.warning("WAL: entry too short for key data")
            return None

        key = checksummed_data[pos: pos + key_length].decode("utf-8")
        pos += key_length

        # value_length
        if pos + _VALUE_LENGTH_SIZE > len(checksummed_data):
            logger.warning("WAL: entry too short for value length")
            return None

        (value_length,) = struct.unpack(
            _VALUE_LENGTH_FORMAT, checksummed_data[pos: pos + _VALUE_LENGTH_SIZE]
        )
        pos += _VALUE_LENGTH_SIZE

        # value
        if pos + value_length > len(checksummed_data):
            logger.warning("WAL: entry too short for value data")
            return None

        value: Optional[bytes] = None
        if value_length > 0:
            value = checksummed_data[pos: pos + value_length]
        pos += value_length

        # timestamp
        if pos + _TIMESTAMP_SIZE > len(checksummed_data):
            logger.warning("WAL: entry too short for timestamp")
            return None

        (timestamp,) = struct.unpack(
            _TIMESTAMP_FORMAT, checksummed_data[pos: pos + _TIMESTAMP_SIZE]
        )
        pos += _TIMESTAMP_SIZE

        return WALEntry(
            sequence_number=sequence_number,
            entry_type=entry_type,
            key=key,
            value=value,
            timestamp=timestamp,
        )

    def _truncate_file(self) -> None:
        """Truncate the WAL file to zero length.

        This effectively creates a new empty segment.
        Closes the existing file descriptor and opens a fresh one.
        """
        # Close existing fd if open
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

        with open(self._wal_path, "wb") as f:
            f.flush()
            os.fsync(f.fileno())
