"""Lease-fenced durable DAG worker; independent processes may run
concurrently.
"""

import logging
import pathlib
import shutil
import time

import video_streaming.database
import video_streaming.media
import video_streaming.service


class Worker:
    """Executes leased transcode DAG tasks with attempt-local working files."""

    def __init__(
        self,
        service,
        media,
        name="worker",
        lease=120,
        max_attempts=3,
        retry_base=1,
    ):
        if lease <= getattr(media, "timeout", 0):
            raise ValueError("lease must exceed the media timeout")
        self.service, self.media, self.name = service, media, name
        self.lease, self.max_attempts, self.retry_base = (
            lease,
            max_attempts,
            retry_base,
        )
        self.work = service.objects.root.parent / "work"
        self.work.mkdir(exist_ok=True)

    def execute(self, job):
        """Execute a claimed task and return its JSON-serializable output.

        Args:
            job: A claim containing kind, video, lease, profile, and video_data.

        Returns:
            Inspection metadata or an asset-name to object-key mapping.

        Raises:
            InvalidMedia: The source cannot be decoded.
            ValueError: The job kind is unsupported.
            OSError: Required source or output objects cannot be accessed.
        """
        service = self.service
        folder = self.work / job["video"] / job["lease"]
        folder.mkdir(parents=True, exist_ok=True)
        source = service.objects.file(job["video_data"]["source"])
        try:
            if hasattr(source, "__fspath__"):
                source = pathlib.Path(source)
            if job["kind"] == "inspect":
                return self.media.inspect(source, folder)
            if job["kind"] == "thumbnail":
                return {
                    "thumbnail.png": service.objects.put_file(
                        self.media.thumbnail(source, folder)
                    )
                }
            if job["kind"] == "encode":
                files = self.media.encode(
                    source,
                    folder,
                    job["profile"],
                    bytes.fromhex(job["video_data"]["key_hex"]),
                )
                return {
                    f"{job['profile']}/{path.name}": service.objects.put_file(
                        path
                    )
                    for path in files
                }
            if job["kind"] == "publish":
                outputs = service.outputs(job["video"])
                assets = {}
                playlist = ["#EXTM3U", "#EXT-X-VERSION:3"]
                # Read the persisted DAG's profiles, not this worker's startup
                # defaults.
                profiles = sorted(
                    int(name.split("-")[1])
                    for name in outputs
                    if name.startswith("encode-")
                )
                for profile in profiles:
                    width, height, bitrate = video_streaming.service.PROFILES[
                        profile
                    ]
                    assets.update(outputs[f"encode-{profile}"])
                    playlist += [
                        (
                            f"#EXT-X-STREAM-INF:BANDWIDTH={bitrate + 96000},"
                            f"RESOLUTION={width}x{height}"
                        ),
                        f"{profile}/index.m3u8",
                    ]
                assets.update(outputs["thumbnail"])
                assets["key.bin"] = service.objects.put(
                    bytes.fromhex(job["video_data"]["key_hex"])
                )
                assets["master.m3u8"] = service.objects.put(
                    ("\n".join(playlist) + "\n").encode()
                )
                if not profiles or any(
                    not service.objects.file(key).is_file()
                    for key in assets.values()
                ):
                    raise RuntimeError("publication output incomplete")
                return assets
            raise ValueError("unknown task type")
        finally:
            # Attempt-local path is generated here, never from an HTTP path.
            folder.resolve().relative_to(self.work.resolve())
            shutil.rmtree(folder)

    def run_once(self, handle_completion=True):
        """Process at most one job; return False when no job is ready."""
        if handle_completion:
            self.service.handle_completions()
        job = self.service.claim(self.name, self.lease, self.max_attempts)
        if job is None:
            return False
        try:
            self.service.complete(job, self.execute(job))
        # Isolate this operation; record failures before continuing.
        except Exception as error:  # pylint: disable=broad-exception-caught
            logging.getLogger(__name__).exception(
                "Failure isolated in %s", "run_once"
            )
            self.service.fail(
                job,
                error,
                isinstance(error, video_streaming.media.InvalidMedia),
                self.max_attempts,
                self.retry_base,
            )
        if handle_completion:
            self.service.handle_completions()
        return True

    def run(self, once=False):
        """Poll for durable work, backing off on transient storage failures.

        Args:
            once: Drain currently ready work and return when idle if True;
                otherwise
                poll indefinitely. In drain mode, storage failures propagate.
        """
        while True:
            try:
                progressed = self.run_once()
            except (video_streaming.database.MetadataUnavailable, OSError):
                # A lost commit may have succeeded. Poll persisted state next
                # time;
                # expired task leases, not blind transaction replay, recover
                # work.
                if once:
                    raise
                time.sleep(1)
                continue
            if not progressed:
                if once:
                    return
                time.sleep(0.2)
