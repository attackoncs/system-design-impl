"""Published assets must still match their content-addressed object keys."""

import pytest
from test_storage_reliability import S3Client
from test_video import FakeMedia, drain, uploaded
from video_streaming import Pipeline, S3Objects, VideoService


class DistinctMedia(FakeMedia):
    def encode(self, source, folder, profile, key):
        files = super().encode(source, folder, profile, key)
        files[1].write_bytes(source.read_bytes() + b"-segment")
        return files


@pytest.mark.parametrize("resource", ["master.m3u8", "144/segment_00000.ts"])
def test_outbox_waits_for_corrupt_asset_repair(tmp_path, resource):
    service = VideoService(
        tmp_path / "metadata.sqlite3",
        tmp_path / "objects",
        profiles=(144,),
    )
    try:
        token, upload = uploaded(service)
        video = upload["video_id"]
        drain(service, FakeMedia(), handle_completion=False)
        key = service.outputs(video)["publish"][resource]
        path = service.objects.file(key)
        original = path.read_bytes()
        path.write_bytes(b"corrupted object")

        assert service.handle_completions() == 0
        assert service.status(token, video)["state"] == "PROCESSING"
        assert (
            service.db.execute(
                "SELECT handled FROM completions WHERE video=?", (video,)
            ).fetchone()[0]
            == 0
        )

        assert service.objects.put(original) == key
        assert service.handle_completions() == 1
        assert service.status(token, video)["state"] == "READY"
        assert (
            service.asset(service.playback(token, video), resource).read_bytes()
            == original
        )
    finally:
        service.close()


@pytest.mark.parametrize("resource", ["master.m3u8", "144/segment_00000.ts"])
def test_corrupt_remote_asset_does_not_block_healthy_completion(tmp_path, resource):
    client = S3Client([])
    store = S3Objects(tmp_path / "objects", "private", client=client)
    service = VideoService(tmp_path / "metadata.sqlite3", store, profiles=(144,))
    try:
        bad_token, bad_upload = uploaded(service, b"first-source", finish=False)
        service.finish(
            bad_token,
            bad_upload["video_id"],
            pipeline=Pipeline((144,), thumbnail=False),
        )
        good_token, good_upload = uploaded(service, b"second-source", finish=False)
        service.finish(
            good_token,
            good_upload["video_id"],
            pipeline=Pipeline((240,), thumbnail=False),
        )
        drain(service, DistinctMedia(), handle_completion=False)

        bad_video, good_video = bad_upload["video_id"], good_upload["video_id"]
        key = service.outputs(bad_video)["publish"][resource]
        original = client.values["video/" + key]
        client.values["video/" + key] = b"corrupted remote body"
        (store.root / key).unlink()

        assert service.handle_completions() == 1
        assert service.status(bad_token, bad_video)["state"] == "PROCESSING"
        assert service.status(good_token, good_video)["state"] == "READY"
        assert (
            service.db.execute(
                "SELECT handled FROM completions WHERE video=?", (bad_video,)
            ).fetchone()[0]
            == 0
        )

        client.values["video/" + key] = original
        assert service.handle_completions() == 1
        assert service.status(bad_token, bad_video)["state"] == "READY"
        assert (
            service.asset(service.playback(bad_token, bad_video), resource).read_bytes()
            == original
        )
    finally:
        service.close()


def test_outbox_propagates_remote_storage_outage(tmp_path, monkeypatch):
    client = S3Client([])
    store = S3Objects(tmp_path / "objects", "private", client=client)
    service = VideoService(tmp_path / "metadata.sqlite3", store, profiles=(144,))
    try:
        token, upload = uploaded(service)
        drain(service, DistinctMedia(), handle_completion=False)

        def offline(**_kwargs):
            raise ConnectionError("storage offline")

        monkeypatch.setattr(client, "head_object", offline)
        with pytest.raises(OSError, match="object storage unavailable"):
            service.handle_completions()
        assert service.status(token, upload["video_id"])["state"] == "PROCESSING"
    finally:
        service.close()
