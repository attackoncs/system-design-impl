from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import sqlite3
from threading import Thread
import time
from urllib.error import HTTPError

import pytest

from file_sync import Drive, Conflict, CorruptBlock, ArchivedBlock, safe_path
from file_sync.client import DeviceClient
from file_sync.http import server


def manifest(data, size=4):
    return [{"digest": hashlib.sha256(data[i:i+size]).hexdigest(), "size": len(data[i:i+size])} for i in range(0, len(data), size)]


def upload(drive, token, space, data=b"abcdefgh", path="folder/file.txt", base=None, request=None):
    import uuid
    blocks = manifest(data, drive.block_size)
    session = drive.begin(token, space, path, len(data), hashlib.sha256(data).hexdigest(), blocks, base, request or uuid.uuid4().hex)
    missing = set(session["missing"])
    for i, block in enumerate(blocks):
        if block["digest"] in missing:
            drive.put(token, session["upload"], block["digest"], data[i*drive.block_size:(i+1)*drive.block_size])
            missing.remove(block["digest"])
    return drive.commit(token, session["upload"])


@pytest.fixture
def drive(tmp_path):
    now = [100000.0]
    service = Drive(tmp_path / "db.sqlite3", tmp_path / "objects", 4, clock=lambda: now[0])
    service.now = now
    service.token = service.issue("alice", quota=1024)
    service.space = service.create_space(service.token, "Personal")
    yield service
    service.close()


@contextmanager
def network(drive):
    http = server(drive)
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_address[1]}"
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=5)


def test_resumable_dedup_and_idempotent_publish(drive):
    data = b"abcdabcd"
    blocks = manifest(data)
    args = (drive.token, drive.space, "a.txt", len(data), hashlib.sha256(data).hexdigest(), blocks)
    pending = drive.begin(*args, request="resume")
    assert len(pending["missing"]) == 1
    assert drive.list_files(drive.token, drive.space) == []
    with pytest.raises(OSError):
        drive.commit(drive.token, pending["upload"])
    drive.put(drive.token, pending["upload"], blocks[0]["digest"], b"abcd")
    assert drive.begin(*args, request="resume")["missing"] == []
    first = drive.commit(drive.token, pending["upload"])
    assert drive.commit(drive.token, pending["upload"]) == first
    assert drive.db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 1
    assert drive.db.execute("SELECT refs FROM blocks").fetchone()[0] == 2
    upload(drive, drive.token, drive.space, data, "b.txt")
    assert drive.db.execute("SELECT COUNT(*) FROM blocks").fetchone()[0] == 1
    with pytest.raises(Conflict):
        drive.begin(drive.token, drive.space, "different.txt", len(data), hashlib.sha256(data).hexdigest(), blocks, request="resume")


def test_ciphertext_integrity_and_namespace_isolation(drive):
    result = upload(drive, drive.token, drive.space)
    row = drive.db.execute("SELECT * FROM blocks LIMIT 1").fetchone()
    encoded = drive.store.path(row["key"]).read_bytes()
    assert b"abcd" not in encoded
    assert drive.get_block(drive.token, result["file"], result["revision"], 0) == b"abcd"
    other = drive.create_space(drive.token, "Other")
    pending = drive.begin(drive.token, other, "a", 4, hashlib.sha256(b"abcd").hexdigest(), manifest(b"abcd"), request="other")
    assert pending["missing"]
    path = drive.store.path(row["key"])
    path.write_bytes(encoded[:-1] + bytes([encoded[-1] ^ 1]))
    with pytest.raises(CorruptBlock):
        drive.get_block(drive.token, result["file"], result["revision"], 0)


@pytest.mark.parametrize("path", ["../a", "/a", "a//b", "a/../b", "a\\b", "NUL.txt", "a:", "a.", ".sync/data", "a\x00b", ""])
def test_unsafe_paths(path):
    with pytest.raises(ValueError):
        safe_path(path)


def test_concurrent_edits_create_exactly_one_conflict(drive, tmp_path):
    first = upload(drive, drive.token, drive.space)
    second = Drive(tmp_path / "db.sqlite3", tmp_path / "objects", 4)
    try:
        def change(service, data):
            return upload(service, drive.token, drive.space, data, base=first["revision"])
        with ThreadPoolExecutor(2) as pool:
            a = pool.submit(change, drive, b"AAAAaaaa")
            b = pool.submit(change, second, b"BBBBbbbb")
            results = [a.result(), b.result()]
        assert sorted(r["conflict"] for r in results) == [False, True]
        assert len(drive.list_files(drive.token, drive.space)) == 2
        assert len(drive.revisions(drive.token, first["file"])) == 2
    finally:
        second.close()


def test_empty_delete_restore_history_and_stale_delete(drive):
    empty = upload(drive, drive.token, drive.space, b"", "empty")
    assert drive.metadata(drive.token, empty["file"])["blocks"] == []
    original = upload(drive, drive.token, drive.space)
    new = upload(drive, drive.token, drive.space, b"ijklmnop", base=original["revision"])
    with pytest.raises(Conflict):
        drive.delete(drive.token, new["file"], original["revision"])
    tombstone = drive.delete(drive.token, new["file"], new["revision"])
    assert drive.metadata(drive.token, new["file"])["deleted"]
    restored = drive.restore(drive.token, new["file"], original["revision"], tombstone, "restore")
    assert not restored["conflict"]
    assert drive.get_block(drive.token, restored["file"], restored["revision"], 0) == b"abcd"


