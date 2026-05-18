"""In-memory sorted key-value table (MemTable) for the LSM-tree storage engine.

The MemTable provides fast in-memory writes and lookups. When the size threshold
is reached, the MemTable is frozen and flushed to an SSTable on disk.

A plain Python dict is used for O(1) put/get operations. The entries_sorted()
method sorts keys on demand for flushing to SSTables.

Covers: FR-9.2, FR-9.3, FR-10.1
"""

from dataclasses import dataclass
from typing import Iterator, Optional


# Approximate overhead per entry in bytes (object headers, dict slot, pointers)
_ENTRY_OVERHEAD_BYTES = 64


@dataclass
class MemTableEntry:
    """An entry stored in the MemTable."""

    key: str
    value: Optional[bytes]  # None indicates a tombstone (delete)
    timestamp: float
    is_tombstone: bool = False


class MemTable:
    """In-memory sorted key-value table.

    Provides O(1) insert and lookup using a dict. When the size threshold
    is reached, the MemTable is frozen and flushed to an SSTable on disk.
    The entries_sorted() method returns entries in sorted key order for
    sequential SSTable writes.

    Covers: FR-9.2, FR-9.3, FR-10.1
    """

    def __init__(self, size_threshold_bytes: int = 4 * 1024 * 1024):
        """Initialize MemTable with a flush size threshold.

        Args:
            size_threshold_bytes: Flush to SSTable when this size is exceeded.
        """
        self._entries: dict[str, MemTableEntry] = {}
        self._size_threshold_bytes = size_threshold_bytes
        self._size_bytes = 0

    def put(self, key: str, value: bytes, timestamp: float) -> None:
        """Insert or update a key-value pair.

        If the key already exists, the old entry's size is subtracted before
        adding the new entry's size. Only the entry with the newer timestamp
        is kept; if the incoming timestamp is older, the write is ignored.

        Args:
            key: The key string.
            value: The value bytes.
            timestamp: Write timestamp for ordering.
        """
        existing = self._entries.get(key)

        if existing is not None:
            # Only accept writes with newer or equal timestamps
            if timestamp < existing.timestamp:
                return
            # Subtract old entry size
            self._size_bytes -= self._entry_size(existing)

        entry = MemTableEntry(key=key, value=value, timestamp=timestamp, is_tombstone=False)
        self._entries[key] = entry
        self._size_bytes += self._entry_size(entry)

    def delete(self, key: str, timestamp: float) -> None:
        """Mark a key as deleted with a tombstone.

        A tombstone entry is stored with value=None and is_tombstone=True.
        This ensures that the delete propagates to SSTables during flush,
        so older versions of the key in lower-level SSTables are shadowed.

        Args:
            key: The key to delete.
            timestamp: Deletion timestamp.
        """
        existing = self._entries.get(key)

        if existing is not None:
            # Only accept deletes with newer or equal timestamps
            if timestamp < existing.timestamp:
                return
            # Subtract old entry size
            self._size_bytes -= self._entry_size(existing)

        entry = MemTableEntry(key=key, value=None, timestamp=timestamp, is_tombstone=True)
        self._entries[key] = entry
        self._size_bytes += self._entry_size(entry)

    def get(self, key: str) -> Optional[MemTableEntry]:
        """Look up a key in the MemTable.

        Args:
            key: The key to look up.

        Returns:
            The MemTableEntry if found, None otherwise.
            Note: the entry may be a tombstone (is_tombstone=True).
        """
        return self._entries.get(key)

    def is_full(self) -> bool:
        """Check if the MemTable has exceeded its size threshold.

        Returns:
            True if size_bytes >= size_threshold_bytes.
        """
        return self._size_bytes >= self._size_threshold_bytes

    @property
    def size_bytes(self) -> int:
        """Current approximate size in bytes."""
        return self._size_bytes

    def entries_sorted(self) -> Iterator[MemTableEntry]:
        """Iterate all entries in sorted key order (for flushing to SSTable).

        Returns:
            An iterator of MemTableEntry objects sorted by key.
        """
        for key in sorted(self._entries.keys()):
            yield self._entries[key]

    def clear(self) -> None:
        """Clear all entries and reset size tracking (after successful flush)."""
        self._entries.clear()
        self._size_bytes = 0

    def __len__(self) -> int:
        """Return the number of entries in the MemTable."""
        return len(self._entries)

    @staticmethod
    def _entry_size(entry: MemTableEntry) -> int:
        """Calculate the approximate size of an entry in bytes.

        Accounts for:
        - Key string (UTF-8 encoded length)
        - Value bytes (length, or 0 for tombstones)
        - Fixed overhead for object headers, dict slot, pointers, timestamp

        Args:
            entry: The MemTableEntry to measure.

        Returns:
            Approximate size in bytes.
        """
        key_size = len(entry.key.encode("utf-8"))
        value_size = len(entry.value) if entry.value is not None else 0
        return key_size + value_size + _ENTRY_OVERHEAD_BYTES
