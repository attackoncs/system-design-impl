"""Reconcile file tombstones with the current state of a device directory."""

from contextlib import closing

import pytest
from file_sync.client import DeviceClient
from test_sync import drive as drive
from test_sync import network, upload


@pytest.fixture
def device(drive, tmp_path):
    with network(drive) as url:
        with closing(
            DeviceClient(url, drive.token, tmp_path / "device", "one")
        ) as client:
            yield client


def test_snapshot_tombstone_leaves_local_directory_in_place(drive, device):
    parent = upload(drive, drive.token, drive.space, b"old", "notes")
    drive.delete(drive.token, parent["file"], parent["revision"])
    upload(drive, drive.token, drive.space, b"new", "notes/remote")
    folder = device.root / "notes"
    folder.mkdir()
    (folder / "draft").write_bytes(b"unpublished")
    device.resnapshot(0)
    assert (folder / "draft").read_bytes() == b"unpublished"
    assert (folder / "remote").read_bytes() == b"new"


def test_snapshot_child_tombstone_below_live_file_converges(drive, device):
    child = upload(drive, drive.token, drive.space, b"old", "notes/draft")
    drive.delete(drive.token, child["file"], child["revision"])
    upload(drive, drive.token, drive.space, b"new", "notes")
    result = device.resnapshot(0)
    assert (device.root / "notes").read_bytes() == b"new"
    assert device.pull()["cursor"] == result["cursor"]


def test_tombstone_preserves_locally_truncated_file(drive, device):
    first = upload(drive, drive.token, drive.space, b"original", "notes")
    device.pull()
    (device.root / "notes").write_bytes(b"")
    drive.delete(drive.token, first["file"], first["revision"])
    device.pull()
    assert not (device.root / "notes").exists()
    assert [p.read_bytes() for p in device.root.glob("notes.local-conflict-*")] == [b""]
