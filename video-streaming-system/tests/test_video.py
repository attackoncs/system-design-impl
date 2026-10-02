from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from threading import Thread
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from video_streaming import FFmpegMedia, InvalidMedia, VideoService, Worker
from video_streaming.http import byte_range, server


class FakeMedia:
    """Deterministic task simulator: its segments are deliberately not playable."""
    timeout = 0

    def inspect(self, source, folder):
        if source.read_bytes().startswith(b"invalid"):
            raise InvalidMedia("fixture is malformed")
        return {"adapter": "fake-test-only"}

    def thumbnail(self, source, folder):
        path = folder / "thumbnail.png"
        path.write_bytes(b"test-thumbnail")
        return path

    def encode(self, source, folder, profile, key):
        playlist, segment = folder / "index.m3u8", folder / "segment_00000.ts"
        playlist.write_text('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="../key.bin"\n#EXTINF:2,\nsegment_00000.ts\n#EXT-X-ENDLIST\n')
        segment.write_bytes(b"fake-segment-not-media")
        return [playlist, segment]


@pytest.fixture
def service(tmp_path):
    now = [100.0]
    value = VideoService(tmp_path / "metadata.sqlite3", tmp_path / "objects", profiles=(144, 240), clock=lambda: now[0])
    value.test_clock = now
    yield value
    value.close()


