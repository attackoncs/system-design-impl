"""FS1 compatibility, bounded authenticated reads and coordinated tier lifecycles."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import threading
import zlib

import pytest

from file_sync.storage import ArchivedBlock, BlockStore, CorruptBlock

SPACE = "a" * 32
MASTER = b"k" * 32


@pytest.fixture
def store(tmp_path):
    return BlockStore(tmp_path / "objects", MASTER, 1024)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(store, data, compressed=None):
    nonce = bytes(range(12))
    return (
        b"FS1"
        + nonce
        + store.cipher(SPACE).encrypt(
            nonce,
            zlib.compress(data) if compressed is None else compressed,
            store.aad(SPACE, digest(data), len(data)),
        )
    )


def test_existing_fs1_ciphertext_remains_readable(store):
    data = b"previously persisted format"
    key = store.key(SPACE, digest(data))
    store.path(key).write_bytes(encoded(store, data))
    assert store.get(SPACE, digest(data), len(data)) == data


@pytest.mark.parametrize("size", [True, 0, -1, 1025, 4.0])
def test_read_rejects_invalid_declared_size(store, size):
    with pytest.raises(ValueError, match="size"):
        store.get(SPACE, digest(b"data"), size)


@pytest.mark.parametrize(
    "compressed",
    [
        zlib.compress(b"data") + b"trailing garbage",
        zlib.compress(b"data") + zlib.compress(b"another stream"),
        zlib.compress(b"data" * 100000),
        b"not a compressed stream",
    ],
)
def test_authenticated_but_invalid_compressed_payload_is_rejected(store, compressed):
    data = b"data"
    key = store.key(SPACE, digest(data))
    store.path(key).write_bytes(encoded(store, data, compressed))
    with pytest.raises(CorruptBlock):
        store.get(SPACE, digest(data), len(data))


def test_ciphertext_read_is_bounded_even_without_trusting_a_prior_stat(
    store, monkeypatch
):
    key = store.key(SPACE, digest(b"data"))
    target = store.path(key)
    target.write_bytes(b"x" * 100000)
    original, sizes = Path.open, []

    class Reader:
        def __init__(self):
            self.stream = original(target, "rb")

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.stream.close()

        def read(self, size=-1):
            sizes.append(size)
            assert 0 < size <= store.block_size + 65537
            return self.stream.read(size)

    def open_path(path, *args, **kwargs):
        return Reader() if path == target else original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_path)
    with pytest.raises(CorruptBlock, match="size"):
        store.get(SPACE, digest(b"data"), 4)
    assert sizes == [store.block_size + 65537]


def test_duplicate_put_preserves_open_inode_and_refreshes_gc_grace(store):
    data = b"same immutable plaintext"
    key = store.put(SPACE, digest(data), data)
    target = store.path(key)
    old = target.read_bytes()
    inode = target.stat().st_ino
    os.utime(target, (1, 1))
    with target.open("rb") as reader:
        assert store.put(SPACE, digest(data), data) == key
        assert reader.read() == old
    assert target.read_bytes() == old and target.stat().st_ino == inode
    assert target.stat().st_mtime > 1


def test_corrupt_existing_ciphertext_can_be_repaired(store):
    data = b"validated incoming bytes"
    key = store.put(SPACE, digest(data), data)
    store.path(key).write_bytes(b"damaged")
    assert store.put(SPACE, digest(data), data) == key
    assert store.get(SPACE, digest(data), len(data)) == data
    assert not list(store.root.glob("*.tmp"))


def test_concurrent_store_instances_publish_once(store, monkeypatch):
    other = BlockStore(store.root, MASTER, store.block_size)
    data, published = b"shared plaintext", []
    original = os.replace

    def replace(source, target):
        published.append(str(target))
        return original(source, target)

    monkeypatch.setattr(os, "replace", replace)
    with ThreadPoolExecutor(16) as pool:
        keys = list(
            pool.map(
                lambda i: (store if i % 2 else other).put(SPACE, digest(data), data),
                range(64),
            )
        )
    assert len(set(keys)) == len(published) == 1
    assert other.get(SPACE, digest(data), len(data)) == data


def test_archive_restore_retries_and_reupload_are_idempotent(store):
    data = b"cold content"
    key = store.put(SPACE, digest(data), data)
    store.archive(key)
    store.archive(key)
    assert store.exists(key)
    with pytest.raises(ArchivedBlock):
        store.get(SPACE, digest(data), len(data))
    store.restore(key)
    store.restore(key)
    assert store.get(SPACE, digest(data), len(data)) == data
    store.archive(key)
    assert store.put(SPACE, digest(data), data) == key
    assert not (store.cold / key).exists()
    assert store.get(SPACE, digest(data), len(data)) == data


def test_parallel_tier_moves_writes_and_reads_never_lose_the_block(store):
    instances = [store, BlockStore(store.root, MASTER, store.block_size)]
    data = b"coordinated tiers"
    key = store.put(SPACE, digest(data), data)

    def operation(index):
        instance = instances[index % 2]
        if index % 4 == 0:
            instance.archive(key)
        elif index % 4 == 1:
            instance.restore(key)
        elif index % 4 == 2:
            instance.put(SPACE, digest(data), data)
        else:
            try:
                assert instance.get(SPACE, digest(data), len(data)) == data
            except ArchivedBlock:
                pass

    with ThreadPoolExecutor(16) as pool:
        list(pool.map(operation, range(128)))
    store.restore(key)
    assert store.get(SPACE, digest(data), len(data)) == data


def test_distinct_existing_tier_copies_are_preserved_for_reconciliation(store):
    key = store.put(SPACE, digest(b"data"), b"data")
    hot = store.path(key).read_bytes()
    (store.cold / key).write_bytes(b"different retained ciphertext")
    with pytest.raises(FileExistsError):
        store.archive(key)
    assert store.path(key).read_bytes() == hot
    assert (store.cold / key).read_bytes() == b"different retained ciphertext"


def test_shared_guard_is_reentrant_across_instances_and_remove_is_idempotent(store):
    other = BlockStore(store.root, MASTER, store.block_size)
    key = store.put(SPACE, digest(b"data"), b"data")
    with store.locked(key):
        assert other.exists(key)
        assert other.get(SPACE, digest(b"data"), 4) == b"data"
    store.archive(key)
    assert other.remove(key) == 1
    assert store.remove(key) == 0
    assert not store.exists(key)


def test_os_guard_coordinates_an_independent_process(store, tmp_path):
    data = b"process ownership"
    key = store.put(SPACE, digest(data), data)
    release = tmp_path / "release"
    code = (
        "import sys,time; from pathlib import Path; from file_sync.storage import BlockStore; "
        "store=BlockStore(sys.argv[1],b'k'*32,1024); "
        "guard=store.locked(sys.argv[2]); guard.__enter__(); print('held',flush=True); "
        "release=Path(sys.argv[3]); deadline=time.monotonic()+5; "
        "\nwhile not release.exists() and time.monotonic()<deadline: time.sleep(.01)"
        "\nguard.__exit__(None,None,None)"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(store.root), key, str(release)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **options,
    )
    entered = threading.Event()

    def archive():
        entered.set()
        store.archive(key)

    try:
        assert child.stdout.readline().strip() == b"held"
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(archive)
            try:
                assert entered.wait(1)
                assert not pending.done()
            finally:
                release.touch()
            pending.result(3)
        assert child.wait(3) == 0
        with pytest.raises(ArchivedBlock):
            store.get(SPACE, digest(data), len(data))
        store.restore(key)
        assert store.get(SPACE, digest(data), len(data)) == data
    finally:
        release.touch(exist_ok=True)
        if child.poll() is None:
            child.kill()
            child.wait(3)
        child.stdout.close()
        child.stderr.close()
