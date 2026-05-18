"""Immutable Sorted String Table (SSTable) for the LSM-tree storage engine.

SSTables are created by flushing a MemTable to disk. They contain sorted
key-value pairs with a sparse index for efficient binary search lookups.
Each SSTable has an associated Bloom filter for fast negative lookups.

File format layout:
    [data block] [index block] [bloom filter block] [footer]

- Data block: sequential entries, each entry is
    [key_len(4)][key][value_len(4)][value][timestamp(8)][is_tombstone(1)]
- Index block: sparse index with every Nth entry (every 16th), each index entry is
    [key_len(4)][key][offset(8)]
- Bloom filter block: serialized bloom filter bytes
- Footer:
    [data_block_size(8)][index_block_size(8)][bloom_filter_size(8)]
    [entry_count(4)][level(4)][min_key_len(4)][min_key]
    [max_key_len(4)][max_key][created_at(8)][magic_number(4)]

Covers: FR-9.4, FR-10.3
"""

import asyncio
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional, Tuple, TYPE_CHECKING

from kv_store.storage.bloom_filter import BloomFilter

if TYPE_CHECKING:
    from kv_store.storage.memtable import MemTable


# Magic number to identify valid SSTable files
_MAGIC_NUMBER = 0x53535442  # "SSTB" in hex

# Sparse index interval: index every Nth entry
_INDEX_INTERVAL = 16


@dataclass
class SSTableMetadata:
    """Metadata for an SSTable file."""

    file_path: str
    level: int
    min_key: str
    max_key: str
    entry_count: int
    size_bytes: int
    created_at: float


@dataclass
class SSTableEntry:
    """A single entry in an SSTable."""

    key: str
    value: Optional[bytes]
    timestamp: float
    is_tombstone: bool



@dataclass
class _IndexEntry:
    """Internal sparse index entry mapping a key to its offset in the data block."""

    key: str
    offset: int