def uploaded(service, data=b"original-video", finish=True):
    token = service.issue("alice")
    part_size = max(4, (len(data) + 127) // 128)
    upload = service.create(token, "Example", len(data), part_size=part_size)
    for i, grant in enumerate(upload["parts"]):
        service.put_part(grant, data[i * part_size:(i + 1) * part_size])
    if finish:
        service.finish(token, upload["video_id"], hashlib.sha256(data).hexdigest())
    return token, upload


def drain(service, media=None, handle_completion=True):
    worker = Worker(service, media or FakeMedia())
    while worker.run_once(handle_completion):
        pass


def test_resumable_idempotent_parts_and_finalization(service):
    token = service.issue("alice")
    upload = service.create(token, "Sample", 7, part_size=4)
    video = upload["video_id"]
    assert service.put_part(upload["parts"][1], b"efg")
    assert service.status(token, video)["uploaded_parts"] == [1]
    with pytest.raises(ValueError):
        service.finish(token, video)
    service.put_part(upload["parts"][0], b"abcd")
    assert not service.put_part(upload["parts"][0], b"abcd")
    with pytest.raises(ValueError):
        service.put_part(upload["parts"][0], b"xxxx")
    with pytest.raises(ValueError):
        service.finish(token, video, "0" * 64)
    assert service.finish(token, video) == video
    assert service.finish(token, video) == video
    assert service.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 5
    drain(service)
    assert service.status(token, video)["state"] == "READY"


@pytest.mark.parametrize("size,part", [(0, 4), (-1, 4), (True, 4), (1024 ** 3 + 1, 4), (1, 0), (1, 8 * 1024 ** 2 + 1), (5000, 1)])
def test_invalid_upload_bounds(service, size, part):
    with pytest.raises(ValueError):
        service.create(service.issue("alice"), "Sample", size, part)


def test_grant_expiry_signature_and_ownership(service):
    alice, bob = service.issue("alice"), service.issue("bob")
    upload = service.create(alice, "Example", 1, ttl=1)
    with pytest.raises(PermissionError):
        service.put_part(upload["parts"][0] + "x", b"x")
    with pytest.raises(PermissionError):
        service.status(bob, upload["video_id"])
    with pytest.raises(PermissionError):
        service.finish(bob, upload["video_id"])
    service.test_clock[0] += 2
    with pytest.raises(PermissionError):
        service.put_part(upload["parts"][0], b"x")
    renewed = service.upload_grants(alice, upload["video_id"])
    assert service.put_part(renewed["parts"][0], b"x")


def test_part_size_and_checksum_checks(service):
    upload = service.create(service.issue("alice"), "Example", 4)
    with pytest.raises(ValueError):
        service.put_part(upload["parts"][0], b"abc")
    with pytest.raises(ValueError):
        service.put_part(upload["parts"][0], b"abcd", "0" * 64)


def test_dependencies_concurrent_claims_and_lease_fencing(service):
    token, upload = uploaded(service)
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = list(pool.map(lambda i: service.claim(str(i), 2), range(8)))
    claimed = [job for job in jobs if job]
    assert len(claimed) == 1 and claimed[0]["name"] == "inspect"
    old = claimed[0]
    service.test_clock[0] += 3
    new = service.claim("replacement", 2)
    assert new["id"] == old["id"] and new["lease"] != old["lease"]
    assert not service.complete(old, {})
    assert not service.fail(old, "late failure", fatal=True)
    assert service.complete(new, {})
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = [job for job in pool.map(lambda i: service.claim(str(i), 2), range(8)) if job]
    assert len(jobs) == 3 and {job["kind"] for job in jobs} == {"thumbnail", "encode"}
    assert service.status(token, upload["video_id"])["state"] == "PROCESSING"


def test_expired_attempts_exhaust_and_cancel_dependents(service):
    token, upload = uploaded(service)
    for _ in range(3):
        assert service.claim("worker", 1)["name"] == "inspect"
        service.test_clock[0] += 2
    assert service.claim("replacement", 1) is None
    assert service.status(token, upload["video_id"])["state"] == "FAILED"


def test_recoverable_retry_and_permanent_failure(service):
    class FailingMedia(FakeMedia):
        def encode(self, *args):
            raise RuntimeError("transient encoder unavailable")
    token, upload = uploaded(service)
    worker = Worker(service, FailingMedia(), retry_base=1)
    for _ in range(20):
        worker.run_once()
        service.test_clock[0] += 10
    assert service.status(token, upload["video_id"])["state"] == "FAILED"
    failed = service.db.execute("SELECT attempts FROM jobs WHERE state='FAILED'").fetchone()
    assert failed[0] == 3
    token, upload = uploaded(service, b"invalid-video")
    drain(service)
    assert service.status(token, upload["video_id"])["state"] == "FAILED"
    assert service.db.execute("SELECT attempts FROM jobs WHERE video=? AND name='inspect'", (upload["video_id"],)).fetchone()[0] == 1


def test_completion_outbox_restart(tmp_path):
    path, objects = tmp_path / "meta.sqlite3", tmp_path / "objects"
    first = VideoService(path, objects, profiles=(144,))
    token, upload = uploaded(first)
    drain(first, handle_completion=False)
    assert first.status(token, upload["video_id"])["state"] == "PROCESSING"
    first.close()
    second = VideoService(path, objects)
    try:
        assert second.handle_completions() == 1
        assert second.handle_completions() == 0
        assert second.status(token, upload["video_id"])["state"] == "READY"
        grant = second.playback(token, upload["video_id"])
        assert b"144/index.m3u8" in second.asset(grant, "master.m3u8").read_bytes()
    finally:
        second.close()


def test_takedown_overrides_cache_and_pending_completion(service):
    token, upload = uploaded(service)
    drain(service)
    grant = service.playback(token, upload["video_id"])
    service.read_small(service.asset(grant, "master.m3u8"))
    assert service.cache
    service.remove(token, upload["video_id"])
    with pytest.raises(PermissionError):
        service.asset(grant, "master.m3u8")
    token, upload = uploaded(service)
    drain(service, handle_completion=False)
    service.remove(token, upload["video_id"])
    service.handle_completions()
    assert service.status(token, upload["video_id"])["state"] == "REMOVED"


def test_cleanup_protects_references_and_active_work(service):
    token, upload = uploaded(service)
    drain(service)
    orphan = service.objects.file(service.objects.put(b"old-orphan"))
    young = service.objects.file(service.objects.put(b"young-orphan"))
    old_time = time.time() - 4000
    os.utime(orphan, (old_time, old_time))
    for path in service.objects.root.iterdir():
        if path != young:
            os.utime(path, (old_time, old_time))
    result = service.cleanup()
    assert result["removed_objects"] == 1 and not orphan.exists() and young.exists()
    grant = service.playback(token, upload["video_id"])
    assert service.asset(grant, "master.m3u8").exists()


def test_metadata_failure_rolls_back_scheduling(service):
    token, upload = uploaded(service, finish=False)
    service.db.executescript("""CREATE TRIGGER fail_publish BEFORE UPDATE OF state ON videos
        WHEN NEW.state='PROCESSING' BEGIN SELECT RAISE(ABORT,'simulated metadata failure'); END;""")
    import sqlite3
    with pytest.raises(sqlite3.DatabaseError):
        service.finish(token, upload["video_id"])
    assert service.status(token, upload["video_id"])["state"] == "UPLOADING"
    assert service.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    service.db.execute("DROP TRIGGER fail_publish")
    service.db.commit()
    service.finish(token, upload["video_id"])
    drain(service)
    assert service.status(token, upload["video_id"])["state"] == "READY"


def test_completion_waits_for_missing_object_recovery(service):
    token, upload = uploaded(service)
    drain(service, handle_completion=False)
    assets = service.outputs(upload["video_id"])["publish"]
    path = service.objects.file(assets["master.m3u8"])
    saved = path.read_bytes()
    path.unlink()
    assert service.handle_completions() == 0
    assert service.status(token, upload["video_id"])["state"] == "PROCESSING"
    service.objects.put(saved)
    assert service.handle_completions() == 1
    assert service.status(token, upload["video_id"])["state"] == "READY"


@pytest.mark.parametrize("value,expected", [(None, (0, 9, False)), ("bytes=2-4", (2, 4, True)),
                                         ("bytes=-3", (7, 9, True)), ("bytes=8-", (8, 9, True)),
                                         ("bytes=0-100", (0, 9, True))])
def test_byte_ranges(value, expected):
    assert byte_range(value, 10) == expected


@pytest.mark.parametrize("value", ["bytes=20-", "bytes=-0", "bytes=5-2", "bytes=0-1,3-4", "bytes=-"])
def test_invalid_ranges(value):
    with pytest.raises(ValueError):
        byte_range(value, 10)


@contextmanager
def running(service):
    http = server(service)
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_address[1]}"
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=5)


