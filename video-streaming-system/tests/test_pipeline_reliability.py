"""Owned media processes, renewed tasks and bounded parallel DAG execution."""

import concurrent.futures
import subprocess
import threading
import urllib.parse

import pytest

from video_streaming import FFmpegMedia, InvalidMedia, VideoService, Worker
from video_streaming.media import MediaCancelled
import video_streaming.media as media_module
import video_streaming.worker as worker_module
from test_video import FakeMedia, running, uploaded


@pytest.fixture
def service(tmp_path):
    value = VideoService(
        tmp_path / "metadata.sqlite3", tmp_path / "objects", profiles=(144, 240)
    )
    try:
        yield value
    finally:
        value.close()


@pytest.mark.parametrize(
    "options",
    [
        {"lease": True},
        {"lease": float("nan")},
        {"lease": float("inf")},
        {"max_attempts": True},
        {"max_attempts": 0},
        {"retry_base": -1},
        {"concurrency": True},
        {"concurrency": 0},
        {"concurrency": 33},
        {"kinds": []},
        {"kinds": ["unknown"]},
        {"kinds": "encode"},
        {"name": ""},
    ],
)
def test_worker_rejects_invalid_resource_configuration(service, options):
    with pytest.raises(ValueError):
        Worker(service, FakeMedia(), **options)


def test_source_download_failure_cleans_attempt_folder(service, monkeypatch):
    uploaded(service)
    worker = Worker(service, FakeMedia())
    job = service.claim("worker")

    def missing(_key):
        raise OSError("source download failed")

    monkeypatch.setattr(service.objects, "file", missing)
    with pytest.raises(OSError, match="source download"):
        worker.execute(job)
    assert not (worker.work / job["video"] / job["lease"]).exists()


def test_cleanup_failure_does_not_hide_original_processing_error(service, monkeypatch):
    class Broken(FakeMedia):
        def inspect(self, *args):
            raise ValueError("original processing error")

    uploaded(service)
    worker = Worker(service, Broken())
    job = service.claim("worker")

    def cannot_remove(_folder):
        raise PermissionError("cleanup denied")

    monkeypatch.setattr(worker_module.shutil, "rmtree", cannot_remove)
    with pytest.raises(ValueError, match="original processing"):
        worker.execute(job)


def test_long_running_task_renews_before_competing_worker_reclaims(
    service, monkeypatch
):
    entered, release, renewed = threading.Event(), threading.Event(), threading.Event()
    token, upload = uploaded(service)
    original, count = service.renew, []

    def observe(job, lease):
        result = original(job, lease)
        count.append(result)
        if len(count) >= 3:
            renewed.set()
        return result

    class Slow(FakeMedia):
        def inspect(self, source, folder):
            entered.set()
            assert release.wait(3)
            return super().inspect(source, folder)

    monkeypatch.setattr(service, "renew", observe)
    worker = Worker(service, Slow(), lease=0.15)
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        attempt = pool.submit(worker.run_once)
        try:
            assert entered.wait(1)
            assert renewed.wait(2)
            assert all(count)
            assert service.claim("competitor", 0.15) is None
        finally:
            release.set()
        assert attempt.result(2)
    assert service.status(token, upload["video_id"])["state"] == "PROCESSING"
    assert service.outputs(upload["video_id"])["inspect"]["adapter"] == "fake-test-only"


def test_bounded_pool_executes_independent_stages_concurrently(service):
    reached, release = threading.Event(), threading.Event()
    lock, activity = threading.Lock(), [0, 0]

    class Parallel(FakeMedia):
        def _enter(self):
            with lock:
                activity[0] += 1
                activity[1] = max(activity[1], activity[0])
                if activity[0] == 2:
                    reached.set()
            assert release.wait(3)

        def _exit(self):
            with lock:
                activity[0] -= 1

        def thumbnail(self, *args):
            self._enter()
            try:
                return super().thumbnail(*args)
            finally:
                self._exit()

        def encode(self, *args):
            self._enter()
            try:
                return super().encode(*args)
            finally:
                self._exit()

    token, upload = uploaded(service)
    worker = Worker(service, Parallel(), concurrency=2)
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        task = pool.submit(worker.run, True)
        try:
            assert reached.wait(2)
            assert activity[1] == 2
        finally:
            release.set()
        task.result(3)
    assert activity == [0, 2]
    assert service.status(token, upload["video_id"])["state"] == "READY"


def test_stop_drains_current_task_without_claiming_next_stage(service):
    entered, release, stop = threading.Event(), threading.Event(), threading.Event()

    class Slow(FakeMedia):
        def inspect(self, source, folder):
            entered.set()
            assert release.wait(3)
            return super().inspect(source, folder)

    uploaded(service)
    worker = Worker(service, Slow(), concurrency=2)
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        task = pool.submit(worker.run, False, stop)
        try:
            assert entered.wait(2)
            stop.set()
            assert not task.done()
        finally:
            release.set()
        task.result(3)
    assert (
        service.db.execute(
            "SELECT COUNT(*) FROM jobs WHERE state='RUNNING'"
        ).fetchone()[0]
        == 0
    )
    assert (
        service.db.execute("SELECT COUNT(*) FROM jobs WHERE state='DONE'").fetchone()[0]
        == 1
    )


