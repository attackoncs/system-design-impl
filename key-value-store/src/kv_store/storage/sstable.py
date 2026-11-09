"""Immutable Sorted String Table (SSTable) for the LSM-tree storage engine.

SSTables are created by flushing a MemTable to disk or by compaction. They
contain sorted key-value pairs, a sparse index and a Bloom filter.

File format layout (unchanged since the first release):
    [data block] [index block] [bloom filter block] [footer]

- Data block: sequential entries, each entry is
    [key_len(4)][key][value_len(4)][value][timestamp(8)][is_tombstone(1)]
- Index block: sparse index with every Nth entry (every 16th), each index entry
  is [key_len(4)][key][offset(8)]
- Bloom filter block: serialized bloom filter bytes
- Footer:
    [data_block_size(8)][index_block_size(8)][bloom_filter_size(8)]
    [entry_count(4)][level(4)][min_key_len(4)][min_key]
    [max_key_len(4)][max_key][created_at(8)][magic_number(4)]

Memory model: an open table keeps only its footer metadata, the sparse index
and the Bloom filter in memory. The data block stays on disk. The 16 entries
between two sparse index offsets form one "block": a point lookup reads exactly
one block (optionally through a shared, byte-bounded LRU ``BlockCache``), and
iteration streams bounded read-ahead chunks. Writers stream entries straight
into the temporary file, so neither flush nor compaction buffers a whole table.

Covers: FR-9.4, FR-10.3
"""

import asyncio
import bisect
import collections
import dataclasses
import heapq
import itertools
import os
import pathlib
import struct
import threading
import time
import weakref
from typing import TYPE_CHECKING, Iterable, Iterator, List, Optional, Tuple

from kv_store.async_work import complete
from kv_store.storage import bloom_filter as storage_bloom_filter
from kv_store.storage.files import publishing

if TYPE_CHECKING:
    from kv_store.storage.memtable import MemTable


# Magic number to identify valid SSTable files
_MAGIC_NUMBER = 0x53535442  # "SSTB" in hex

# Sparse index interval: index every Nth entry (one block = N entries)
_INDEX_INTERVAL = 16

# Footer = 52 fixed bytes + both keys; readers accept keys up to ~64 KiB total.
_MIN_FOOTER_SIZE = 52
_MAX_FOOTER_SIZE = _MIN_FOOTER_SIZE + 65536

# Sequential scans read whole blocks, grouped up to this many bytes per read.
_SCAN_READ_BYTES = 256 * 1024

_table_ids = itertools.count()

# (key, value, timestamp, is_tombstone) as written to the data block.
Row = Tuple[str, Optional[bytes], float, bool]


@dataclasses.dataclass
class SSTableMetadata:
    """Metadata for an SSTable file."""

    file_path: str
    level: int
    min_key: str
    max_key: str
    entry_count: int
    size_bytes: int
    created_at: float


@dataclasses.dataclass
class SSTableEntry:
    """A single entry in an SSTable."""

    key: str
    value: Optional[bytes]
    timestamp: float
    is_tombstone: bool


class SSTableRetiredError(RuntimeError):
    """The table was retired by compaction; its replacement is already live."""


class BlockCache:
    """Thread-safe LRU of raw data blocks bounded by total payload bytes.

    One cache is shared by all tables of an engine, so the budget is global
    rather than per file. A capacity of 0 disables caching. Blocks larger than
    the whole budget are never cached.
    """

    def __init__(self, capacity_bytes: int):
        if type(capacity_bytes) is not int or capacity_bytes < 0:
            raise ValueError("block cache capacity must be a non-negative int")
        self.capacity_bytes = capacity_bytes
        self._blocks: "collections.OrderedDict[Tuple[int, int], bytes]" = (
            collections.OrderedDict()
        )
        self._size = 0
        self._lock = threading.Lock()

    def get(self, key: Tuple[int, int]) -> Optional[bytes]:
        with self._lock:
            block = self._blocks.get(key)
            if block is not None:
                self._blocks.move_to_end(key)
            return block

    def put(self, key: Tuple[int, int], block: bytes) -> None:
        if len(block) > self.capacity_bytes:
            return
        with self._lock:
            previous = self._blocks.pop(key, None)
            if previous is not None:
                self._size -= len(previous)
            self._blocks[key] = block
            self._size += len(block)
            while self._size > self.capacity_bytes:
                _, evicted = self._blocks.popitem(last=False)
                self._size -= len(evicted)

    def drop_table(self, table_id: int) -> None:
        """Forget every block of one (retired) table."""
        with self._lock:
            for key in [key for key in self._blocks if key[0] == table_id]:
                self._size -= len(self._blocks.pop(key))

    @property
    def size_bytes(self) -> int:
        return self._size

    def __len__(self) -> int:
        return len(self._blocks)


