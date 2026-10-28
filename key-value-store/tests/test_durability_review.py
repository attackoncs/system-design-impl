"""Cancellation, immutable input, publication and causal persistence regressions."""

import asyncio
import json
import threading

import pytest

from kv_store.async_work import complete
from kv_store.cluster.merkle_tree import MerkleTree
from kv_store.cluster.repair import HintJournal
from kv_store.config import StorageConfig
from kv_store.replication.records import ReplicaRecords, Version, decode, encode
from kv_store.replication.vector_clock import VectorClock
from kv_store.storage.engine import StorageEngine
from kv_store.storage.files import publish


def storage(root, threshold=4 * 1024 * 1024):
    return StorageConfig(
        str(root),
        str(root / "wal"),
        str(root / "sstables"),
        memtable_size_bytes=threshold,
        max_value_size_bytes=1024 * 1024,
    )


async def event_started(event):
    async def poll():
        while not event.is_set():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), 2)


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_owned_disk_work():
    entered, release = threading.Event(), threading.Event()
    finished = []

    def disk():
        entered.set()
        assert release.wait(3)
        finished.append(True)

    task = asyncio.create_task(complete(asyncio.to_thread(disk)))
    try:
        await event_started(entered)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished == [True]


@pytest.mark.asyncio
async def test_cancelled_record_commit_finishes_wal_memtable_and_merkle(tmp_path):
    config = storage(tmp_path)
    engine = StorageEngine(config)
    await engine.start()
    records = ReplicaRecords(engine, MerkleTree(16))
    entered, release = threading.Event(), threading.Event()
    original = engine._wal._write_and_sync

    def slow(payload):
        entered.set()
        assert release.wait(3)
        original(payload)

    engine._wal._write_and_sync = slow
    task = asyncio.create_task(
        records.merge("committed", [Version(b"value", VectorClock({"n": 1}), 1)])
    )
    try:
        await event_started(entered)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await records.read("committed"))[0].value == b"value"
        assert "committed" in records.tree.get_keys_in_bucket(
            records.tree._key_to_bucket("committed")
        )
    finally:
        await engine.stop()
    assert engine._wal._fd is None
    recovered = StorageEngine(config)
    await recovered.start()
    try:
        assert decode((await recovered.get("committed")).value)[0].value == b"value"
    finally:
        await recovered.stop()


@pytest.mark.asyncio
async def test_record_inputs_are_frozen_before_waiting_for_key_lock(tmp_path):
    engine = StorageEngine(storage(tmp_path))
    await engine.start()
    records = ReplicaRecords(engine, MerkleTree(16))
    original = Version(b"original", VectorClock({"n": 1}), 1)
    incoming = [original]
    lock = records.lock("key")
    await lock.acquire()
    task = asyncio.create_task(records.merge("key", incoming))
    try:
        await asyncio.sleep(0)
        incoming[0] = Version(b"modified", VectorClock({"n": 99}), 1)
        original.clock._entries["n"] = 42
    finally:
        lock.release()
    try:
        await task
        result = (await records.read("key"))[0]
        assert result.value == b"original" and result.clock.to_dict() == {"n": 1}
    finally:
        await engine.stop()


def test_failed_file_sync_leaves_existing_file_and_no_partial_publication(
    tmp_path, monkeypatch
):
    path = tmp_path / "data.sst"
    path.write_bytes(b"original")

    def fail(_):
        raise OSError("injected fsync failure")

    monkeypatch.setattr("kv_store.storage.files.os.fsync", fail)
    with pytest.raises(OSError):
        publish(path, b"replacement")
    assert path.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [path]


def test_failed_replace_keeps_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "data.sst"
    path.write_bytes(b"old")

    def fail(*_):
        raise OSError("injected replacement failure")

    monkeypatch.setattr("kv_store.storage.files.os.replace", fail)
    with pytest.raises(OSError):
        publish(path, b"new")
    assert path.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    "change",
    [
        lambda data: data.update(format=True),
        lambda data: data.update(extra=1),
        lambda data: data["versions"][0].update(extra=1),
        lambda data: data["versions"][0].update(timestamp=-1),
        lambda data: data["versions"][0].update(timestamp=1e13),
        lambda data: data["versions"][0].update(clock={"n": 1 << 63}),
    ],
)
def test_record_schema_rejects_ambiguous_or_unrepresentable_versions(change):
    data = json.loads(encode([Version(b"x", VectorClock({"n": 1}), 1)]))
    change(data)
    with pytest.raises(ValueError):
        decode(json.dumps(data).encode())


def test_duplicate_json_fields_cannot_change_record_meaning():
    with pytest.raises(ValueError):
        decode(b'{"format":1,"format":1,"versions":[]}')


