"""WAL recovery must preserve writes acknowledged after a failure or restart."""

import asyncio
import os
import threading

import pytest

from kv_store.config import StorageConfig
from kv_store.storage.engine import StorageEngine
from kv_store.storage.wal import WALEntry, WALEntryType, WriteAheadLog


def put_entry(key):
    return WALEntry(0, WALEntryType.PUT, key, key.encode(), 1)


@pytest.mark.parametrize("tail_size", [1, 2, 3, 4, 10, -1])
async def test_append_after_torn_tail_survives_another_replay(tmp_path, tail_size):
    wal = WriteAheadLog(str(tmp_path))
    path = tmp_path / "wal.log"
    try:
        await wal.append(put_entry("accepted"))
        prefix = path.read_bytes()
        await wal.append(put_entry("interrupted"))
        complete = path.read_bytes()
    finally:
        await wal.close()
    cutoff = len(complete) - 1 if tail_size == -1 else len(prefix) + tail_size
    path.write_bytes(complete[:cutoff])

    recovered = WriteAheadLog(str(tmp_path))
    try:
        assert [entry.key for entry in await recovered.replay()] == ["accepted"]
        await recovered.append(put_entry("after-recovery"))
    finally:
        await recovered.close()
    reopened = WriteAheadLog(str(tmp_path))
    assert [entry.key for entry in await reopened.replay()] == [
        "accepted",
        "after-recovery",
    ]


@pytest.mark.parametrize("failure", ["write", "fsync"])
async def test_io_failure_fences_queued_appends_and_truncate(
    tmp_path, monkeypatch, failure
):
    wal = WriteAheadLog(str(tmp_path))
    path = tmp_path / "wal.log"
    try:
        await wal.append(put_entry("accepted"))
        write = os.write
        calls = 0

        def partial_write(descriptor, payload):
            nonlocal calls
            calls += 1
            if calls == 1:
                return write(descriptor, payload[:7])
            raise OSError("injected short-write failure")

        def failed_sync(_):
            raise OSError("injected fsync failure")

        with monkeypatch.context() as patch:
            if failure == "write":
                patch.setattr("kv_store.storage.wal.os.write", partial_write)
            else:
                patch.setattr("kv_store.storage.wal.os.fsync", failed_sync)
            with pytest.raises(OSError, match="injected"):
                await wal.append(put_entry("uncertain"))

        failed_bytes = path.read_bytes()
        failed_sequence = wal.sequence_number
        results = await asyncio.gather(
            *(wal.append(put_entry(f"queued-{i}")) for i in range(16)),
            return_exceptions=True,
        )
        assert all(isinstance(result, RuntimeError) for result in results)
        assert wal.sequence_number == failed_sequence
        with pytest.raises(RuntimeError, match="recovery"):
            await wal.truncate()
        assert path.read_bytes() == failed_bytes

        await wal.close()
        with pytest.raises(RuntimeError, match="recovery"):
            await wal.append(put_entry("close-is-not-recovery"))
        expected = ["accepted"] + (["uncertain"] if failure == "fsync" else [])
        assert [entry.key for entry in await wal.replay()] == expected
        await wal.append(put_entry("after-recovery"))
    finally:
        await wal.close()
    recovered = WriteAheadLog(str(tmp_path))
    assert [entry.key for entry in await recovered.replay()] == expected + [
        "after-recovery"
    ]


async def test_failed_truncation_requires_recovery(tmp_path, monkeypatch):
    wal = WriteAheadLog(str(tmp_path))
    try:
        await wal.append(put_entry("accepted"))

        def fail():
            raise OSError("injected truncate failure")

        with monkeypatch.context() as patch:
            patch.setattr(wal, "_truncate_file", fail)
            with pytest.raises(OSError, match="truncate failure"):
                await wal.truncate()
        with pytest.raises(RuntimeError, match="recovery"):
            await wal.append(put_entry("rejected"))
        assert [entry.key for entry in await wal.replay()] == ["accepted"]
        await wal.append(put_entry("after-recovery"))
    finally:
        await wal.close()


async def test_cancelled_recovery_keeps_writer_lock_until_disk_work_finishes(tmp_path):
    wal = WriteAheadLog(str(tmp_path))
    await wal.append(put_entry("accepted"))
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    original = wal._read_all_entries

    def blocked_read():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(3)
        return original()

    wal._read_all_entries = blocked_read
    replay = asyncio.create_task(wal.replay())
    append = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        append = asyncio.create_task(wal.append(put_entry("queued")))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(append), 0.05)
        replay.cancel()
        await asyncio.sleep(0)
        replay.cancel()
        await asyncio.sleep(0)
        assert not replay.done()
    finally:
        release.set()
        await asyncio.gather(
            replay, *([append] if append else []), return_exceptions=True
        )
        await wal.close()
    assert replay.cancelled()
    assert append is not None and append.result() is None
    recovered = WriteAheadLog(str(tmp_path))
    assert [entry.key for entry in await recovered.replay()] == ["accepted", "queued"]


async def test_storage_restart_then_write_survives_a_second_unclean_stop(tmp_path):
    config = StorageConfig(str(tmp_path), str(tmp_path / "wal"), str(tmp_path / "sst"))
    original = StorageEngine(config)
    await original.start()
    await original.put("accepted", b"old", 1)
    await original._wal.close()  # Simulate an unclean stop without MemTable flush.
    with (tmp_path / "wal/wal.log").open("ab") as stream:
        stream.write(b"\x10\x00")

    recovered = StorageEngine(config)
    await recovered.start()
    await recovered.put("after-recovery", b"new", 2)
    await recovered._wal.close()
    reopened = StorageEngine(config)
    await reopened.start()
    try:
        assert (await reopened.get("accepted")).value == b"old"
        result = await reopened.get("after-recovery")
        assert result is not None and result.value == b"new"
    finally:
        await reopened.stop()
