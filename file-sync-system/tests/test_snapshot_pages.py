"""An offline device must finish recovery across bounded pages and concurrent edits."""

import pytest
from file_sync.client import DeviceClient
from test_sync import drive as drive
from test_sync import network, upload


def test_snapshot_resumes_from_first_watermark_after_concurrent_edit(
    drive, tmp_path, monkeypatch
):
    first = upload(drive, drive.token, drive.space, b"old", "a")
    upload(drive, drive.token, drive.space, b"two", "b")
    with network(drive) as url:
        device = DeviceClient(url, drive.token, tmp_path / "device", "reader")
        try:
            device.bind_space(drive.space)
            send = device.request
            pages = []

            def interleave(method, route, value=None, **kwargs):
                if route != "/snapshot":
                    return send(method, route, value, **kwargs)
                page = send(method, route, {**value, "limit": 1}, **kwargs)
                pages.append(page)
                if len(pages) == 1:
                    upload(
                        drive, drive.token, drive.space, b"new", "a", first["revision"]
                    )
                return page

            monkeypatch.setattr(device, "request", interleave)
            recovered = device.resnapshot(0)
            assert len(pages) == 2 and recovered["cursor"] == pages[0]["cursor"]
            assert (device.root / "b").read_bytes() == b"two"
            device.pull()
            assert (device.root / "a").read_bytes() == b"new"
        finally:
            device.close()


def test_page_delivery_only_advances_at_end_and_rechecks_membership(drive):
    bob = drive.issue("bob")
    drive.device(bob, "reader")
    for path in ("a", "b"):
        upload(drive, drive.token, drive.space, b"data", path)
    drive.share(drive.token, drive.space, "bob", "read")
    page = drive.snapshot(bob, "reader", drive.space, limit=1)
    with pytest.raises(ValueError, match="not delivered"):
        drive.ack(bob, "reader", page["cursor"])
    drive.share(drive.token, drive.space, "bob", "revoke")
    with pytest.raises(PermissionError):
        drive.snapshot(
            bob,
            "reader",
            drive.space,
            limit=1,
            after=page["next"],
            cursor=page["cursor"],
        )