@pytest.mark.asyncio
@pytest.mark.parametrize("threshold", [1, 4 * 1024 * 1024])
async def test_causal_progress_survives_wall_clock_rollback_flush_and_restart(
    tmp_path, monkeypatch, threshold
):
    config = storage(tmp_path, threshold)
    config.compaction_threshold = 2
    engine = StorageEngine(config)
    await engine.start()
    records = ReplicaRecords(engine, MerkleTree(16))
    clock = [100.0]
    monkeypatch.setattr("kv_store.replication.records.time.time", lambda: clock[0])
    try:
        await records.merge("key", [Version(b"old", VectorClock({"n": 1}), 1)])
        clock[0] = 10.0
        await records.merge("key", [Version(b"new", VectorClock({"n": 2}), 2)])
        assert (await records.read("key"))[0].value == b"new"
        await records.merge("key", [Version(None, VectorClock({"n": 3}), 3)])
        assert (await records.read("key"))[0].value is None
        assert len(list((tmp_path / "sstables").glob("*.sst"))) == len(engine._sstables)
    finally:
        await engine.stop()
    recovered = StorageEngine(config)
    await recovered.start()
    try:
        result = decode((await recovered.get("key")).value)
        assert result[0].value is None and result[0].clock.to_dict() == {"n": 3}
        assert (await recovered.scan())["key"].value == encode(result)
    finally:
        await recovered.stop()


@pytest.mark.asyncio
async def test_late_hint_ack_cannot_delete_a_new_row_reusing_its_id(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class Membership:
        def is_node_alive(self, target):
            return True

        def get_node_address(self, target):
            return target

    class Transport:
        def __init__(self, pause=False):
            self.pause = pause

        async def merge_replica(self, *args):
            if self.pause:
                entered.set()
                await release.wait()
            return True

    path = tmp_path / "hints.sqlite"
    slow = HintJournal(path, Membership(), Transport(True))
    fast = HintJournal(path, Membership(), Transport())
    await slow.save("target", "key", [Version(b"old", VectorClock({"n": 1}), 1)])
    task = asyncio.create_task(slow.deliver())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await fast.deliver()
        await fast.save("target", "key", [Version(b"new", VectorClock({"n": 2}), 2)])
    finally:
        release.set()
    await task
    rows = slow._execute("SELECT id,record FROM hints")
    assert len(rows) == 1 and rows[0][0] == 1
    assert decode(rows[0][1])[0].value == b"new"


@pytest.mark.asyncio
async def test_failed_shutdown_flush_closes_wal_and_recovers_accepted_data(tmp_path):
    config = storage(tmp_path)
    engine = StorageEngine(config)
    await engine.start()
    await engine.put("accepted", b"value", 1)

    async def failed_flush():
        raise OSError("injected flush failure")

    engine._flush_memtable = failed_flush
    with pytest.raises(OSError):
        await engine.stop()
    assert engine._wal._fd is None and not engine._started
    recovered = StorageEngine(config)
    await recovered.start()
    try:
        assert (await recovered.get("accepted")).value == b"value"
    finally:
        await recovered.stop()


@pytest.mark.asyncio
async def test_corrupt_final_sstable_is_not_silently_omitted(tmp_path):
    config = storage(tmp_path)
    folder = tmp_path / "sstables"
    folder.mkdir()
    path = folder / "bad.sst"
    path.write_bytes(b"corrupt")
    engine = StorageEngine(config)
    with pytest.raises(ValueError, match="requires recovery"):
        await engine.start()
    assert path.read_bytes() == b"corrupt" and not engine._started


@pytest.mark.asyncio
async def test_storage_writes_require_a_running_owner(tmp_path):
    engine = StorageEngine(storage(tmp_path))
    with pytest.raises(RuntimeError, match="not started"):
        await engine.put("key", b"value", 1)
    await engine.start()
    with pytest.raises(RuntimeError, match="already started"):
        await engine.start()
    await engine.stop()
    with pytest.raises(RuntimeError, match="not started"):
        await engine.delete("key", 2)


@pytest.mark.asyncio
async def test_post_wal_flush_failure_keeps_read_and_merkle_index_in_agreement(
    tmp_path,
):
    engine = StorageEngine(storage(tmp_path, threshold=1))
    await engine.start()
    records = ReplicaRecords(engine, MerkleTree(16))
    original = engine._flush_memtable

    async def failed():
        raise OSError("injected flush failure after WAL commit")

    engine._flush_memtable = failed
    try:
        with pytest.raises(OSError):
            await records.merge(
                "key", [Version(b"uncertain", VectorClock({"n": 1}), 1)]
            )
        assert (await records.read("key"))[0].value == b"uncertain"
        assert records.tree.get_keys_in_bucket(records.tree._key_to_bucket("key")) == [
            "key"
        ]
    finally:
        engine._flush_memtable = original
        await engine.stop()
