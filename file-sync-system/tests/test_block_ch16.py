import hashlib
from contextlib import closing
from unicodedata import unidata_version

import pytest
from file_sync import Conflict, CorruptBlock, Drive
from test_sync import drive as drive
from test_sync import manifest, upload


@pytest.mark.parametrize("detected_by", ["download", "commit"])
def test_resume_advertises_only_known_corrupt_block_across_connections(
    drive, tmp_path, detected_by
):
    data = b"abcdwxyzabcd"
    token, space = drive.token, drive.space
    original = upload(drive, token, space, data)
    blocks = manifest(data)
    digest = blocks[0]["digest"]
    pending = drive.begin(
        token,
        space,
        "copy",
        len(data),
        hashlib.sha256(data).hexdigest(),
        blocks,
        request="copy",
    )["upload"]
    drive.store.path(drive.store.key(space, digest)).write_bytes(b"damaged")
    with pytest.raises(CorruptBlock):
        if detected_by == "download":
            drive.get_block(token, original["file"], original["revision"], 0)
        else:
            drive.commit(token, pending)

    other = Drive(tmp_path / "db.sqlite3", tmp_path / "objects", 4)
    try:
        assert other.upload_status(token, pending)["missing"] == [digest]
        assert other.quota("alice")["reserved"] == len(data)
        other.put(token, pending, digest, b"abcd")
        assert drive.upload_status(token, pending)["missing"] == []
        recovered = other.commit(token, pending)
        assert drive.commit(token, pending) == recovered
        assert (
            drive.get_block(token, original["file"], original["revision"], 0) == b"abcd"
        )
        assert other.quota("alice")["used"] == 2 * len(data)
    finally:
        other.close()


def test_wrong_declared_length_does_not_mark_healthy_block_for_repair(drive):
    token, space = drive.token, drive.space
    original = upload(drive, token, space, b"abcd")
    digest = hashlib.sha256(b"abcd").hexdigest()
    wrong = drive.begin(
        token,
        space,
        "wrong",
        1,
        digest,
        [{"size": 1, "digest": digest}],
        request="wrong",
    )
    with pytest.raises((CorruptBlock, ValueError)):
        drive.commit(token, wrong["upload"])
    copy = drive.begin(
        token, space, "copy", 4, digest, manifest(b"abcd"), request="valid"
    )
    assert copy["missing"] == []
    assert drive.get_block(token, original["file"], original["revision"], 0) == b"abcd"


def test_unicode_upgrade_rebuilds_live_path_keys_and_delete_releases_name(
    drive, tmp_path
):
    token, space = drive.token, drive.space
    original = drive.upload(token, space, "Straße/B", b"")
    with drive.db:
        drive.db.execute(
            "UPDATE settings SET value='older' WHERE key='path_unicode_version'"
        )
        drive.db.execute(
            "UPDATE files SET live_path_key='stale' WHERE id=?", (original["file"],)
        )

    with closing(Drive(tmp_path / "db.sqlite3", tmp_path / "objects", 4)) as reopened:
        assert (
            reopened.db.execute(
                "SELECT value FROM settings WHERE key='path_unicode_version'"
            ).fetchone()[0]
            == unidata_version
        )
        for collision in ("STRASSE", "STRASSE/b", "STRASSE/b/child"):
            with pytest.raises(Conflict):
                reopened.upload(token, space, collision, b"")
        reopened.delete(token, original["file"], original["revision"])
        assert reopened.upload(token, space, "STRASSE", b"")["path"] == "STRASSE"
