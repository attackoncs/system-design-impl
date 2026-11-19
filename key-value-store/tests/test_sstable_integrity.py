"""Corrupt immutable files must fail reads and compaction without data loss."""

import struct

import pytest
from kv_store.storage.compaction import CompactionManager
from kv_store.storage.sstable import SSTable, write_sstable


def make_table(path):
    write_sstable(
        str(path), [("a", b"one", 1.0, False), ("z", b"two", 2.0, False)], 2, 0
    )
    return SSTable(str(path))


@pytest.mark.parametrize("operation", ["get", "scan", "compact"])
async def test_bad_frame_is_an_error_not_missing_data(tmp_path, operation):
    path = tmp_path / "input.sst"
    table = make_table(path)
    try:
        # Corrupt the second value length, leaving its index/footer intact.
        with path.open("r+b") as stream:
            stream.seek(21 + 5)  # first record=4+1+4+3+8+1; second key header=5
            stream.write(struct.pack("<I", 0xFFFFFFFF))
        with pytest.raises(ValueError, match="[Cc]orrupt|[Ii]nvalid|[Tt]runcated"):
            if operation == "get":
                await table.get("z")
            elif operation == "scan":
                list(table.entries())
            else:
                await CompactionManager(str(tmp_path)).compact([table], 0)
        assert list(tmp_path.glob("*.sst")) == [path]
        assert not list(tmp_path.glob("*.tmp"))
    finally:
        table.close()


@pytest.mark.parametrize(
    "offset, replacement", [(12, struct.pack("<d", float("nan"))), (20, b"\x02")]
)
async def test_invalid_timestamp_or_tombstone_rejected(tmp_path, offset, replacement):
    path = tmp_path / "input.sst"
    table = make_table(path)
    try:
        with path.open("r+b") as stream:
            stream.seek(offset)
            stream.write(replacement)
        with pytest.raises(ValueError):
            await table.get("a")
        with pytest.raises(ValueError):
            list(table.entries())
    finally:
        table.close()


async def test_short_disk_read_is_not_end_of_table(tmp_path):
    path = tmp_path / "input.sst"
    table = make_table(path)
    try:
        with path.open("r+b") as stream:
            stream.truncate(21)
        with pytest.raises(ValueError):
            list(table.entries())
    finally:
        table.close()


def test_invalid_sparse_index_rejected_at_open(tmp_path):
    path = tmp_path / "input.sst"
    table = make_table(path)
    table.close()
    with path.open("r+b") as stream:
        stream.seek(42 + 5)  # two data records, then index key header
        stream.write(struct.pack("<Q", 1))  # the first block must start at 0
    with pytest.raises(ValueError):
        SSTable(str(path))


@pytest.mark.parametrize("operation", ["get", "scan"])
async def test_corrupt_middle_index_cannot_hide_an_existing_key(tmp_path, operation):
    path = tmp_path / "input.sst"
    write_sstable(str(path), [(f"k{i:02}", b"v", 1.0, False) for i in range(33)], 33, 0)
    # Each data record is 21 bytes, each sparse-index entry is 15 bytes.
    with path.open("r+b") as stream:
        stream.seek(33 * 21 + 15 + 4)
        stream.write(b"k17")  # valid sorted index, but its actual block starts at k16
    table = SSTable(str(path))
    try:
        with pytest.raises(ValueError):
            if operation == "get":
                await table.get("k16")
            else:
                list(table.entries())
    finally:
        table.close()
