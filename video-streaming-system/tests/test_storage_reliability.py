"""Immutable publication, materialization isolation and metadata ownership."""

import concurrent.futures
import hashlib
import os
import pathlib
import threading
import types

import pytest

from video_streaming.database import Postgres, Result
from video_streaming.remote_storage import S3Objects
from video_streaming.storage import LocalObjects
import video_streaming.database as metadata


def test_duplicate_write_reuses_open_immutable_file_and_refreshes_gc_grace(tmp_path):
    store = LocalObjects(tmp_path)
    key = store.put(b"unchanged object")
    path = store.file(key)
    os.utime(path, (1, 1))
    inode = path.stat().st_ino
    with path.open("rb") as reader:
        assert store.put(b"unchanged object") == key
        assert reader.read() == b"unchanged object"
    assert path.stat().st_ino == inode
    assert path.stat().st_mtime > 1


def test_concurrent_duplicate_writes_and_corruption_repair(tmp_path):
    store = LocalObjects(tmp_path)
    data = b"shared source" * 4096
    with concurrent.futures.ThreadPoolExecutor(16) as pool:
        keys = list(pool.map(lambda _: store.put(data), range(64)))
    assert len(set(keys)) == 1
    path = store.file(keys[0])
    assert path.read_bytes() == data
    path.write_bytes(b"corruption")
    assert store.put(data) == keys[0]
    assert path.read_bytes() == data
    assert not list(tmp_path.glob("*.tmp"))


def test_interrupted_source_never_publishes_partial_object(tmp_path):
    store = LocalObjects(tmp_path)

    def interrupted():
        yield b"prefix"
        raise OSError("source interrupted")

    with pytest.raises(OSError, match="interrupted"):
        store.write(interrupted())
    assert not list(tmp_path.iterdir())