def test_capability_limited_worker_only_claims_supported_tasks(service):
    token, upload = uploaded(service)
    assert Worker(service, FakeMedia()).run_once()
    Worker(service, FakeMedia(), concurrency=2, kinds={"encode"}).run(True)
    assert set(service.outputs(upload["video_id"])) == {
        "inspect",
        "encode-144",
        "encode-240",
    }
    Worker(service, FakeMedia()).run(True)
    assert service.status(token, upload["video_id"])["state"] == "READY"


def test_worker_never_publishes_output_after_renewal_loses_ownership(
    service, monkeypatch
):
    class Lost(FakeMedia):
        def operation(self, cancelled):
            self.cancelled = cancelled
            return worker_module.contextlib.nullcontext()

        def inspect(self, source, folder):
            assert self.cancelled.wait(2)
            return super().inspect(source, folder)

    _token, upload = uploaded(service)
    monkeypatch.setattr(service, "renew", lambda *_args: False)
    assert Worker(service, Lost(), lease=0.09).run_once()
    assert "inspect" not in service.outputs(upload["video_id"])


def test_inspection_rejects_corruption_after_a_valid_first_frame(tmp_path):
    media = FFmpegMedia()
    fixture = tmp_path / "corrupted.mp4"
    media.run(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=30",
            "-t",
            "3",
            "-c:v",
            "libx264",
            "-threads",
            "2",
            str(fixture),
        ]
    )
    data = bytearray(fixture.read_bytes())
    start, end = data.index(b"mdat") + 4, data.index(b"moov") - 4
    damaged = start + (end - start) * 2 // 3
    data[damaged:end] = b"\xff" * (end - damaged)
    fixture.write_bytes(data)
    # The old first-frame-only inspection accepted this valid container prefix.
    media.run(media.input(fixture) + ["-frames:v", "1", "-f", "null", "-"], fatal=True)
    with pytest.raises(InvalidMedia):
        media.inspect(fixture, tmp_path)


@pytest.mark.parametrize("timeout", [True, 0, -1, float("nan"), float("inf"), 10**1000])
def test_media_timeout_is_strict(timeout):
    with pytest.raises(ValueError):
        FFmpegMedia(timeout=timeout)


@pytest.mark.parametrize("cancel", [False, True])
def test_media_timeout_or_lease_loss_kills_and_joins_child(monkeypatch, cancel):
    media = FFmpegMedia(timeout=5 if cancel else 0.15)
    original, children = media_module.subprocess.Popen, []

    def start(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(media_module.subprocess, "Popen", start)
    cancelled = threading.Event()
    timer = threading.Timer(0.15, cancelled.set)
    try:
        if cancel:
            timer.start()
        with media.operation(cancelled):
            with pytest.raises(MediaCancelled if cancel else subprocess.TimeoutExpired):
                media.run(
                    [
                        "-f",
                        "lavfi",
                        "-i",
                        "testsrc2=size=32x32:rate=30",
                        "-f",
                        "null",
                        "-",
                    ]
                )
    finally:
        timer.cancel()
        if cancel:
            timer.join()
    assert len(children) == 1 and children[0].poll() is not None


def test_real_gop_chunks_are_reusable_for_later_encoding(tmp_path):
    media = FFmpegMedia()
    # Include an apostrophe in the store path to exercise ffconcat path escaping.
    root = tmp_path / "owner's media"
    root.mkdir()
    service = VideoService(
        root / "metadata.sqlite3",
        root / "objects",
        pipeline={"profiles": [144], "preprocess": True, "thumbnail": False},
    )
    fixture = tmp_path / "source.mp4"
    media.run(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=30",
            "-t",
            "5",
            "-c:v",
            "libx264",
            "-threads",
            "2",
            "-g",
            "30",
            "-sc_threshold",
            "0",
            str(fixture),
        ]
    )
    try:
        token, upload = uploaded(service, fixture.read_bytes())
        worker = Worker(service, media)
        assert worker.run_once()  # Inspect the complete original source.
        assert worker.run_once()  # Persist the keyframe-aligned intermediates.
        prepared = service.outputs(upload["video_id"])["preprocess"]
        assert len(prepared) >= 2
        # The encoding attempt consumes durable chunks, even if the original
        # source is unavailable; no in-memory preprocessor object is required.
        source = service.db.execute(
            "SELECT source FROM videos WHERE id=?", (upload["video_id"],)
        ).fetchone()[0]
        service.objects.file(source).unlink()
        service.close()
        # Different startup defaults cannot change the already persisted plan.
        service = VideoService(
            root / "metadata.sqlite3", root / "objects", profiles=(240,)
        )
        Worker(service, media, concurrency=2).run(True)
        assert service.status(token, upload["video_id"])["state"] == "READY"
        grant = service.playback(token, upload["video_id"])
        playlist = service.asset(grant, "144/index.m3u8").read_bytes().decode()
        assert (
            4.8
            < sum(
                float(line.split(":")[1].rstrip(","))
                for line in playlist.splitlines()
                if line.startswith("#EXTINF:")
            )
            < 5.3
        )
        with running(service) as endpoint:
            url = (
                endpoint
                + "/media?"
                + urllib.parse.urlencode(
                    {
                        "grant": grant,
                        "resource": "144/index.m3u8",
                    }
                )
            )
            media.run(
                [
                    "-protocol_whitelist",
                    "file,http,tcp,crypto",
                    "-allowed_extensions",
                    "ALL",
                    "-i",
                    url,
                    "-f",
                    "null",
                    "-",
                ]
            )
    finally:
        service.close()
