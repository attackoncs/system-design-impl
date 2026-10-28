"""Accepted-I/O ownership, ACL fences, durable events and maintenance races."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import threading

import pytest

from file_sync import Drive
from test_sync import drive as drive, manifest, upload


@pytest.mark.parametrize("operation", ["put", "commit", "get"])
def test_slow_block_io_does_not_block_unrelated_metadata(drive, monkeypatch, operation):
    data = b"abcd"
    if operation == "get":
        result = upload(drive, drive.token, drive.space, data, "source")
        execute = lambda: drive.get_block(
            drive.token, result["file"], result["revision"], 0
        )
    else:
        session = drive.begin(
            drive.token,
            drive.space,
            "source",
            4,
            hashlib.sha256(data).hexdigest(),
            manifest(data),
            request="slow",
        )
        if operation == "commit":
            drive.put(drive.token, session["upload"], manifest(data)[0]["digest"], data)
            execute = lambda: drive.commit(drive.token, session["upload"])
        else:
            execute = lambda: drive.put(
                drive.token, session["upload"], manifest(data)[0]["digest"], data
            )
    entered, release = threading.Event(), threading.Event()
    method = "put" if operation == "put" else "get"
    original = getattr(drive.store, method)

    def slow(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(drive.store, method, slow)
    with ThreadPoolExecutor(2) as pool:
        pending = pool.submit(execute)
        try:
            assert entered.wait(2)
            assert pool.submit(drive.create_space, drive.token, "Independent").result(1)
        finally:
            release.set()
        assert pending.result(2)


@pytest.mark.parametrize("action", ["cancel", "revoke"])
def test_commit_rechecks_session_and_acl_after_checksum_io(
    drive, tmp_path, monkeypatch, action
):
    bob = drive.issue("bob")
    drive.share(drive.token, drive.space, "bob", "write")
    data = b"abcd"
    session = drive.begin(
        bob,
        drive.space,
        "unpublished",
        4,
        hashlib.sha256(data).hexdigest(),
        manifest(data),
        request="fenced",
    )
    drive.put(bob, session["upload"], manifest(data)[0]["digest"], data)
    other = Drive(tmp_path / "db.sqlite3", tmp_path / "objects", 4)
    entered, release = threading.Event(), threading.Event()
    original = drive.store.get

    def slow(*args):
        value = original(*args)
        entered.set()
        assert release.wait(5)
        return value

    monkeypatch.setattr(drive.store, "get", slow)
    try:
        with ThreadPoolExecutor(2) as pool:
            pending = pool.submit(drive.commit, bob, session["upload"])
            try:
                assert entered.wait(2)
                if action == "cancel":
                    pool.submit(other.cancel, bob, session["upload"]).result(1)
                else:
                    pool.submit(
                        other.share, drive.token, drive.space, "bob", "revoke"
                    ).result(1)
            finally:
                release.set()
            with pytest.raises(ValueError if action == "cancel" else PermissionError):
                pending.result(2)
        assert drive.list_files(drive.token, drive.space) == []
        assert drive.db.execute("SELECT refs FROM blocks").fetchone()[0] == 0
    finally:
        release.set()
        other.close()


def test_close_wakes_long_poll_but_waits_for_inflight_block_read(drive, monkeypatch):
    result = upload(drive, drive.token, drive.space, b"abcd", "source")
    drive.device(drive.token, "waiting")
    cursor = drive.changes(drive.token, "waiting", 0)["cursor"]
    poll_entered, read_entered, release, closed = (threading.Event() for _ in range(4))
    batch = drive.events._batch
    original = drive.store.get

    def observing(*args):
        value = batch(*args)
        poll_entered.set()
        return value

    def slow(*args):
        read_entered.set()
        assert release.wait(5)
        return original(*args)

    def close():
        drive.close()
        closed.set()

    monkeypatch.setattr(drive.events, "_batch", observing)
    monkeypatch.setattr(drive.store, "get", slow)
    with ThreadPoolExecutor(3) as pool:
        poll = pool.submit(drive.changes, drive.token, "waiting", cursor, 25)
        read = pool.submit(
            drive.get_block, drive.token, result["file"], result["revision"], 0
        )
        try:
            assert poll_entered.wait(2) and read_entered.wait(2)
            shutdown = pool.submit(close)
            assert poll.result(1)["events"] == []
            assert not closed.wait(0.05)
        finally:
            release.set()
        assert read.result(2) == b"abcd"
        shutdown.result(2)
    with pytest.raises(RuntimeError):
        drive.issue("after-close")


def test_idle_poll_reads_repeatedly_but_updates_delivery_only_once(drive):
    drive.device(drive.token, "waiting")
    statements = []
    drive.db.set_trace_callback(statements.append)
    try:
        assert drive.changes(drive.token, "waiting", 0, wait=0.25)["events"] == []
    finally:
        drive.db.set_trace_callback(None)
    writes = [
        sql
        for sql in statements
        if sql.upper().startswith("UPDATE DEVICES SET DELIVERED")
    ]
    reads = [sql for sql in statements if "SELECT E.* FROM EVENTS" in sql.upper()]
    assert len(reads) >= 2 and len(writes) == 1


def test_gc_failure_cannot_restore_expired_upload_after_deleting_content(
    drive, monkeypatch
):
    data = b"abcd"
    session = drive.begin(
        drive.token,
        drive.space,
        "expires",
        4,
        hashlib.sha256(data).hexdigest(),
        manifest(data),
        request="expires",
    )
    drive.put(drive.token, session["upload"], manifest(data)[0]["digest"], data)
    drive.now[0] += 100000
    original = drive.store.remove

    def deleted_then_failed(key):
        original(key)
        raise OSError("storage failed after deletion")

    monkeypatch.setattr(drive.store, "remove", deleted_then_failed)
    with pytest.raises(OSError):
        drive.cleanup()
    assert drive.upload_status(drive.token, session["upload"])["state"] == "CANCELLED"
    assert drive.quota("alice")["reserved"] == 0
    with pytest.raises(ValueError, match="not open"):
        drive.put(drive.token, session["upload"], manifest(data)[0]["digest"], data)


def test_simple_upload_history_limit_and_pending_notifications_are_idempotent(drive):
    first = drive.upload(
        drive.token, drive.space, "simple", b"abcd", request="simple-1"
    )
    assert (
        drive.upload(drive.token, drive.space, "simple", b"abcd", request="simple-1")
        == first
    )
    kinds = [row[0] for row in drive.db.execute("SELECT kind FROM events")]
    assert kinds.count("pending") == kinds.count("upsert") == 1
    second = drive.upload(
        drive.token,
        drive.space,
        "simple",
        b"efgh",
        base=first["revision"],
        request="simple-2",
    )
    assert [
        row["id"] for row in drive.revisions(drive.token, first["file"], limit=1)
    ] == [second["revision"]]
    meta = drive.metadata(drive.token, first["file"])
    meta["blocks"][0]["digest"] = "0" * 64
    assert (
        drive.metadata(drive.token, first["file"])["blocks"][0]["digest"]
        == hashlib.sha256(b"efgh").hexdigest()
    )
    pending = drive.begin(
        drive.token,
        drive.space,
        "cancel",
        0,
        hashlib.sha256(b"").hexdigest(),
        [],
        request="cancel",
    )
    drive.cancel(drive.token, pending["upload"])
    drive.cancel(drive.token, pending["upload"])
    assert (
        drive.db.execute(
            "SELECT COUNT(*) FROM events WHERE kind='cancelled'"
        ).fetchone()[0]
        == 1
    )


def test_notification_reply_rechecks_acl_after_readonly_batch(
    drive, tmp_path, monkeypatch
):
    bob = drive.issue("bob")
    drive.share(drive.token, drive.space, "bob", "read")
    upload(drive, drive.token, drive.space, b"abcd", "private")
    drive.device(bob, "bob-device")
    other = Drive(tmp_path / "db.sqlite3", tmp_path / "objects", 4)
    original = drive.events._batch
    revoked = False

    def capture_then_revoke(*args):
        nonlocal revoked
        result = original(*args)
        if not revoked:
            revoked = True
            other.share(drive.token, drive.space, "bob", "revoke")
        return result

    monkeypatch.setattr(drive.events, "_batch", capture_then_revoke)
    try:
        batch = drive.changes(bob, "bob-device", 0)
        assert [event["kind"] for event in batch["events"]] == ["revoke"]
        assert all(event["path"] is None for event in batch["events"])
    finally:
        other.close()