def _pread(descriptor: int, size: int, offset: int) -> bytes:
    """Read up to ``size`` bytes at ``offset`` without a shared file position
    on POSIX. Callers hold the table lock, which also covers the Windows
    seek+read fallback."""
    chunks = []
    while size > 0:
        if hasattr(os, "pread"):
            chunk = os.pread(descriptor, size, offset)
        else:  # pragma: no cover - Windows
            os.lseek(descriptor, offset, os.SEEK_SET)
            chunk = os.read(descriptor, size)
        if not chunk:
            break
        chunks.append(chunk)
        size -= len(chunk)
        offset += len(chunk)
    return b"".join(chunks)


def _close_descriptor(box: List[Optional[int]]) -> None:
    descriptor, box[0] = box[0], None
    if descriptor is not None:
        os.close(descriptor)


def _decode_entry(buffer: bytes, pos: int) -> Tuple[Optional[SSTableEntry], int]:
    """Decode one data entry; ``(None, pos)`` if it is truncated or invalid."""
    end = len(buffer)
    try:
        if pos + 4 > end:
            return None, pos
        key_len = struct.unpack_from("<I", buffer, pos)[0]
        pos += 4
        if pos + key_len > end:
            return None, pos
        key = buffer[pos : pos + key_len].decode("utf-8")
        pos += key_len
        if pos + 4 > end:
            return None, pos
        value_len = struct.unpack_from("<I", buffer, pos)[0]
        pos += 4
        if pos + value_len > end:
            return None, pos
        value_bytes = buffer[pos : pos + value_len]
        pos += value_len
        if pos + 9 > end:
            return None, pos
        timestamp, tombstone_flag = struct.unpack_from("<dB", buffer, pos)
        pos += 9
    except (struct.error, UnicodeDecodeError):
        return None, pos
    is_tombstone = tombstone_flag == 1
    value = None if is_tombstone else value_bytes
    return SSTableEntry(key, value, timestamp, is_tombstone), pos


def _decode_entries(buffer: bytes) -> Tuple[List[SSTableEntry], bool]:
    """Decode consecutive entries; the flag is False if decoding stopped early."""
    entries = []
    pos = 0
    while pos < len(buffer):
        entry, pos = _decode_entry(buffer, pos)
        if entry is None:
            return entries, False
        entries.append(entry)
    return entries, True


def _parse_index_block(index_data: bytes) -> Tuple[List[str], List[int]]:
    """Parse the sparse index block into parallel key/offset lists."""
    keys: List[str] = []
    offsets: List[int] = []
    pos = 0
    while pos + 4 <= len(index_data):
        key_len = struct.unpack_from("<I", index_data, pos)[0]
        pos += 4
        if pos + key_len + 8 > len(index_data):
            break
        keys.append(index_data[pos : pos + key_len].decode("utf-8"))
        pos += key_len
        offsets.append(struct.unpack_from("<Q", index_data, pos)[0])
        pos += 8
    return keys, offsets