def test_sharing_revocation_and_read_only(drive):
    result = upload(drive, drive.token, drive.space)
    bob = drive.issue("bob")
    with pytest.raises(PermissionError):
        drive.metadata(bob, result["file"])
    drive.share(drive.token, drive.space, "bob", "read")
    assert drive.metadata(bob, result["file"])
    with pytest.raises(PermissionError):
        upload(drive, bob, drive.space, b"bob", "bob")
    drive.share(drive.token, drive.space, "bob", "write")
    upload(drive, bob, drive.space, b"bob", "bob")
    drive.share(drive.token, drive.space, "bob", "revoke")
    with pytest.raises(PermissionError):
        drive.get_block(bob, result["file"], result["revision"], 0)


def test_quota_reservations_cancel_and_revision_retention(drive):
    tiny = drive.issue("tiny", quota=8)
    space = drive.create_space(tiny, "tiny")
    data = b"abcdabcd"
    pending = drive.begin(tiny, space, "a", 8, hashlib.sha256(data).hexdigest(), manifest(data), request="reservation")
    with pytest.raises(ValueError, match="quota"):
        upload(drive, tiny, space, b"x", "b")
    drive.cancel(tiny, pending["upload"])
    a = upload(drive, tiny, space, b"abcd", "a")
    b = upload(drive, tiny, space, b"efgh", "a", a["revision"])
    with pytest.raises(ValueError, match="quota"):
        upload(drive, tiny, space, b"ijkl", "a", b["revision"])
    assert drive.prune(tiny, a["file"], keep=1) == 1
    upload(drive, tiny, space, b"ijkl", "a", b["revision"])


def test_transaction_failure_does_not_publish_or_emit(drive):
    data = b"abcd"
    session = drive.begin(drive.token, drive.space, "a", 4, hashlib.sha256(data).hexdigest(), manifest(data), request="fail")
    drive.put(drive.token, session["upload"], manifest(data)[0]["digest"], data)
    drive.db.execute("CREATE TRIGGER fail_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'fault'); END")
    with pytest.raises(sqlite3.Error):
        drive.commit(drive.token, session["upload"])
    assert drive.list_files(drive.token, drive.space) == []
    assert drive.db.execute("SELECT refs FROM blocks").fetchone()[0] == 0
    drive.db.execute("DROP TRIGGER fail_event")
    assert drive.commit(drive.token, session["upload"])


def test_device_replay_ack_long_poll_and_restart(drive, tmp_path):
    drive.device(drive.token, "desktop")
    first = upload(drive, drive.token, drive.space)
    a = drive.changes(drive.token, "desktop", 0)
    assert drive.changes(drive.token, "desktop", 0) == a
    with pytest.raises(ValueError):
        drive.ack(drive.token, "desktop", a["cursor"] + 1)
    drive.ack(drive.token, "desktop", a["cursor"])
    second = Drive(tmp_path / "db.sqlite3", tmp_path / "objects", 4)
    try:
        assert second.device(drive.token, "desktop")["cursor"] == a["cursor"]
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(second.changes, drive.token, "desktop", a["cursor"], 2)
            time.sleep(.05)
            upload(drive, drive.token, drive.space, b"ijklmnop", base=first["revision"])
            assert future.result()["events"]
    finally:
        second.close()


def test_gc_pins_sessions_and_references_and_cold_restore(drive):
    first = upload(drive, drive.token, drive.space, b"abcd", "a")
    old_key = drive.db.execute("SELECT key FROM blocks").fetchone()[0]
    second = upload(drive, drive.token, drive.space, b"efgh", "a", first["revision"])
    drive.prune(drive.token, first["file"], keep=1)
    pending = drive.begin(drive.token, drive.space, "pinned", 4, hashlib.sha256(b"abcd").hexdigest(), manifest(b"abcd"), request="pin")
    drive.now[0] += 4000
    assert drive.cleanup()["removed_objects"] == 0
    drive.cancel(drive.token, pending["upload"])
    assert drive.cleanup()["removed_objects"] == 1
    assert not drive.store.path(old_key).exists()
    meta = drive.metadata(drive.token, second["file"])
    key = drive.store.key(drive.space, meta["blocks"][0]["digest"])
    drive.store.archive(key)
    with pytest.raises(ArchivedBlock):
        drive.get_block(drive.token, second["file"], second["revision"], 0)
    drive.store.restore(key)
    assert drive.get_block(drive.token, second["file"], second["revision"], 0) == b"efgh"


def test_real_http_delta_client_dirty_copy_and_delete(drive, tmp_path):
    with network(drive) as url:
        a = DeviceClient(url, drive.token, tmp_path / "a", "a")
        b = DeviceClient(url, drive.token, tmp_path / "b", "b")
        try:
            (a.root / "test.txt").write_bytes(b"abcdefgh")
            first = a.push(drive.space, "test.txt")
            b.pull()
            assert (b.root / "test.txt").read_bytes() == b"abcdefgh"
            assert b.transferred_down == 8
            (a.root / "test.txt").write_bytes(b"abcdijkl")
            a.push(drive.space, "test.txt")
            assert a.transferred_up == 12
            (b.root / "test.txt").write_bytes(b"local-dirty")
            b.pull()
            assert (b.root / "test.txt").read_bytes() == b"abcdijkl"
            assert list(b.root.glob("*.local-conflict-*.txt"))[0].read_bytes() == b"local-dirty"
            assert b.transferred_down == 12
            a.delete(drive.space, "test.txt")
            b.pull()
            assert not (b.root / "test.txt").exists()
        finally:
            a.close()
            b.close()
