"""SSTable compaction manager using size-tiered compaction strategy.

When the number of SSTables at a given level exceeds the threshold, they are
merged into a single SSTable at the next level. During merge, duplicate keys
are resolved by keeping the newest version, and expired tombstones are removed.

The merge is streaming: a heap holds one pending entry per input table, input
tables are read in bounded chunks and the output is written straight into a
temporary file that is fsynced and atomically published. Memory is
O(inputs + read chunk) plus the output's sparse index and Bloom filter, never
O(total data).

Covers: FR-9.5
"""

import asyncio
import dataclasses
import pathlib
import time
import uuid
from typing import TYPE_CHECKING, Iterator, List, Optional

from kv_store.async_work import complete
from kv_store.storage import sstable as storage_sstable

if TYPE_CHECKING:
    from kv_store.storage.sstable import SSTable


# Default tombstone grace period: 24 hours
_DEFAULT_TOMBSTONE_GRACE_SECONDS = 24 * 60 * 60


@dataclasses.dataclass
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
        block_cache: Optional[storage_sstable.BlockCache] = None,
    ):
        """Initialize the compaction manager.

        Args:
            sstable_dir: Directory containing SSTable files.
            compaction_threshold: Number of SSTables at a level before
                compaction triggers.
            tombstone_grace_seconds: How long to keep tombstones before removal
                (default 24h).
            block_cache: Shared block cache attached to compaction outputs.
        """
        self._sstable_dir = sstable_dir
        self._compaction_threshold = compaction_threshold
        self._tombstone_grace_seconds = tombstone_grace_seconds
        self._block_cache = block_cache
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
        """Merge multiple SSTables into one, removing duplicates and
        expired tombstones.

        Streams a k-way merge of the sorted inputs. For duplicate keys the
        newest timestamp wins; on equal timestamps the table listed first wins.
        Tombstones older than the grace period are dropped. The output file is
        fully written, fsynced and atomically renamed before it is opened; the
        caller retires the inputs only afterwards.

        Args:
            sstables: SSTables to merge (should be at the same level), newest
                first.
            level: The current level of the input SSTables. Output goes to
                level + 1.

        Returns:
            The newly created merged SSTable at level + 1.
        """
        bytes_before = sum(sst.metadata.size_bytes for sst in sstables)
        current_time = time.time()
        output_filename = f"sstable_L{level + 1}_{uuid.uuid4().hex[:12]}.sst"
        output_path = str(pathlib.Path(self._sstable_dir) / output_filename)

        # The whole merge is blocking file I/O; drain it even if cancelled so
        # the caller's lock is never released while a writer thread still runs.
        tombstones_removed = await complete(
            asyncio.to_thread(
                self._write_merged, sstables, current_time, output_path, level + 1
            )
        )
        output_sst = storage_sstable.SSTable(output_path, self._block_cache)

        self._last_stats = CompactionStats(
            input_sstables=len(sstables),
            output_sstable=1,
            keys_merged=output_sst.metadata.entry_count,
            tombstones_removed=tombstones_removed,
            bytes_before=bytes_before,
            bytes_after=output_sst.metadata.size_bytes,
        )
        return output_sst

    def _write_merged(
        self,
        sstables: List["SSTable"],
        current_time: float,
        output_path: str,
        level: int,
    ) -> int:
        """Stream the merged inputs into ``output_path``; return the number
        of expired tombstones dropped."""
        removed = 0

        def survivors() -> Iterator[storage_sstable.Row]:
            nonlocal removed
            inputs = [sst.entries() for sst in sstables]
            for entry in storage_sstable.merge_newest(inputs):
                if (
                    entry.is_tombstone
                    and current_time - entry.timestamp > self._tombstone_grace_seconds
                ):
                    removed += 1
                    continue
                yield entry.key, entry.value, entry.timestamp, entry.is_tombstone

        # Input counts over-estimate the output; that only enlarges the Bloom
        # filter slightly and avoids a second pass.
        expected = sum(sst.metadata.entry_count for sst in sstables)
        storage_sstable.write_sstable(output_path, survivors(), expected, level)
        return removed

    def get_compaction_stats(self) -> CompactionStats:
        """Get statistics from the last compaction run.

        Returns:
            CompactionStats with details about the last compaction operation.
        """
        return self._last_stats