class SSTable:
    """Immutable sorted string table stored on disk.

    Only the footer, sparse index and Bloom filter are resident. Each table
    holds one read-only descriptor; reads use positioned I/O under a per-table
    lock, so concurrent lookups and compaction iterators never share a file
    position. ``close()`` releases the descriptor (it is reopened lazily);
    ``retire()`` closes it for good before compaction removes the file.

    Covers: FR-9.4, FR-10.3
    """

    def __init__(self, file_path: str, block_cache: Optional[BlockCache] = None):
        """Open an existing SSTable file for reading.

        Loads the footer, sparse index, and bloom filter into memory.
        The data block remains on disk and is read on demand.

        Args:
            file_path: Path to the SSTable file on disk.
            block_cache: Optional shared LRU cache for point-lookup blocks.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: If the file is not a valid SSTable (bad magic number).
        """
        self._file_path = file_path
        self._block_cache = block_cache
        self._table_id = next(_table_ids)
        self._lock = threading.Lock()
        self._retired = False
        # The finalizer closes whichever descriptor is open when the table is
        # garbage collected (e.g. a retired table still referenced by a reader).
        self._descriptor: List[Optional[int]] = [None]
        weakref.finalize(self, _close_descriptor, self._descriptor)
        try:
            self._descriptor[0] = os.open(file_path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except FileNotFoundError:
            raise FileNotFoundError(f"SSTable file not found: {file_path}") from None
        try:
            self._file_size = os.fstat(self._descriptor[0]).st_size
            self._load_metadata()
        except BaseException:
            self.close()
            raise

    # ------------------------------------------------------------------ open

    def _load_metadata(self) -> None:
        """Locate and parse the footer, then load the index and Bloom filter.

        The footer starts at data_block_size + index_block_size +
        bloom_filter_size and begins with those three sizes, so every candidate
        footer length is tried until the sizes are self-consistent and the rest
        of the footer, index and Bloom filter parse. Only the file tail (at most
        the largest footer) is read for this search.
        """
        file_size = self._file_size
        if file_size < 4:
            raise ValueError("File too small to be a valid SSTable")
        tail_start = max(0, file_size - _MAX_FOOTER_SIZE)
        tail = self._read(tail_start, file_size - tail_start)
        magic = struct.unpack_from("<I", tail, len(tail) - 4)[0]
        if magic != _MAGIC_NUMBER:
            raise ValueError(
                "Invalid SSTable file: bad magic number "
                f"(expected 0x{_MAGIC_NUMBER:08X}, got 0x{magic:08X})"
            )
        for footer_size in range(_MIN_FOOTER_SIZE, len(tail) + 1):
            footer_start = file_size - footer_size
            sizes = struct.unpack_from("<QQQ", tail, footer_start - tail_start)
            if sum(sizes) != footer_start:
                continue
            try:
                self._load_footer(tail[footer_start - tail_start :], *sizes)
                return
            except (struct.error, UnicodeDecodeError, ValueError):
                continue
        raise ValueError("Could not locate valid footer in SSTable file")

    def _load_footer(
        self,
        footer: bytes,
        data_block_size: int,
        index_block_size: int,
        bloom_filter_size: int,
    ) -> None:
        pos = 24  # skip the three 8-byte size fields
        entry_count, level, min_key_len = struct.unpack_from("<III", footer, pos)
        pos += 12
        min_key = footer[pos : pos + min_key_len].decode("utf-8")
        pos += min_key_len
        max_key_len = struct.unpack_from("<I", footer, pos)[0]
        pos += 4
        max_key = footer[pos : pos + max_key_len].decode("utf-8")
        pos += max_key_len
        created_at, magic = struct.unpack_from("<dI", footer, pos)
        pos += 12
        if magic != _MAGIC_NUMBER:
            raise ValueError("Magic number mismatch in footer")
        if pos != len(footer):
            raise ValueError("Footer size mismatch")

        index_data = self._read(data_block_size, index_block_size)
        bloom_data = self._read(data_block_size + index_block_size, bloom_filter_size)
        self._bloom_filter = storage_bloom_filter.BloomFilter.deserialize(bloom_data)
        self._index_keys, self._index_offsets = _parse_index_block(index_data)
        self._data_block_size = data_block_size
        self._metadata_obj = SSTableMetadata(
            file_path=self._file_path,
            level=level,
            min_key=min_key,
            max_key=max_key,
            entry_count=entry_count,
            size_bytes=self._file_size,
            created_at=created_at,
        )

    # ------------------------------------------------------------- file I/O

    def _read(self, offset: int, size: int) -> bytes:
        with self._lock:
            descriptor = self._descriptor[0]
            if descriptor is None:
                descriptor = self._reopen()
            return _pread(descriptor, size, offset)

    def _reopen(self) -> int:
        """Reopen after close(); refuse retired or replaced files."""
        if self._retired:
            raise SSTableRetiredError(f"SSTable was retired: {self._file_path}")
        try:
            descriptor = os.open(self._file_path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except FileNotFoundError:
            raise SSTableRetiredError(f"SSTable was removed: {self._file_path}") from None
        if os.fstat(descriptor).st_size != self._file_size:
            os.close(descriptor)
            raise SSTableRetiredError(f"SSTable was replaced: {self._file_path}")
        self._descriptor[0] = descriptor
        return descriptor

    def close(self) -> None:
        """Release the file descriptor; a later read reopens the file."""
        with self._lock:
            _close_descriptor(self._descriptor)

    def retire(self) -> None:
        """Permanently close before the file is unlinked (also on Windows)."""
        with self._lock:
            self._retired = True
            _close_descriptor(self._descriptor)
        if self._block_cache is not None:
            self._block_cache.drop_table(self._table_id)

    # --------------------------------------------------------------- blocks

    def _block_count(self) -> int:
        return max(len(self._index_offsets), 1)

    def _block_start(self, block: int) -> int:
        return self._index_offsets[block] if self._index_offsets else 0

    def _block_end(self, block: int) -> int:
        if block + 1 < len(self._index_offsets):
            return self._index_offsets[block + 1]
        return self._data_block_size

    def _read_block(self, block: int) -> bytes:
        start = self._block_start(block)
        size = max(0, self._block_end(block) - start)
        if self._block_cache is None:
            return self._read(start, size)
        cache_key = (self._table_id, start)
        data = self._block_cache.get(cache_key)
        if data is None:
            data = self._read(start, size)
            self._block_cache.put(cache_key, data)
        return data

    # ------------------------------------------------------------- writing

    @classmethod
    async def create_from_memtable(
        cls,
        memtable: "MemTable",
        file_path: str,
        level: int = 0,
        block_cache: Optional[BlockCache] = None,
    ) -> "SSTable":
        """Flush a MemTable to a new, durably published SSTable file.

        Args:
            memtable: The MemTable to flush (must not change during the call).
            file_path: Destination file path.
            level: Compaction level (0 for freshly flushed).
            block_cache: Optional shared block cache for the returned table.

        Returns:
            The newly created SSTable instance.
        """
        if len(memtable) == 0:
            raise ValueError("Cannot create SSTable from empty MemTable")
        rows = (
            (entry.key, entry.value, entry.timestamp, entry.is_tombstone)
            for entry in memtable.entries_sorted()
        )
        await complete(
            asyncio.to_thread(write_sstable, file_path, rows, len(memtable), level)
        )
        return cls(file_path, block_cache)

    # ------------------------------------------------------------- reading

    async def get(self, key: str) -> Optional[SSTableEntry]:
        """Look up a key: Bloom filter, key range, sparse index, one block.

        Args:
            key: The key to look up.

        Returns:
            The SSTableEntry if found, None otherwise.

        Raises:
            SSTableRetiredError: If compaction retired this table meanwhile.
        """
        if not self._bloom_filter.might_contain(key):
            return None
        if key < self._metadata_obj.min_key or key > self._metadata_obj.max_key:
            return None
        # Largest sparse-index key <= key; the first index key is min_key.
        block = max(bisect.bisect_right(self._index_keys, key) - 1, 0)
        return await asyncio.to_thread(self._find_in_block, key, block)

    def _find_in_block(self, key: str, block: int) -> Optional[SSTableEntry]:
        """Walk one block comparing raw key bytes (UTF-8 byte order equals
        str order); only the matching entry is fully decoded."""
        data = self._read_block(block)
        target = key.encode("utf-8")
        pos = 0
        while pos + 4 <= len(data):
            key_end = pos + 4 + struct.unpack_from("<I", data, pos)[0]
            if key_end + 4 > len(data):
                return None
            candidate = data[pos + 4 : key_end]
            if candidate == target:
                return _decode_entry(data, pos)[0]
            if candidate > target:
                return None
            pos = key_end + 4 + struct.unpack_from("<I", data, key_end)[0] + 9
        return None

    def may_contain(self, key: str) -> bool:
        """Check the Bloom filter for possible key existence.

        Args:
            key: The key to check.

        Returns:
            True if the key might exist, False if definitely not.
        """
        return self._bloom_filter.might_contain(key)

    def entries(self) -> Iterator[SSTableEntry]:
        """Stream all entries in sorted key order (compaction, full scans).

        Reads whole blocks grouped into chunks of about 256 KiB, bypassing the
        block cache, so memory stays bounded by one chunk plus one block.
        Decoding stops at the first invalid entry, as it always has.
        """
        count = self._block_count()
        first = 0
        while first < count:
            start = self._block_start(first)
            last = first
            while last + 1 < count and self._block_end(last + 1) - start <= _SCAN_READ_BYTES:
                last += 1
            chunk = self._read(start, max(0, self._block_end(last) - start))
            entries, complete_chunk = _decode_entries(chunk)
            yield from entries
            if not complete_chunk:
                return
            first = last + 1

    @property
    def metadata(self) -> SSTableMetadata:
        """Get SSTable metadata.

        Returns:
            SSTableMetadata with file path, level, key range, entry count, etc.
        """
        return self._metadata_obj


class _SSTableWriter:
    """Append sorted rows to an open stream in the SSTable file format.

    Only the sparse index (every 16th key) and the Bloom filter grow with the
    table; entries are written through as they arrive.
    """

    def __init__(self, stream, expected_items: int):
        self._stream = stream
        self._bloom = storage_bloom_filter.BloomFilter(expected_items=max(expected_items, 1))
        self._index = bytearray()
        self._created_at = time.time()
        self._offset = 0
        self._count = 0
        self._min_key: Optional[str] = None
        self._last_key: Optional[str] = None

    def add(self, key: str, value: Optional[bytes], timestamp: float, is_tombstone: bool) -> None:
        if self._last_key is not None and key <= self._last_key:
            raise ValueError("SSTable keys must be written in strictly increasing order")
        key_bytes = key.encode("utf-8")
        value_bytes = value if value is not None else b""
        if self._count % _INDEX_INTERVAL == 0:
            self._index += struct.pack("<I", len(key_bytes)) + key_bytes
            self._index += struct.pack("<Q", self._offset)
        self._bloom.add(key)
        record = b"".join(
            (
                struct.pack("<I", len(key_bytes)),
                key_bytes,
                struct.pack("<I", len(value_bytes)),
                value_bytes,
                struct.pack("<dB", timestamp, 1 if is_tombstone else 0),
            )
        )
        self._stream.write(record)
        self._offset += len(record)
        self._count += 1
        if self._min_key is None:
            self._min_key = key
        self._last_key = key

    def finish(self, level: int) -> None:
        if self._count == 0:
            raise ValueError("Cannot create SSTable from empty entries")
        bloom_data = self._bloom.serialize()
        min_key = self._min_key.encode("utf-8")
        max_key = self._last_key.encode("utf-8")
        footer = b"".join(
            (
                struct.pack("<QQQ", self._offset, len(self._index), len(bloom_data)),
                struct.pack("<II", self._count, level),
                struct.pack("<I", len(min_key)),
                min_key,
                struct.pack("<I", len(max_key)),
                max_key,
                struct.pack("<dI", self._created_at, _MAGIC_NUMBER),
            )
        )
        self._stream.write(bytes(self._index))
        self._stream.write(bloom_data)
        self._stream.write(footer)


def write_sstable(
    file_path: str, rows: Iterable[Row], expected_items: int, level: int
) -> None:
    """Stream sorted rows into a durably published SSTable (blocking).

    Rows go to a temporary file which is fsynced, atomically renamed to
    ``file_path`` and followed by a directory sync. Any failure, including an
    empty input, removes the temporary file and publishes nothing.
    ``expected_items`` sizes the Bloom filter and may over-estimate.
    """
    path = pathlib.Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with publishing(path) as stream:
        writer = _SSTableWriter(stream, expected_items)
        for row in rows:
            writer.add(*row)
        writer.finish(level)


def _ranked(rank: int, entries: Iterable) -> Iterator[tuple]:
    for entry in entries:
        yield entry.key, -entry.timestamp, rank, entry


def merge_newest(sources: List[Iterable]) -> Iterator:
    """K-way merge sorted entry streams, yielding the newest entry per key.

    ``sources`` are sorted by key and listed in priority order. The entry with
    the largest timestamp wins; on equal timestamps the earlier source wins.
    Memory is one pending entry per source.
    """
    previous = object()
    for key, _, _, entry in heapq.merge(*(_ranked(rank, source) for rank, source in enumerate(sources))):
        if key != previous:
            previous = key
            yield entry
