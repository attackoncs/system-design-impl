"""Lease-fenced durable DAG worker; independent processes may run concurrently."""
import json
from pathlib import Path
import shutil
import time

from .media import InvalidMedia
from .service import PROFILES
from .database import MetadataUnavailable


class Worker:
    def __init__(self, service, media, name="worker", lease=120, max_attempts=3, retry_base=1):
        if lease <= getattr(media, "timeout", 0):
            raise ValueError("lease must exceed the media timeout")
        self.service, self.media, self.name = service, media, name
        self.lease, self.max_attempts, self.retry_base = lease, max_attempts, retry_base
        self.work = service.objects.root.parent / "work"
        self.work.mkdir(exist_ok=True)

    def execute(self, job):
        service = self.service
        folder = self.work / job["video"] / job["lease"]
        folder.mkdir(parents=True, exist_ok=True)
        source = service.objects.file(job["video_data"]["source"])
        try:
            if hasattr(source, "__fspath__"):
                source = Path(source)
            if job["kind"] == "inspect":
                return self.media.inspect(source, folder)
            if job["kind"] == "thumbnail":
                return {"thumbnail.png": service.objects.put_file(self.media.thumbnail(source, folder))}
            if job["kind"] == "encode":
                files = self.media.encode(source, folder, job["profile"], bytes.fromhex(job["video_data"]["key_hex"]))
                return {f"{job['profile']}/{path.name}": service.objects.put_file(path) for path in files}
            if job["kind"] == "publish":
                outputs = service.outputs(job["video"])
                assets = {}
                playlist = ["#EXTM3U", "#EXT-X-VERSION:3"]
                # Read the persisted DAG's profiles, not this worker's startup defaults.
                profiles = sorted(int(name.split("-")[1]) for name in outputs if name.startswith("encode-"))
                for profile in profiles:
                    width, height, bitrate = PROFILES[profile]
                    assets.update(outputs[f"encode-{profile}"])
                    playlist += [f"#EXT-X-STREAM-INF:BANDWIDTH={bitrate + 96000},RESOLUTION={width}x{height}",
                                 f"{profile}/index.m3u8"]
                assets.update(outputs["thumbnail"])
                assets["key.bin"] = service.objects.put(bytes.fromhex(job["video_data"]["key_hex"]))
                assets["master.m3u8"] = service.objects.put(("\n".join(playlist) + "\n").encode())
                if not profiles or any(not service.objects.file(key).is_file() for key in assets.values()):
                    raise RuntimeError("publication output incomplete")
                return assets
            raise ValueError("unknown task type")
        finally:
            # Attempt-local path is generated here, never from an HTTP path.
            folder.resolve().relative_to(self.work.resolve())
            shutil.rmtree(folder)

    def run_once(self, handle_completion=True):
        if handle_completion:
            self.service.handle_completions()
        job = self.service.claim(self.name, self.lease, self.max_attempts)
        if job is None:
            return False
        try:
            self.service.complete(job, self.execute(job))
        except Exception as error:
            self.service.fail(job, error, isinstance(error, InvalidMedia), self.max_attempts, self.retry_base)
        if handle_completion:
            self.service.handle_completions()
        return True

    def run(self, once=False):
        while True:
            try:
                progressed = self.run_once()
            except (MetadataUnavailable, OSError):
                # A lost commit may have succeeded. Poll persisted state next time;
                # expired task leases, not blind transaction replay, recover work.
                if once:
                    raise
                time.sleep(1)
                continue
            if not progressed:
                if once:
                    return
                time.sleep(.2)
