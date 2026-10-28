import hashlib
from urllib.error import HTTPError

import pytest

from file_sync import Conflict
from file_sync import Drive
from file_sync.client import DeviceClient
from test_sync import drive as drive, manifest, network, upload


def test_manifest_checks_and_file_hash_failure(drive):
    with pytest.raises(ValueError):
        drive.begin(
            drive.token,
            drive.space,
            "bad",
            8,
            "0" * 64,
            [{"digest": "0" * 64, "size": 2}, {"digest": "1" * 64, "size": 6}],
            request="bad",
        )
    session = drive.begin(
        drive.token,
        drive.space,
        "bad",
        4,
        "0" * 64,
        manifest(b"abcd"),
        request="wrong-hash",
    )
    with pytest.raises(ValueError):
        drive.put(
            drive.token, session["upload"], manifest(b"abcd")[0]["digest"], b"ABCD"
        )
    drive.put(drive.token, session["upload"], manifest(b"abcd")[0]["digest"], b"abcd")
    with pytest.raises(ValueError, match="file checksum"):
        drive.commit(drive.token, session["upload"])
    assert drive.list_files(drive.token, drive.space) == []


@pytest.mark.parametrize("path", ["FOLDER/FILE.txt", "folder/file.txt/child", "folder"])
def test_portable_path_and_directory_collisions(drive, path):
    upload(drive, drive.token, drive.space)
    with pytest.raises(Conflict):
        upload(drive, drive.token, drive.space, b"abcd", path)


def test_lost_upload_response_reuses_session_and_publishes_once(
    drive, tmp_path, monkeypatch
):
    with network(drive) as url:
        client = DeviceClient(url, drive.token, tmp_path / "device", "one")
        try:
            (client.root / "a").write_bytes(b"abcdefgh")
            original = client.request
            lost = [False]

            def request(method, route, value=None, binary=False):
                response = original(method, route, value, binary)
                if route == "/commit" and not lost[0]:
                    lost[0] = True
                    raise OSError("lost acknowledgement")
                return response

            monkeypatch.setattr(client, "request", request)
            with pytest.raises(OSError):
                client.push(drive.space, "a")
            result = client.push(drive.space, "a")
            assert not result["conflict"]
            assert drive.db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 1
        finally:
            client.close()


def test_download_failure_retains_local_dirty_file_and_cursor(
    drive, tmp_path, monkeypatch
):
    with network(drive) as url:
        upload(drive, drive.token, drive.space, path="a")
        client = DeviceClient(url, drive.token, tmp_path / "device", "one")
        try:
            (client.root / "a").write_bytes(b"unsynchronized")
            original = client.request

            def request(method, route, value=None, binary=False):
                if method == "GET" and route.startswith("/block?"):
                    raise OSError("network down")
                return original(method, route, value, binary)

            monkeypatch.setattr(client, "request", request)
            with pytest.raises(OSError):
                client.pull()
            assert (client.root / "a").read_bytes() == b"unsynchronized"
            assert (
                client.db.execute(
                    "SELECT value FROM state WHERE key='cursor'"
                ).fetchone()[0]
                == "0"
            )
            monkeypatch.setattr(client, "request", original)
            client.pull()
            assert (client.root / "a").read_bytes() == b"abcdefgh"
        finally:
            client.close()


def test_old_event_replay_never_rolls_back_latest_client_revision(drive, tmp_path):
    with network(drive) as url:
        client = DeviceClient(url, drive.token, tmp_path / "device", "one")
        try:
            (client.root / "a").write_bytes(b"abcdefgh")
            client.push(drive.space, "a")
            (client.root / "a").write_bytes(b"abcdijkl")
            latest = client.push(drive.space, "a")
            client.pull()
            assert (client.root / "a").read_bytes() == b"abcdijkl"
            assert client.transferred_down == 0
            assert (
                client.db.execute("SELECT revision FROM files").fetchone()[0]
                == latest["revision"]
            )
        finally:
            client.close()


def test_pruned_events_recover_latest_and_revocation_blocks_download(drive, tmp_path):
    first = upload(drive, drive.token, drive.space)
    upload(drive, drive.token, drive.space, b"ijklmnop", base=first["revision"])
    drive.prune(drive.token, first["file"], 1)
    bob = drive.issue("bob")
    drive.share(drive.token, drive.space, "bob", "read")
    with network(drive) as url:
        owner = DeviceClient(url, drive.token, tmp_path / "owner", "owner")
        reader = DeviceClient(url, bob, tmp_path / "reader", "reader")
        try:
            owner.pull()
            assert (owner.root / "folder/file.txt").read_bytes() == b"ijklmnop"
            reader.pull()
            drive.share(drive.token, drive.space, "bob", "revoke")
            with pytest.raises(HTTPError) as failure:
                reader.request("GET", "/metadata?file=" + first["file"])
            assert failure.value.code == 403
            assert reader.pull()["events"][-1]["kind"] == "revoke"
        finally:
            owner.close()
            reader.close()


def test_device_and_folder_identity_are_protected(drive, tmp_path):
    bob = drive.issue("bob")
    drive.device(drive.token, "alice-device")
    with pytest.raises(PermissionError):
        drive.device(bob, "alice-device")
    with network(drive) as url:
        client = DeviceClient(url, drive.token, tmp_path / "device", "one")
        client.close()
        with pytest.raises(ValueError):
            DeviceClient(url, bob, tmp_path / "device", "one")


def test_partial_upload_survives_service_reopen_with_same_key(drive, tmp_path):
    data = b"abcdefgh"
    pending = drive.begin(
        drive.token,
        drive.space,
        "resume",
        8,
        hashlib.sha256(data).hexdigest(),
        manifest(data),
        request="restart",
    )
    drive.put(drive.token, pending["upload"], manifest(data)[0]["digest"], b"abcd")
    other = Drive(tmp_path / "db.sqlite3", tmp_path / "objects", 4)
    try:
        assert other.upload_status(drive.token, pending["upload"])["missing"] == [
            manifest(data)[1]["digest"]
        ]
        other.put(drive.token, pending["upload"], manifest(data)[1]["digest"], b"efgh")
        result = other.commit(drive.token, pending["upload"])
        assert (
            other.get_block(drive.token, result["file"], result["revision"], 0)
            == b"abcd"
        )
    finally:
        other.close()


def test_read_permission_cannot_confirm_another_users_upload(drive):
    session = drive.begin(
        drive.token,
        drive.space,
        "a",
        4,
        hashlib.sha256(b"abcd").hexdigest(),
        manifest(b"abcd"),
        request="private",
    )
    bob = drive.issue("bob")
    drive.share(drive.token, drive.space, "bob", "write")
    with pytest.raises(PermissionError):
        drive.upload_status(bob, session["upload"])
    with pytest.raises(PermissionError):
        drive.put(bob, session["upload"], manifest(b"abcd")[0]["digest"], b"abcd")


def test_verified_reupload_repairs_corruption_without_losing_references(drive):
    first = upload(drive, drive.token, drive.space, b"abcd", "first")
    block = manifest(b"abcd")[0]
    key = drive.store.key(drive.space, block["digest"])
    drive.store.path(key).write_bytes(b"damaged")
    session = drive.begin(
        drive.token,
        drive.space,
        "repair",
        4,
        block["digest"],
        [block],
        request="repair",
    )
    assert drive.put(drive.token, session["upload"], block["digest"], b"abcd")
    assert drive.get_block(drive.token, first["file"], first["revision"], 0) == b"abcd"
    assert drive.db.execute("SELECT refs FROM blocks").fetchone()[0] == 1