class MissingObject(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class S3Client:
    def __init__(self, values):
        self.values = {
            "video/" + hashlib.sha256(value).hexdigest(): value for value in values
        }
        self.downloads = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.lock = threading.Lock()

    def head_object(self, *, Bucket, Key):
        del Bucket
        if Key not in self.values:
            raise MissingObject()
        return {"ContentLength": len(self.values[Key])}

    def download_file(self, bucket, key, target):
        del bucket
        with self.lock:
            self.downloads += 1
        self.entered.set()
        assert self.release.wait(5)
        pathlib.Path(target).write_bytes(self.values[key])

    def upload_file(self, path, bucket, key, ExtraArgs):
        del bucket
        value = pathlib.Path(path).read_bytes()
        assert ExtraArgs["Metadata"]["sha256"] == hashlib.sha256(value).hexdigest()
        self.values[key] = value


def test_concurrent_cold_materialization_downloads_once(tmp_path):
    data = b"remote object" * 1000
    client = S3Client([data])
    client.release.clear()
    store = S3Objects(tmp_path, "private", client=client)
    key = hashlib.sha256(data).hexdigest()
    with concurrent.futures.ThreadPoolExecutor(16) as pool:
        pending = [pool.submit(store.file(key).read_bytes) for _ in range(16)]
        try:
            assert client.entered.wait(2)
        finally:
            client.release.set()
        assert [future.result(2) for future in pending] == [data] * 16
    assert client.downloads == 1
    assert not list(tmp_path.glob("*.tmp"))


def test_modified_warm_cache_is_recovered_from_authoritative_object(tmp_path):
    data = b"remote content"
    client = S3Client([data])
    store = S3Objects(tmp_path, "private", client=client)
    key = hashlib.sha256(data).hexdigest()
    assert store.file(key).read_bytes() == data
    (tmp_path / key).write_bytes(b"corrupt cache")
    assert store.file(key).read_bytes() == data
    assert client.downloads == 2


def test_existing_unverified_cache_is_checked_before_use(tmp_path):
    data = b"authoritative"
    client = S3Client([data])
    store = S3Objects(tmp_path, "private", client=client)
    key = hashlib.sha256(data).hexdigest()
    (tmp_path / key).write_bytes(b"broken from previous process")
    assert store.file(key).read_bytes() == data
    assert client.downloads == 1


def test_warm_cache_cannot_hide_remote_loss_or_failed_repair(tmp_path):
    data = b"original"
    client = S3Client([data])
    store = S3Objects(tmp_path, "private", client=client)
    key = hashlib.sha256(data).hexdigest()
    assert store.file(key).read_bytes() == data
    del client.values["video/" + key]
    with pytest.raises(FileNotFoundError):
        store.file(key).read_bytes()
    client.values["video/" + key] = b"remote corruption"
    (tmp_path / key).write_bytes(b"cache corruption")
    with pytest.raises(OSError, match="checksum"):
        store.file(key).read_bytes()
    assert not list(tmp_path.glob("*.tmp"))


def test_cold_download_does_not_block_different_warm_object(tmp_path):
    warm = b"warm"
    warm_key = hashlib.sha256(warm).hexdigest()
    cold = next(
        str(i).encode()
        for i in range(1000)
        if hash(hashlib.sha256(str(i).encode()).hexdigest()) % 64 != hash(warm_key) % 64
    )
    client = S3Client([warm, cold])
    store = S3Objects(tmp_path, "private", client=client)
    assert store.file(warm_key).read_bytes() == warm
    client.entered.clear()
    client.release.clear()
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        future = pool.submit(store.file(hashlib.sha256(cold).hexdigest()).read_bytes)
        try:
            assert client.entered.wait(2)
            assert pool.submit(store.file(warm_key).read_bytes).result(2) == warm
        finally:
            client.release.set()
        assert future.result(2) == cold


class Cursor:
    rowcount = 3
    description = [types.SimpleNamespace(name="value")]

    def __init__(self, values=(1, 2, 3)):
        self.values = values

    def fetchall(self):
        return [(value,) for value in self.values]

    def fetchone(self):
        return (self.values[0],)


def test_buffered_result_iteration_consumes_one_cursor_position():
    result = Result(Cursor())
    assert next(iter(result))["value"] == 1
    assert result.fetchone()[0] == 2
    assert result.fetchall() == [(3,)]
    assert result.fetchone() is None and list(result) == []


@pytest.fixture
def postgres(monkeypatch):
    class DriverError(Exception):
        pass

    class Connection:
        instances = []

        @classmethod
        def connect(cls, *args, **kwargs):
            del args, kwargs
            instance = cls()
            instance.operations = []
            instance.closed = False
            cls.instances.append(instance)
            return instance

        def execute(self, query, params=()):
            self.operations.append((query, params))
            return (
                Cursor(("off",)) if query == "SHOW transaction_read_only" else Cursor()
            )

        def close(self):
            self.closed = True

    driver = types.SimpleNamespace(
        Connection=Connection, Error=DriverError, OperationalError=DriverError
    )
    monkeypatch.setattr(metadata, "psycopg", driver)
    adapter = Postgres("test-only")
    yield adapter
    adapter.close()


def test_postgres_other_thread_cannot_join_owned_transaction(postgres):
    entered, release, attempted, finished = (threading.Event() for _ in range(4))

    def owner():
        with postgres:
            postgres.execute("SELECT ?", ("owner",))
            entered.set()
            assert release.wait(5)

    def outsider():
        attempted.set()
        postgres.execute("SELECT ?", ("outsider",))
        finished.set()

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        first = pool.submit(owner)
        try:
            assert entered.wait(2)
            second = pool.submit(outsider)
            assert attempted.wait(2)
            assert not finished.wait(0.05)
        finally:
            release.set()
        first.result(2)
        second.result(2)
    operations = postgres.connection.operations
    assert operations.index(("COMMIT", ())) < operations.index(
        ("SELECT %s", ("outsider",))
    )


def test_postgres_nested_transaction_rejection_preserves_outer_owner(postgres):
    with postgres:
        with pytest.raises(RuntimeError, match="nested"):
            with postgres:
                pass
        postgres.execute("SELECT ?", ("still owned",))
    assert not postgres.active
    assert postgres.connection.operations[-1] == ("COMMIT", ())


def test_postgres_close_waits_transaction_then_rejects_reconnection(postgres):
    entered, release, closing = threading.Event(), threading.Event(), threading.Event()

    def owner():
        with postgres:
            entered.set()
            assert release.wait(5)

    def close():
        postgres.close()
        closing.set()

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        first = pool.submit(owner)
        try:
            assert entered.wait(2)
            second = pool.submit(close)
            assert not closing.wait(0.05)
        finally:
            release.set()
        first.result(2)
        second.result(2)
    assert closing.is_set()
    with pytest.raises(RuntimeError, match="closed"):
        postgres.execute("SELECT 1")
