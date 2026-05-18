"""LSM-tree storage engine orchestrating WAL, MemTable, SSTables.

Write path: WAL → MemTable → (flush) → SSTable
Read path:  MemTable → Bloom filter → SSTables (newest first)

The StorageEngine is the main entry point for all local storage operations.
It coordinates the write-ahead log for durability, the in-memory MemTable
for fast writes, and immutable SSTables on disk for persistent storage.

Covers: FR-9, FR-10
"""

import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from kv_store.config import StorageConfig
from kv_store.storage.compaction import CompactionManager
from kv_store.storage.memtable import MemTable
from kv_store.storage.sstable import SSTable
from kv_store.storage.wal import WriteAheadLog, WALEntry, WALEntryType

logger = logging.getLogger(__name__)

# Validation limits
_MAX_KEY_SIZE_BYTES = 256
_MAX_VALUE_SIZE_BYTES = 10 * 1024  # 10 KB


@dataclass
class StorageResult:
    """Result of a storage operation."""

    key: str
    value: Optional[bytes]
    timestamp: float
    is_tombstone: bool
    found: bool


class StorageEngine:
    """LSM-tree storage engine orchestrating WAL, MemTable, SSTables.

    Write path: WAL → MemTable → (flush) → SSTable
    Read path:  MemTable → Bloom filter → SSTables (newest first)

    Covers: FR-9, FR-10
    """

    def __init__(self, config: StorageConfig):
        """Initialize the storage engine.

        Args:
            config: Storage configuration.
        """
        self._config = config
        self._wal = WriteAheadLog(config.wal_dir)
        self._memtable = MemTable(size_threshold_bytes=config.memtable_size_bytes)
        self._sstables: List[SSTable] = []  # Newest first
        self._compaction_manager = CompactionManager(
            sstable_dir=config.sstable_dir,
            compaction_threshold=config.compaction_threshold,
        )
        self._started = False

    async def start(self) -> None:
        """Start the storage engine — create directories, replay WAL, load SSTables.

        1. Create data directories if they don't exist.
        2. Load existing SSTables from disk (sorted newest first by created_at).
        3. Replay WAL to restore MemTable state.
        """
        # Create directories
        Path(self._config.data_dir).mkdir(parents=True, exist_ok=True)
        Path(self._config.wal_dir).mkdir(parents=True, exist_ok=True)
        Path(self._config.sstable_dir).mkdir(parents=True, exist_ok=True)

        # Load existing SSTables from disk
        self._sstables = self._load_sstables()

        # Replay WAL to restore MemTable state
        entries = await self._wal.replay()
        for entry in entries:
            if entry.entry_type == WALEntryType.PUT:
                self._memtable.put(entry.key, entry.value or b"", entry.timestamp)
            elif entry.entry_type == WALEntryType.DELETE:
                self._memtable.delete(entry.key, entry.timestamp)

        if entries:
            logger.info(
                "Replayed %d WAL entries to restore MemTable state", len(entries)
            )

        self._started = True
        logger.info(
            "Storage engine started: %d SSTables loaded, MemTable size=%d bytes",
            len(self._sstables),
            self._memtable.size_bytes,
        )

    async def stop(self) -> None:
        """Gracefully stop — flush MemTable if non-empty, close WAL."""
        if len(self._memtable) > 0:
            await self._flush_memtable()
            logger.info("Flushed MemTable to SSTable on shutdown")

        self._started = False
        logger.info("Storage engine stopped")

    async def put(self, key: str, value: bytes, timestamp: float) -> None:
        """Write a key-value pair.

        1. Validate key and value sizes.
        2. Append to WAL for durability.
        3. Insert into MemTable.
        4. If MemTable is full, flush to SSTable.

        Args:
            key: Key string (max 256 bytes UTF-8).
            value: Value bytes (max 10 KB).
            timestamp: Write timestamp.

        Raises:
            ValueError: If key or value exceeds size limits.
        """
        self._validate_key(key)
        self._validate_value(value)

        # Write to WAL first for durability
        wal_entry = WALEntry(
            sequence_number=0,  # Assigned by WAL
            entry_type=WALEntryType.PUT,
            key=key,
            value=value,
            timestamp=timestamp,
        )
        await self._wal.append(wal_entry)

        # Insert into MemTable
        self._memtable.put(key, value, timestamp)

        # Flush if MemTable is full
        if self._memtable.is_full():
            await self._flush_memtable()

    async def get(self, key: str) -> Optional[StorageResult]:
        """Read a key.

        1. Check MemTable first.
        2. If not found, check SSTables from newest to oldest.
        3. For each SSTable, check Bloom filter before doing actual lookup.

        Args:
            key: Key to look up.

        Returns:
            StorageResult if found (may be a tombstone), None if not found.
        """
        # Check MemTable first
        mem_entry = self._memtable.get(key)
        if mem_entry is not None:
            return StorageResult(
                key=mem_entry.key,
                value=mem_entry.value,
                timestamp=mem_entry.timestamp,
                is_tombstone=mem_entry.is_tombstone,
                found=True,
            )

        # Check SSTables from newest to oldest
        for sstable in self._sstables:
            # Check Bloom filter first for fast negative lookup
            if not sstable.may_contain(key):
                continue

            # Bloom filter says maybe — do actual lookup
            entry = await sstable.get(key)
            if entry is not None:
                return StorageResult(
                    key=entry.key,
                    value=entry.value,
                    timestamp=entry.timestamp,
                    is_tombstone=entry.is_tombstone,
                    found=True,
                )

        # Not found anywhere
        return None

    async def delete(self, key: str, timestamp: float) -> None:
        """Delete a key by writing a tombstone marker.

        1. Validate key size.
        2. Append DELETE entry to WAL.
        3. Insert tombstone into MemTable.
        4. If MemTable is full, flush to SSTable.

        Args:
            key: Key to delete (max 256 bytes UTF-8).
            timestamp: Deletion timestamp.

        Raises:
            ValueError: If key exceeds size limit.
        """
        self._validate_key(key)

        # Write to WAL
        wal_entry = WALEntry(
            sequence_number=0,  # Assigned by WAL
            entry_type=WALEntryType.DELETE,
            key=key,
            value=None,
            timestamp=timestamp,
        )
        await self._wal.append(wal_entry)

        # Insert tombstone into MemTable
        self._memtable.delete(key, timestamp)

        # Flush if MemTable is full
        if self._memtable.is_full():
            await self._flush_memtable()

    async def _flush_memtable(self) -> None:
        """Flush the current MemTable to a new SSTable and truncate WAL.

        1. Create SSTable from current MemTable (generate unique filename).
        2. Add new SSTable to the list (prepend, so newest is first).
        3. Clear MemTable.
        4. Truncate WAL.
        5. Call compaction manager's maybe_compact().
        6. If compaction produced a new SSTable, update the SSTable list.
        """
        if len(self._memtable) == 0:
            return

        # Generate unique SSTable filename
        sstable_filename = f"sstable_L0_{uuid.uuid4().hex[:12]}.sst"
        sstable_path = str(Path(self._config.sstable_dir) / sstable_filename)

        # Create SSTable from MemTable
        new_sstable = await SSTable.create_from_memtable(
            self._memtable, sstable_path, level=0
        )

        # Prepend to SSTable list (newest first)
        self._sstables.insert(0, new_sstable)

        # Clear MemTable
        self._memtable.clear()

        # Truncate WAL
        await self._wal.truncate()

        logger.info(
            "Flushed MemTable to SSTable: %s (%d entries)",
            sstable_path,
            new_sstable.metadata.entry_count,
        )

        # Check if compaction is needed
        compacted = await self._compaction_manager.maybe_compact(self._sstables)
        if compacted is not None:
            # Remove the compacted SSTables from our list and add the new one
            self._replace_compacted_sstables(compacted)
            logger.info(
                "Compaction completed: %s",
                self._compaction_manager.get_compaction_stats(),
            )

    def _replace_compacted_sstables(self, compacted_sstable: SSTable) -> None:
        """Replace compacted SSTables with the new merged SSTable.

        Removes SSTables at the level that was compacted (level - 1 of the
        output) and inserts the new compacted SSTable in the correct position.
        """
        stats = self._compaction_manager.get_compaction_stats()
        compacted_level = compacted_sstable.metadata.level - 1

        # Remove SSTables that were at the compacted level
        self._sstables = [
            sst for sst in self._sstables
            if sst.metadata.level != compacted_level
        ]

        # Insert the compacted SSTable maintaining newest-first order
        # The compacted SSTable is newer than any existing SSTable at its level
        inserted = False
        for i, sst in enumerate(self._sstables):
            if sst.metadata.created_at < compacted_sstable.metadata.created_at:
                self._sstables.insert(i, compacted_sstable)
                inserted = True
                break

        if not inserted:
            self._sstables.append(compacted_sstable)

    def _load_sstables(self) -> List[SSTable]:
        """Load all existing SSTable files from the sstable directory.

        Returns:
            List of SSTable instances sorted by created_at descending (newest first).
        """
        sstable_dir = Path(self._config.sstable_dir)
        if not sstable_dir.exists():
            return []

        sstables: List[SSTable] = []
        for file_path in sstable_dir.iterdir():
            if file_path.suffix == ".sst":
                try:
                    sst = SSTable(str(file_path))
                    sstables.append(sst)
                except (ValueError, FileNotFoundError) as e:
                    logger.warning(
                        "Skipping invalid SSTable file %s: %s", file_path, e
                    )

        # Sort by created_at descending (newest first)
        sstables.sort(key=lambda s: s.metadata.created_at, reverse=True)
        return sstables

    @staticmethod
    def _validate_key(key: str) -> None:
        """Validate that the key does not exceed the maximum size.

        Args:
            key: The key to validate.

        Raises:
            ValueError: If key exceeds 256 bytes when UTF-8 encoded.
        """
        key_bytes = key.encode("utf-8")
        if len(key_bytes) > _MAX_KEY_SIZE_BYTES:
            raise ValueError(
                f"Key exceeds maximum size: {len(key_bytes)} bytes "
                f"(max {_MAX_KEY_SIZE_BYTES} bytes)"
            )

    @staticmethod
    def _validate_value(value: bytes) -> None:
        """Validate that the value does not exceed the maximum size.

        Args:
            value: The value to validate.

        Raises:
            ValueError: If value exceeds 10 KB.
        """
        if len(value) > _MAX_VALUE_SIZE_BYTES:
            raise ValueError(
                f"Value exceeds maximum size: {len(value)} bytes "
                f"(max {_MAX_VALUE_SIZE_BYTES} bytes)"
            )
