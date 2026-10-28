"""Cross-instance policies, atomic acceptance, cache isolation and close ownership."""

from concurrent.futures import ThreadPoolExecutor
import dataclasses
import os
import threading

import pytest

from video_streaming import Pipeline, VideoService, Worker
from test_video import FakeMedia, drain, uploaded


@pytest.fixture
def service(tmp_path):
    now = [100.0]
    instance = VideoService(
        tmp_path / "metadata.sqlite3",
        tmp_path / "objects",
        profiles=(144,),
        clock=lambda: now[0],
    )
    instance.test_clock = now
    try:
        yield instance
    finally:
        instance.close()


def test_pipeline_is_frozen_and_input_alias_is_copied():
    profiles = [144]
    pipeline = Pipeline(profiles, thumbnail=False, preprocess=True)
    profiles.append(240)
    assert pipeline.profiles == (144,)
    assert [task.name for task in pipeline.tasks()] == [
        "inspect",
        "preprocess",
        "encode-144",
        "publish",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        pipeline.thumbnail = True


@pytest.mark.parametrize(
    "config",
    [
        None,
        [],
        {"unknown": True},
        {"profiles": []},
        {"profiles": [144, 144]},
        {"profiles": [144.0]},
        {"profiles": [True]},
        {"profiles": [999]},
        {"thumbnail": 1},
        {"preprocess": "true"},
    ],
)
def test_invalid_pipeline_config(config):
    with pytest.raises(ValueError):
        Pipeline.from_config(config)


def test_explicit_finalization_retry_cannot_change_dag(service):
    token, upload = uploaded(service, finish=False)
    plan = Pipeline((144,), thumbnail=False)
    service.finish(token, upload["video_id"], pipeline=plan)
    service.finish(token, upload["video_id"], pipeline=plan)
    with pytest.raises(ValueError, match="pipeline"):
        service.finish(token, upload["video_id"], pipeline=Pipeline((240,)))
    assert service.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3


def test_metadata_updates_do_not_replace_processing_state(service):
    token, upload = uploaded(service, finish=False)
    video = upload["video_id"]
    assert (
        service.update_metadata(token, video, "Uploaded title")["state"] == "UPLOADING"
    )
    service.finish(token, video)
    assert (
        service.update_metadata(token, video, "Encoding title")["state"] == "PROCESSING"
    )
    drain(service)
    assert service.update_metadata(token, video, "Ready title")["state"] == "READY"
    with pytest.raises(PermissionError):
        service.update_metadata(service.issue("bob"), video, "Wrong owner")
    service.remove(token, video)
    with pytest.raises(ValueError):
        service.update_metadata(token, video, "Removed title")


def test_slow_part_write_does_not_block_other_metadata(service, monkeypatch):
    token = service.issue("alice")
    upload = service.create(token, "Video", 3)
    entered, release = threading.Event(), threading.Event()
    original = service.objects.put

    def slow(data):
        entered.set()
        assert release.wait(3)
        return original(data)

    monkeypatch.setattr(service.objects, "put", slow)
    with ThreadPoolExecutor(2) as pool:
        write = pool.submit(service.put_part, upload["parts"][0], b"abc")
        try:
            assert entered.wait(2)
            assert (
                pool.submit(service.status, token, upload["video_id"]).result(1)[
                    "state"
                ]
                == "UPLOADING"
            )
        finally:
            release.set()
        assert write.result(2)


def test_gc_between_object_write_and_reference_rejects_acceptance(service, monkeypatch):
    token = service.issue("alice")
    upload = service.create(token, "Video", 3)
    original = service.objects.put

    def swept(data):
        key = original(data)
        service.objects.file(key).unlink()
        return key

    monkeypatch.setattr(service.objects, "put", swept)
    with pytest.raises(OSError):
        service.put_part(upload["parts"][0], b"abc")
    assert service.status(token, upload["video_id"])["uploaded_parts"] == []


def test_source_removed_during_finalization_cannot_enqueue(service, monkeypatch):
    token, upload = uploaded(service, finish=False)
    original = service.objects.assemble

    def swept(keys):
        key, size = original(keys)
        service.objects.file(key).unlink()
        return key, size

    monkeypatch.setattr(service.objects, "assemble", swept)
    with pytest.raises(OSError):
        service.finish(token, upload["video_id"])
    assert service.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_lease_cannot_complete_after_slow_output_validation(service, monkeypatch):
    _token, _upload = uploaded(service)
    assert service.complete(service.claim("inspector"), {})
    job = service.claim("encoder", 1, kinds={"encode"})
    key = service.objects.put(b"placeholder")
    original = service.objects.file

    class Delayed:
        def is_file(self):
            service.test_clock[0] += 2
            return True

    monkeypatch.setattr(service.objects, "file", lambda _key: Delayed())
    assert not service.complete(
        job, {"144/index.m3u8": key, "144/segment_00000.ts": key}
    )
    monkeypatch.setattr(service.objects, "file", original)
    assert (
        service.db.execute(
            "SELECT state FROM jobs WHERE id=?", (job["id"],)
        ).fetchone()[0]
        == "RUNNING"
    )


def test_first_claim_attempt_policy_survives_other_worker_limits(service):
    _token, _upload = uploaded(service)
    service.complete(service.claim("inspector"), {})
    encode = service.claim("encoder", 1, max_attempts=3, kinds={"encode"})
    service.test_clock[0] += 2
    thumbnail = service.claim("thumbnail", max_attempts=1, kinds={"thumbnail"})
    assert thumbnail is not None
    retry = service.claim("different-encoder", max_attempts=1, kinds={"encode"})
    assert retry["id"] == encode["id"] and retry["max_attempts"] == 3
    assert service.fail(retry, "temporary", max_attempts=1)
    assert service.db.execute("SELECT state FROM videos").fetchone()[0] == "PROCESSING"


def test_claim_expiry_uses_time_after_lock_wait(service):
    uploaded(service)
    entered = threading.Event()

    def claim():
        entered.set()
        return service.claim("waiting", lease_seconds=1)

    with ThreadPoolExecutor(1) as pool:
        with service.lock:
            pending = pool.submit(claim)
            assert entered.wait(1)
            service.test_clock[0] += 50
        assert pending.result(2)["expires"] == 151


def test_ready_task_beyond_blocked_priority_page_is_not_starved(service):
    _token, ready = uploaded(service)
    service.complete(service.claim("inspector"), {})
    for _ in range(65):
        token, upload = uploaded(service, b"a", finish=False)
        service.finish(
            token, upload["video_id"], priority=99, pipeline=Pipeline((144, 240))
        )
    selected = service.claim("encoder", kinds={"encode"})
    assert selected["video"] == ready["video_id"]


def test_tampered_job_attempt_count_cannot_cancel_video(service):
    _token, upload = uploaded(service)
    job = service.claim("worker", max_attempts=3)
    job["attempts"] = 999
    assert service.fail(job, "temporary")
    assert (
        service.db.execute(
            "SELECT state FROM videos WHERE id=?", (upload["video_id"],)
        ).fetchone()[0]
        == "PROCESSING"
    )


def test_invalid_encode_output_does_not_become_done(service):
    uploaded(service)
    service.complete(service.claim("inspector"), {})
    job = service.claim("encoder", kinds={"encode"})
    with pytest.raises(ValueError, match="plan"):
        service.complete(job, {"wrong-name": service.objects.put(b"random")})


def test_cold_object_read_does_not_block_metadata_or_close_early(service):
    token, upload = uploaded(service)
    entered, release, closing = threading.Event(), threading.Event(), threading.Event()
    path = service.objects.file(service.objects.put(b"cold cache"))

    class SlowPath:
        name = path.name

        def is_file(self):
            return True

        def read_bytes(self):
            entered.set()
            assert release.wait(3)
            return path.read_bytes()

    def close():
        service.close()
        closing.set()

    with ThreadPoolExecutor(3) as pool:
        pending = pool.submit(service.read_small, SlowPath())
        try:
            assert entered.wait(1)
            assert (
                pool.submit(service.status, token, upload["video_id"]).result(1)[
                    "state"
                ]
                == "PROCESSING"
            )
            shutdown = pool.submit(close)
            assert not closing.wait(0.05)
        finally:
            release.set()
        assert pending.result(2) == b"cold cache"
        shutdown.result(2)
    with pytest.raises(RuntimeError):
        service.issue("late")


def test_completion_object_checks_do_not_block_metadata_and_remove_wins(
    service, monkeypatch
):
    token, upload = uploaded(service)
    drain(service, handle_completion=False)
    entered, release = threading.Event(), threading.Event()
    original = service.objects.file

    class Slow:
        def is_file(self):
            entered.set()
            assert release.wait(3)
            return True

    monkeypatch.setattr(service.objects, "file", lambda _key: Slow())
    with ThreadPoolExecutor(2) as pool:
        pending = pool.submit(service.handle_completions)
        try:
            assert entered.wait(1)
            pool.submit(service.remove, token, upload["video_id"]).result(1)
        finally:
            release.set()
        assert pending.result(2) == 1
    monkeypatch.setattr(service.objects, "file", original)
    assert service.status(token, upload["video_id"])["state"] == "REMOVED"


def test_cleanup_releases_completed_gop_intermediates_but_retains_playback(service):
    class Prepared(FakeMedia):
        def preprocess(self, source, folder):
            path = folder / "gop_00000.mkv"
            path.write_bytes(b"temporary GOP")
            return [path]

    token, upload = uploaded(service, finish=False)
    service.finish(
        token, upload["video_id"], pipeline=Pipeline((144,), preprocess=True)
    )
    Worker(service, Prepared()).run(once=True)
    intermediate = next(
        iter(service.outputs(upload["video_id"])["preprocess"].values())
    )
    path = service.objects.file(intermediate)
    os.utime(path, (1, 1))
    service.cleanup()
    assert not path.exists()
    assert service.asset(
        service.playback(token, upload["video_id"]), "master.m3u8"
    ).is_file()


def test_sweep_failure_does_not_rollback_expiry_after_object_deletion(
    service, monkeypatch
):
    token = service.issue("alice")
    upload = service.create(token, "Incomplete", 6, part_size=3)
    service.put_part(upload["parts"][0], b"abc")
    service.test_clock[0] += 90000

    def failed_sweep(_references, _cutoff):
        assert service.db.execute("SELECT state FROM videos").fetchone()[0] == "EXPIRED"
        raise OSError("storage unavailable")

    monkeypatch.setattr(service.objects, "sweep", failed_sweep, raising=False)
    with pytest.raises(OSError):
        service.cleanup()
    assert service.status(token, upload["video_id"])["state"] == "EXPIRED"
    assert service.status(token, upload["video_id"])["uploaded_parts"] == []