class SSTable:
    """Immutable sorted string table stored on disk.

    SSTables are created by flushing a MemTable. They contain sorted
    key-value pairs with a sparse index for efficient binary search lookups.
    Each SSTable has an associated Bloom filter for fast negative lookups.

    File format:
        [data block] [index block] [bloom filter block] [footer]

    Covers: FR-9.4, FR-10.3
    """

    def __init__(self, file_path: str):
        """Open an existing SSTable file for reading.

        Loads the footer, sparse index, and bloom filter into memory.
        The data block remains on disk and is read on demand.

        Args:
            file_path: Path to the SSTable file on disk.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: If the file is not a valid SSTable (bad magic number).
        """
        self._file_path = file_path
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"SSTable file not found: {file_path}")

        self._file_size = path.stat().st_size

        # Read and parse the file structure
        with open(file_path, "rb") as f:
            data = f.read()

        self._raw_data = data
        self._metadata_obj: SSTableMetadata
        self._index: List[_IndexEntry] = []
        self._bloom_filter: BloomFilter
        self._data_block_size: int = 0

        self._load_from_bytes(data)

    def _load_from_bytes(self, data: bytes) -> None:
        """Parse the SSTable file format from raw bytes.

        Reads the footer first to determine block sizes, then loads
        the sparse index and bloom filter into memory.
        """
        # The footer has a variable size due to min_key and max_key.
        # We need to read from the end to find the magic number and parse backwards.
        # Fixed footer suffix: created_at(8) + magic_number(4) = 12 bytes
        # Before that: max_key_len(4) + max_key + min_key_len(4) + min_key
        # Before that: data_block_size(8) + index_block_size(8) + bloom_filter_size(8) + entry_count(4) + level(4)

        if len(data) < 4:
            raise ValueError("File too small to be a valid SSTable")

        # Verify magic number at the very end
        magic = struct.unpack_from("<I", data, len(data) - 4)[0]
        if magic != _MAGIC_NUMBER:
            raise ValueError(
                f"Invalid SSTable file: bad magic number "
                f"(expected 0x{_MAGIC_NUMBER:08X}, got 0x{magic:08X})"
            )

        # Parse footer from the beginning of the footer block.
        # We know the structure: the footer starts after bloom filter block.
        # Strategy: read the fixed-size prefix of the footer to get block sizes,
        # then use those to locate everything.

        # First, let's determine footer start by reading the first 32 bytes of footer.
        # The footer format is:
        #   data_block_size(8) + index_block_size(8) + bloom_filter_size(8) +
        #   entry_count(4) + level(4) +
        #   min_key_len(4) + min_key + max_key_len(4) + max_key +
        #   created_at(8) + magic_number(4)

        # We can determine footer start because:
        # total_size = data_block_size + index_block_size + bloom_filter_size + footer_size
        # And footer_size = 8+8+8+4+4 + 4+min_key_len + 4+max_key_len + 8+4
        #                 = 32 + 4 + min_key_len + 4 + max_key_len + 12
        #                 = 52 + min_key_len + max_key_len

        # Since we don't know footer size yet, we parse from the end.
        # magic_number is at offset -4
        # created_at is at offset -12
        # Before created_at is max_key (variable), preceded by max_key_len(4)
        # We need to scan backwards.

        # Alternative approach: read data_block_size from the start of the footer.
        # The footer starts at offset = data_block_size + index_block_size + bloom_filter_size.
        # But we don't know those yet...

        # Best approach: parse from end backwards.
        pos = len(data)

        # magic_number (4 bytes) - already verified
        pos -= 4

        # created_at (8 bytes)
        pos -= 8
        created_at = struct.unpack_from("<d", data, pos)[0]

        # max_key: [max_key_len(4)][max_key_bytes]
        # We need to find max_key_len. It's before max_key bytes.
        # But we don't know max_key length yet. We need to read max_key_len first.
        # The layout before created_at is: ...max_key_len(4) + max_key_bytes...
        # Actually the layout is: min_key_len(4) + min_key + max_key_len(4) + max_key + created_at(8) + magic(4)
        # So before created_at we have max_key bytes, and before that max_key_len.

        # Let's use a different strategy: find the footer start by trying to parse
        # from the beginning. We know the first 8 bytes of the file are the start
        # of the data block (the first entry). But we can also just read the footer
        # by scanning from the end.

        # Actually, let's use a cleaner approach:
        # Read the fixed header of the footer (first 32 bytes after the three blocks).
        # We need to find where the footer starts.

        # The simplest reliable approach: parse backwards from end of file.
        # End of file layout (from end):
        #   magic(4) | created_at(8) | max_key | max_key_len(4) | min_key | min_key_len(4) |
        #   level(4) | entry_count(4) | bloom_filter_size(8) | index_block_size(8) | data_block_size(8)

        # Wait - the footer is written sequentially, so let me re-read the spec:
        # Footer: [data_block_size(8)][index_block_size(8)][bloom_filter_size(8)]
        #         [entry_count(4)][level(4)][min_key_len(4)][min_key]
        #         [max_key_len(4)][max_key][created_at(8)][magic_number(4)]

        # So reading from end:
        # -4: magic_number
        # -12: created_at
        # Then before that: max_key (variable) preceded by max_key_len
        # Then before that: min_key (variable) preceded by min_key_len
        # Then: level(4), entry_count(4), bloom_filter_size(8), index_block_size(8), data_block_size(8)

        # Parse from end, we already have created_at and verified magic.
        # Now we need to find max_key. The issue is we don't know its length
        # without reading max_key_len which is BEFORE max_key.

        # Better strategy: use the data_block_size to find footer start.
        # We'll try a two-pass approach:
        # 1. Tentatively read data_block_size from multiple candidate positions
        # 2. Validate by checking if the math adds up

        # Actually the cleanest approach: since we have the whole file in memory,
        # let's try all possible footer starts. The footer fixed prefix is 32 bytes
        # (8+8+8+4+4), then variable keys, then 12 bytes (8+4).

        # Let's just try: read data_block_size from every possible offset and check
        # if data_block_size + index_block_size + bloom_filter_size + footer_size == file_size

        # Even simpler: iterate from the end to find the structure.
        # Since we wrote the file, we know the exact format. Let's just
        # try reading data_block_size from offset 0 of what we think is the footer.

        # The most robust approach for reading: try each possible footer_start
        # where footer_start = file_size - footer_size, and footer_size >= 52 (minimum).

        # Let me use a simpler strategy that works reliably:
        # Read the first 8 bytes at various candidate offsets and validate.

        # Actually, the SIMPLEST approach: since data_block_size is the first field
        # in the footer, and the footer comes after the three blocks, we have:
        # footer_start = data_block_size + index_block_size + bloom_filter_size
        # And data_block_size is stored at footer_start.
        # This is circular, but we can solve it:
        # file_size = data_block_size + index_block_size + bloom_filter_size + footer_size
        # footer_size = 32 + 4 + len(min_key_encoded) + 4 + len(max_key_encoded) + 12
        #             = 52 + len(min_key_encoded) + len(max_key_encoded)

        # Strategy: scan backwards to find the variable-length keys.
        # From the end: magic(4), created_at(8) = 12 bytes fixed suffix
        # Before that: max_key bytes (unknown length), max_key_len(4)
        # Before that: min_key bytes (unknown length), min_key_len(4)
        # Before that: level(4), entry_count(4), bloom_filter_size(8), index_block_size(8), data_block_size(8) = 32 bytes

        # So the structure before created_at (going backwards) is:
        # [max_key][max_key_len(4)][min_key][min_key_len(4)][level(4)][entry_count(4)][bloom(8)][index(8)][data(8)]

        # We can find max_key_len by noting that it's a 4-byte int right before max_key,
        # and max_key ends right before created_at. But we don't know where max_key starts.

        # The correct approach: since the footer is written sequentially and we know
        # the fixed-size fields at the start of the footer (32 bytes), we can:
        # 1. Try candidate footer_start positions
        # 2. Read data_block_size, index_block_size, bloom_filter_size from there
        # 3. Check if data_block_size + index_block_size + bloom_filter_size == candidate footer_start

        # This gives us a unique solution. Let's iterate:
        # For any valid file, footer_start = data_block_size + index_block_size + bloom_filter_size
        # And at offset footer_start, we find data_block_size stored as 8 bytes.

        # So: read 8 bytes at every possible offset, interpret as data_block_size,
        # then read next 8 as index_block_size, next 8 as bloom_filter_size,
        # check if they sum to the current offset.

        # But this is O(n). Better: just try offset candidates.
        # In practice, the footer is small relative to the file. Let's just
        # try a direct approach: the minimum footer is 52 bytes (empty keys).
        # Maximum footer is bounded by file size.

        # Most efficient: binary search or just compute.
        # footer_start is stored implicitly. Let's just scan from a reasonable position.

        # FINAL CLEAN APPROACH: We'll parse the footer by reading from the end
        # in a structured way. We know the last 12 bytes are created_at + magic.
        # Before that, we have max_key_len + max_key (but in forward order in the file).
        # The trick is that in the file, the order is:
        #   ... [max_key_len(4)][max_key_bytes][created_at(8)][magic(4)]
        # So max_key_len is at offset (file_end - 12 - len(max_key) - 4).
        # But we don't know len(max_key).

        # OK let me just use a forward-parsing approach with a candidate footer_start.
        # We'll try all offsets from (file_size - max_reasonable_footer) to file_size.
        # But actually, the simplest correct approach:

        # Read the file from the end to get the fixed suffix, then scan backwards
        # to find the key lengths.

        # Let me implement a clean forward parse once we find footer_start:
        self._parse_footer_from_end(data)

    def _parse_footer_from_end(self, data: bytes) -> None:
        """Parse the footer by finding its start position.

        Uses the property that footer_start = data_block_size + index_block_size + bloom_filter_size,
        and data_block_size is the first 8 bytes of the footer.
        """
        file_size = len(data)

        # Minimum footer size: 8+8+8+4+4+4+0+4+0+8+4 = 52 bytes (empty keys)
        # Maximum footer size is bounded; keys are typically short.
        # We'll try candidate footer starts from the minimum footer position.

        min_footer_size = 52
        max_footer_size = min(file_size, 52 + 65536)  # keys up to ~32KB each

        found = False
        for footer_size_candidate in range(min_footer_size, max_footer_size + 1):
            footer_start = file_size - footer_size_candidate
            if footer_start < 0:
                break

            # Read the first 24 bytes of the candidate footer
            if footer_start + 24 > file_size:
                continue

            data_block_size, index_block_size, bloom_filter_size = struct.unpack_from(
                "<QQQ", data, footer_start
            )

            # Check if the three block sizes sum to footer_start
            if data_block_size + index_block_size + bloom_filter_size == footer_start:
                # Validate further: parse the rest of the footer
                try:
                    self._parse_footer_at(data, footer_start, data_block_size, index_block_size, bloom_filter_size)
                    found = True
                    break
                except (struct.error, UnicodeDecodeError, ValueError):
                    continue

        if not found:
            raise ValueError("Could not locate valid footer in SSTable file")

    def _parse_footer_at(
        self,
        data: bytes,
        footer_start: int,
        data_block_size: int,
        index_block_size: int,
        bloom_filter_size: int,
    ) -> None:
        """Parse the footer at the given offset and load index + bloom filter."""
        pos = footer_start + 24  # skip the three 8-byte size fields

        # entry_count (4 bytes)
        entry_count = struct.unpack_from("<I", data, pos)[0]
        pos += 4

        # level (4 bytes)
        level = struct.unpack_from("<I", data, pos)[0]
        pos += 4

        # min_key_len (4 bytes) + min_key
        min_key_len = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        min_key = data[pos: pos + min_key_len].decode("utf-8")
        pos += min_key_len

        # max_key_len (4 bytes) + max_key
        max_key_len = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        max_key = data[pos: pos + max_key_len].decode("utf-8")
        pos += max_key_len

        # created_at (8 bytes, double)
        created_at = struct.unpack_from("<d", data, pos)[0]
        pos += 8

        # magic_number (4 bytes) - should match
        magic = struct.unpack_from("<I", data, pos)[0]
        pos += 4

        if magic != _MAGIC_NUMBER:
            raise ValueError("Magic number mismatch in footer")

        # Verify we consumed exactly the footer
        if pos != len(data):
            raise ValueError("Footer size mismatch")

        self._data_block_size = data_block_size
        self._index_block_size = index_block_size
        self._bloom_filter_size = bloom_filter_size

        # Build metadata
        self._metadata_obj = SSTableMetadata(
            file_path=self._file_path,
            level=level,
            min_key=min_key,
            max_key=max_key,
            entry_count=entry_count,
            size_bytes=self._file_size,
            created_at=created_at,
        )

        # Load sparse index from index block
        index_start = data_block_size
        index_end = index_start + index_block_size
        self._index = self._parse_index_block(data[index_start:index_end])

        # Load bloom filter from bloom filter block
        bloom_start = index_end
        bloom_end = bloom_start + bloom_filter_size
        self._bloom_filter = BloomFilter.deserialize(data[bloom_start:bloom_end])

    def _parse_index_block(self, index_data: bytes) -> List[_IndexEntry]:
        """Parse the sparse index block into a list of index entries."""
        entries: List[_IndexEntry] = []
        pos = 0
        while pos < len(index_data):
            # key_len (4 bytes)
            if pos + 4 > len(index_data):
                break
            key_len = struct.unpack_from("<I", index_data, pos)[0]
            pos += 4

            # key bytes
            if pos + key_len > len(index_data):
                break
            key = index_data[pos: pos + key_len].decode("utf-8")
            pos += key_len

            # offset (8 bytes)
            if pos + 8 > len(index_data):
                break
            offset = struct.unpack_from("<Q", index_data, pos)[0]
            pos += 8

            entries.append(_IndexEntry(key=key, offset=offset))

        return entries

    @classmethod
    async def create_from_memtable(
        cls, memtable: "MemTable", file_path: str, level: int = 0
    ) -> "SSTable":
        """Flush a MemTable to a new SSTable file on disk.

        Writes entries in sorted key order with a sparse index (every 16th entry)
        and a Bloom filter for fast negative lookups.

        Args:
            memtable: The MemTable to flush.
            file_path: Destination file path.
            level: Compaction level (0 for freshly flushed).

        Returns:
            The newly created SSTable instance.
        """
        # Collect sorted entries
        entries: List[Tuple[str, Optional[bytes], float, bool]] = []
        for mem_entry in memtable.entries_sorted():
            entries.append((
                mem_entry.key,
                mem_entry.value,
                mem_entry.timestamp,
                mem_entry.is_tombstone,
            ))

        if not entries:
            raise ValueError("Cannot create SSTable from empty MemTable")

        # Build the data block, sparse index, and bloom filter
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

        # Write to disk using asyncio.to_thread
        def _write_file() -> None:
            path = Path(file_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, "wb") as f:
                f.write(file_data)
                f.flush()

        await asyncio.to_thread(_write_file)

        # Return a new SSTable instance by reading the file back
        return cls(file_path)

    async def get(self, key: str) -> Optional[SSTableEntry]:
        """Look up a key using the Bloom filter and sparse index with binary search.

        1. Check bloom filter - if negative, key definitely not present
        2. Use sparse index to find the range of data block to scan
        3. Scan the data block range for the exact key

        Args:
            key: The key to look up.

        Returns:
            The SSTableEntry if found, None otherwise.
        """
        # Fast path: bloom filter check
        if not self._bloom_filter.might_contain(key):
            return None

        # Key range check
        if key < self._metadata_obj.min_key or key > self._metadata_obj.max_key:
            return None

        # Use sparse index to find the scan range via binary search
        start_offset, end_offset = self._find_scan_range(key)

        # Scan the data block range for the key
        result = await asyncio.to_thread(self._scan_data_block, key, start_offset, end_offset)
        return result

    def _find_scan_range(self, key: str) -> Tuple[int, int]:
        """Use binary search on the sparse index to find the data block range to scan.

        Returns:
            Tuple of (start_offset, end_offset) in the data block.
        """
        if not self._index:
            return 0, self._data_block_size

        # Binary search for the largest index entry <= key
        lo, hi = 0, len(self._index) - 1
        result_idx = 0

        while lo <= hi:
            mid = (lo + hi) // 2
            if self._index[mid].key <= key:
                result_idx = mid
                lo = mid + 1
            else:
                hi = mid - 1

        start_offset = self._index[result_idx].offset

        # End offset is the next index entry's offset, or end of data block
        if result_idx + 1 < len(self._index):
            end_offset = self._index[result_idx + 1].offset
        else:
            end_offset = self._data_block_size

        return start_offset, end_offset

    def _scan_data_block(self, key: str, start_offset: int, end_offset: int) -> Optional[SSTableEntry]:
        """Scan a range of the data block for a specific key.

        Since entries are sorted, we can stop early if we pass the target key.
        """
        pos = start_offset
        data = self._raw_data

        while pos < end_offset:
            entry, next_pos = self._read_entry_at(data, pos)
            if entry is None:
                break

            if entry.key == key:
                return entry
            elif entry.key > key:
                # Entries are sorted, so key is not present
                break

            pos = next_pos

        return None

    def _read_entry_at(self, data: bytes, pos: int) -> Tuple[Optional[SSTableEntry], int]:
        """Read a single entry from the data block at the given position.

        Returns:
            Tuple of (entry, next_position). Entry is None if read fails.
        """
        try:
            # key_len (4 bytes)
            if pos + 4 > self._data_block_size:
                return None, pos
            key_len = struct.unpack_from("<I", data, pos)[0]
            pos += 4

            # key bytes
            if pos + key_len > self._data_block_size:
                return None, pos
            key = data[pos: pos + key_len].decode("utf-8")
            pos += key_len

            # value_len (4 bytes)
            if pos + 4 > self._data_block_size:
                return None, pos
            value_len = struct.unpack_from("<I", data, pos)[0]
            pos += 4

            # value bytes
            if pos + value_len > self._data_block_size:
                return None, pos
            value_bytes = data[pos: pos + value_len]
            pos += value_len

            # timestamp (8 bytes, double)
            if pos + 8 > self._data_block_size:
                return None, pos
            timestamp = struct.unpack_from("<d", data, pos)[0]
            pos += 8

            # is_tombstone (1 byte)
            if pos + 1 > self._data_block_size:
                return None, pos
            is_tombstone = struct.unpack_from("<B", data, pos)[0] == 1
            pos += 1

            value = value_bytes if not is_tombstone else None

            return SSTableEntry(
                key=key,
                value=value,
                timestamp=timestamp,
                is_tombstone=is_tombstone,
            ), pos

        except (struct.error, UnicodeDecodeError):
            return None, pos

    def may_contain(self, key: str) -> bool:
        """Check the Bloom filter for possible key existence.

        Args:
            key: The key to check.

        Returns:
            True if the key might exist, False if definitely not.
        """
        return self._bloom_filter.might_contain(key)

    def entries(self) -> Iterator[SSTableEntry]:
        """Iterate all entries in the data block in sorted order.

        Used for compaction and full table scans.

        Returns:
            Iterator of SSTableEntry objects in sorted key order.
        """
        pos = 0
        data = self._raw_data

        while pos < self._data_block_size:
            entry, next_pos = self._read_entry_at(data, pos)
            if entry is None:
                break
            yield entry
            pos = next_pos

    @property
    def metadata(self) -> SSTableMetadata:
        """Get SSTable metadata.

        Returns:
            SSTableMetadata with file path, level, key range, entry count, etc.
        """
        return self._metadata_obj
