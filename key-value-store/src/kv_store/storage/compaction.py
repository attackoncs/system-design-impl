"""SSTable compaction manager using size-tiered compaction strategy.

When the number of SSTables at a given level exceeds the threshold,
they are merged into a single SSTable at the next level. During merge,
duplicate keys are resolved by keeping the newest version, and expired
tombstones are removed.

Covers: FR-9.5
"""

import asyncio
import heapq
import struct
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional, Tuple, TYPE_CHECKING

from kv_store.storage.bloom_filter import BloomFilter

if TYPE_CHECKING:
    from kv_store.storage.sstable import SSTable, SSTableEntry


# Default tombstone grace period: 24 hours
_DEFAULT_TOMBSTONE_GRACE_SECONDS = 24 * 60 * 60


@dataclass
class CompactionStats:
    """Statistics from a compaction run."""

    input_sstables: int = 0
    output_sstable: int = 0
    keys_merged: int = 0
    tombstones_removed: int = 0
    bytes_before: int = 0
    bytes_after: int = 0


class CompactionManager:
    """Manages SSTable compaction using size-tiered compaction strategy.

    When the number of SSTables at a given level exceeds the threshold,
    they are merged into a single SSTable at the next level. During merge,
    duplicate keys are resolved by keeping the newest version, and expired
    tombstones are removed.

    Covers: FR-9.5
    """

    def __init__(
        self,
        sstable_dir: str,
        compaction_threshold: int = 4,
        tombstone_grace_seconds: float = _DEFAULT_TOMBSTONE_GRACE_SECONDS,
    ):
        """Initialize the compaction manager.

        Args:
            sstable_dir: Directory containing SSTable files.
            compaction_threshold: Number of SSTables at a level before compaction triggers.
            tombstone_grace_seconds: How long to keep tombstones before removal (default 24h).
        """
        self._sstable_dir = sstable_dir
        self._compaction_threshold = compaction_threshold
        self._tombstone_grace_seconds = tombstone_grace_seconds
        self._last_stats = CompactionStats()

    async def maybe_compact(self, sstables: List["SSTable"]) -> Optional["SSTable"]:
        """Check if compaction is needed and perform it if so.

        Groups SSTables by level and checks if any level exceeds the threshold.
        If so, compacts the SSTables at that level into a single SSTable at the
        next level.

        Args:
            sstables: Current list of SSTables.

        Returns:
            The new merged SSTable if compaction occurred, None otherwise.
        """
        if not sstables:
            return None

        # Group SSTables by level
        levels: dict[int, List["SSTable"]] = {}
        for sst in sstables:
            level = sst.metadata.level
            if level not in levels:
                levels[level] = []
            levels[level].append(sst)

        # Find the first level that exceeds the threshold
        for level in sorted(levels.keys()):
            if len(levels[level]) >= self._compaction_threshold:
                return await self.compact(levels[level], level)

        return None

    async def compact(self, sstables: List["SSTable"], level: int) -> "SSTable":
        """Merge multiple SSTables into one, removing duplicates and tombstones.

        Uses a k-way merge of sorted iterators. For duplicate keys, keeps
        the entry with the newest timestamp. Tombstones older than the
        configured grace period are removed.

        Args:
            sstables: SSTables to merge (should be at the same level).
            level: The current level of the input SSTables. Output goes to level + 1.

        Returns:
            The newly created merged SSTable at level + 1.
        """
        from kv_store.storage.sstable import SSTable, SSTableEntry

        # Track stats
        bytes_before = sum(sst.metadata.size_bytes for sst in sstables)
        current_time = time.time()

        # K-way merge using a heap
        # Each heap entry: (key, negative_timestamp, sstable_index, entry)
        # We use negative timestamp so that for the same key, the newest entry
        # comes first when we pop from the heap.
        merged_entries = self._k_way_merge(sstables, current_time)

        # Generate unique output file path
        output_filename = f"sstable_L{level + 1}_{uuid.uuid4().hex[:12]}.sst"
        output_path = str(Path(self._sstable_dir) / output_filename)

        # Create the output SSTable using a class method that accepts entries directly
        output_sst = await self._create_sstable_from_entries(
            merged_entries, output_path, level + 1
        )

        # Update stats
        self._last_stats = CompactionStats(
            input_sstables=len(sstables),
            output_sstable=1,
            keys_merged=output_sst.metadata.entry_count,
            tombstones_removed=self._last_tombstones_removed,
            bytes_before=bytes_before,
            bytes_after=output_sst.metadata.size_bytes,
        )

        return output_sst

    def _k_way_merge(
        self, sstables: List["SSTable"], current_time: float
    ) -> List[Tuple[str, Optional[bytes], float, bool]]:
        """Perform k-way merge of sorted SSTable iterators.

        For duplicate keys, keeps only the entry with the newest timestamp.
        Removes tombstones that have exceeded the grace period.

        Args:
            sstables: SSTables to merge.
            current_time: Current time for tombstone expiry check.

        Returns:
            List of (key, value, timestamp, is_tombstone) tuples in sorted key order.
        """
        # Build a heap of (key, -timestamp, sstable_idx, entry) tuples
        # Using -timestamp ensures newest entries come first for same key
        heap: List[Tuple[str, float, int, "SSTableEntry", Iterator["SSTableEntry"]]] = []

        iterators: List[Iterator["SSTableEntry"]] = []
        for idx, sst in enumerate(sstables):
            it = iter(sst.entries())
            iterators.append(it)
            entry = next(it, None)
            if entry is not None:
                # heap item: (key, -timestamp, sstable_idx, entry, iterator)
                heapq.heappush(heap, (entry.key, -entry.timestamp, idx, entry, it))

        # Merge entries, resolving duplicates
        merged: List[Tuple[str, Optional[bytes], float, bool]] = []
        tombstones_removed = 0
        last_key: Optional[str] = None

        while heap:
            key, neg_ts, idx, entry, it = heapq.heappop(heap)

            # Advance the iterator for this SSTable
            next_entry = next(it, None)
            if next_entry is not None:
                heapq.heappush(heap, (next_entry.key, -next_entry.timestamp, idx, next_entry, it))

            # Skip duplicate keys (keep only the first occurrence = newest timestamp)
            if key == last_key:
                continue

            last_key = key

            # Check if this is an expired tombstone
            if entry.is_tombstone:
                tombstone_age = current_time - entry.timestamp
                if tombstone_age > self._tombstone_grace_seconds:
                    tombstones_removed += 1
                    continue

            merged.append((entry.key, entry.value, entry.timestamp, entry.is_tombstone))

        self._last_tombstones_removed = tombstones_removed
        return merged

    async def _create_sstable_from_entries(
        self,
        entries: List[Tuple[str, Optional[bytes], float, bool]],
        file_path: str,
        level: int,
    ) -> "SSTable":
        """Create an SSTable directly from a list of entries.

        This is more memory-efficient than creating a temporary MemTable
        for large compactions, as it writes entries directly to the SSTable
        file format.

        Args:
            entries: List of (key, value, timestamp, is_tombstone) tuples in sorted order.
            file_path: Output file path.
            level: Compaction level for the output SSTable.

        Returns:
            The newly created SSTable.
        """
        from kv_store.storage.sstable import SSTable

        if not entries:
            # Create a minimal SSTable with a dummy entry if no entries remain
            # after tombstone removal. This shouldn't normally happen.
            raise ValueError("Cannot create SSTable from empty entries after compaction")

        # Use the same file format as SSTable.create_from_memtable
        # [data block] [index block] [bloom filter block] [footer]
        _MAGIC_NUMBER = 0x53535442
        _INDEX_INTERVAL = 16

        data_block = bytearray()
        index_entries: List[Tuple[str, int]] = []
        bloom = BloomFilter(expected_items=max(len(entries), 1))
        created_at = time.time()

        for i, (key, value, timestamp, is_tombstone) in enumerate(entries):
            # Record offset for sparse index (every Nth entry)
            if i % _INDEX_INTERVAL == 0:
                index_entries.append((key, len(data_block)))

            # Add to bloom filter
            bloom.add(key)

            # Write entry to data block:
            # [key_len(4)][key][value_len(4)][value][timestamp(8)][is_tombstone(1)]
            key_bytes = key.encode("utf-8")
            value_bytes = value if value is not None else b""

            data_block.extend(struct.pack("<I", len(key_bytes)))
            data_block.extend(key_bytes)
            data_block.extend(struct.pack("<I", len(value_bytes)))
            data_block.extend(value_bytes)
            data_block.extend(struct.pack("<d", timestamp))
            data_block.extend(struct.pack("<B", 1 if is_tombstone else 0))

        # Build index block
        index_block = bytearray()
        for key, offset in index_entries:
            key_bytes = key.encode("utf-8")
            index_block.extend(struct.pack("<I", len(key_bytes)))
            index_block.extend(key_bytes)
            index_block.extend(struct.pack("<Q", offset))

        # Serialize bloom filter
        bloom_data = bloom.serialize()

        # Build footer
        min_key = entries[0][0]
        max_key = entries[-1][0]
        min_key_bytes = min_key.encode("utf-8")
        max_key_bytes = max_key.encode("utf-8")

        footer = bytearray()
        footer.extend(struct.pack("<Q", len(data_block)))       # data_block_size
        footer.extend(struct.pack("<Q", len(index_block)))      # index_block_size
        footer.extend(struct.pack("<Q", len(bloom_data)))       # bloom_filter_size
        footer.extend(struct.pack("<I", len(entries)))           # entry_count
        footer.extend(struct.pack("<I", level))                  # level
        footer.extend(struct.pack("<I", len(min_key_bytes)))    # min_key_len
        footer.extend(min_key_bytes)                             # min_key
        footer.extend(struct.pack("<I", len(max_key_bytes)))    # max_key_len
        footer.extend(max_key_bytes)                             # max_key
        footer.extend(struct.pack("<d", created_at))            # created_at
        footer.extend(struct.pack("<I", _MAGIC_NUMBER))         # magic_number

        # Combine all blocks
        file_data = bytes(data_block) + bytes(index_block) + bytes(bloom_data) + bytes(footer)

        # Write to disk
        def _write_file() -> None:
            path = Path(file_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, "wb") as f:
                f.write(file_data)
                f.flush()

        await asyncio.to_thread(_write_file)

        # Return a new SSTable instance
        return SSTable(file_path)

    def get_compaction_stats(self) -> CompactionStats:
        """Get statistics from the last compaction run.

        Returns:
            CompactionStats with details about the last compaction operation.
        """
        return self._last_stats