def request(url, method="GET", data=None, token=None, headers=None):
    values = dict(headers or {})
    if token:
        values["Authorization"] = "Bearer " + token
    if isinstance(data, dict):
        data = json.dumps(data).encode()
        values["Content-Type"] = "application/json"
    if method == "POST" and data is None:
        data = b""
    return urlopen(Request(url, data=data, method=method, headers=values), timeout=10)


def test_actual_http_upload_hls_authorization_and_range(service):
    token = service.issue("alice")
    with running(service) as url:
        with request(url + "/uploads", "POST", {"title": "Example", "size": 8, "part_size": 4}, token) as response:
            upload = json.load(response)
        for grant in upload["parts"]:
            with request(url + "/part?" + urlencode({"grant": grant}), "PUT", b"data") as response:
                assert json.load(response)["created"]
        with request(url + "/finish?video=" + upload["video_id"], "POST", token=token):
            pass
        drain(service)
        with request(url + "/playback?video=" + upload["video_id"], "POST", token=token) as response:
            grant = json.load(response)["grant"]
        with request(url + "/media?" + urlencode({"grant": grant, "resource": "144/index.m3u8"})) as response:
            playlist = response.read().decode()
        assert "URI=\"/media?" in playlist and "resource=key.bin" in playlist
        segment = url + "/media?" + urlencode({"grant": grant, "resource": "144/segment_00000.ts"})
        with request(segment, headers={"Range": "bytes=0-3"}) as response:
            assert response.status == 206 and len(response.read()) == 4
        with pytest.raises(HTTPError) as error:
            request(segment, headers={"Range": "bytes=999999-"})
        assert error.value.code == 416
        with pytest.raises(HTTPError) as error:
            request(url + "/media?grant=invalid&resource=key.bin")
        assert error.value.code == 403


def make_fixture(media, path):
    media.run(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30", "-f", "lavfi", "-i",
               "sine=frequency=440:sample_rate=44100", "-t", "3", "-c:v", "libx264",
               "-threads", "2", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)])


def test_real_ffmpeg_encrypted_multi_quality_http_playback(tmp_path):
    media = FFmpegMedia()
    fixture = tmp_path / "fixture.mp4"
    make_fixture(media, fixture)
    service = VideoService(tmp_path / "media.sqlite3", tmp_path / "objects", profiles=(144, 240))
    try:
        token, upload = uploaded(service, fixture.read_bytes())
        # Explicit fixture upload uses small parts to exercise resume; media remains real.
        drain(service, media)
        assert service.status(token, upload["video_id"])["state"] == "READY"
        grant = service.playback(token, upload["video_id"])
        with running(service) as url:
            master = url + "/media?" + urlencode({"grant": grant, "resource": "master.m3u8"})
            with request(master) as response:
                value = response.read().decode()
            assert "RESOLUTION=256x144" in value and "RESOLUTION=426x240" in value
            for profile in (144, 240):
                playlist = url + "/media?" + urlencode({"grant": grant, "resource": f"{profile}/index.m3u8"})
                # FFmpeg fetches manifests, encrypted segments and authenticated key over real HTTP.
                media.run(["-protocol_whitelist", "file,http,tcp,crypto", "-allowed_extensions", "ALL",
                           "-i", playlist, "-f", "null", "-"])
        assert service.asset(grant, "thumbnail.png").read_bytes().startswith(b"\x89PNG")
    finally:
        service.close()


def test_independent_worker_crash_and_real_replacement(tmp_path):
    media = FFmpegMedia()
    fixture = tmp_path / "fixture.mp4"
    make_fixture(media, fixture)
    root = tmp_path / "process-data"
    root.mkdir()
    service = VideoService(root / "metadata.sqlite3", root / "objects", profiles=(144,))
    token, upload = uploaded(service, fixture.read_bytes())
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    # The process owns an inspection lease and deliberately stops making progress.
    code = ("import sys,time; from pathlib import Path; from video_streaming import VideoService; "
            "r=Path(sys.argv[1]); s=VideoService(r/'metadata.sqlite3',r/'objects'); "
            "s.claim('crashing-worker',.2); print('Lease claimed',flush=True); time.sleep(60)")
    crashed = subprocess.Popen([sys.executable, "-c", code, str(root)], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
    replacements = []
    try:
        deadline = time.monotonic() + 10
        while service.db.execute("SELECT COUNT(*) FROM jobs WHERE state='RUNNING'").fetchone()[0] != 1:
            if crashed.poll() is not None or time.monotonic() > deadline:
                raise AssertionError("worker failed to claim its lease")
            time.sleep(.02)
        crashed.kill()
        crashed.wait(timeout=5)
        time.sleep(.25)
        for index in range(2):
            replacements.append(subprocess.Popen([sys.executable, "-m", "video_streaming.cli", "--root", str(root),
                "worker", "--name", f"replacement-{index}"], env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options))
        deadline = time.monotonic() + 20
        while service.status(token, upload["video_id"])["state"] != "READY":
            if time.monotonic() > deadline:
                raise AssertionError("replacement workers did not publish real media")
            time.sleep(.05)
        assert service.db.execute("SELECT attempts FROM jobs WHERE name='inspect'").fetchone()[0] == 2
        grant = service.playback(token, upload["video_id"])
        assert "144/index.m3u8" in service.asset(grant, "master.m3u8").read_text()
        assert service.db.execute("SELECT COUNT(*) FROM completions WHERE handled=1").fetchone()[0] == 1
    finally:
        if crashed.poll() is None:
            crashed.kill()
            crashed.wait(timeout=5)
        crashed.stdout.close()
        crashed.stderr.close()
        for process in replacements:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        service.close()
