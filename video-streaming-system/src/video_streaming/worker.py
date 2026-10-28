"""Durable DAG workers with bounded execution, renewed leases and owned cleanup."""

import concurrent.futures
import contextlib
import logging
import pathlib
import re
import shutil
import threading

import video_streaming.database
import video_streaming.media
from video_streaming.pipeline import KINDS, PROFILES
from video_streaming.scheduling import attempts, duration

_LOG = logging.getLogger(__name__)


class Worker:
    """Run supported tasks within a fixed local capacity; other processes cooperate.

    Lease renewal covers object materialization and uploads as well as FFmpeg.
    Stop drains active operations. Lost leases stop media subprocesses and prevent
    local completion; remote object operations retain their backend timeouts.
    """

    def __init__(
        self,
        service,
        media,
        name="worker",
        lease=120,
        max_attempts=3,
        retry_base=1,
        *,
        concurrency=1,
        kinds=None,
    ):
        lease = duration(lease, "lease")
        timeout = duration(getattr(media, "timeout", 0), "media timeout", zero=True)
        if lease <= timeout:
            raise ValueError("lease must exceed the media timeout")
        attempts(max_attempts)
        if type(concurrency) is not int or not 1 <= concurrency <= 32:
            raise ValueError("invalid worker concurrency")
        if not isinstance(name, str) or not 1 <= len(name) <= 200:
            raise ValueError("invalid worker name")
        if kinds is not None:
            if not isinstance(kinds, (list, tuple, set, frozenset)) or not kinds:
                raise ValueError("invalid worker capabilities")
            if any(not isinstance(kind, str) or kind not in KINDS for kind in kinds):
                raise ValueError("invalid worker capabilities")
            kinds = frozenset(kinds)
        self.service, self.media, self.name = service, media, name
        self.lease, self.max_attempts = lease, max_attempts
        self.retry_base = duration(retry_base, "retry base", zero=True)
        self.concurrency, self.kinds = concurrency, kinds
        self._slots = threading.BoundedSemaphore(concurrency)
        self._run_lock = threading.Lock()
        self.work = service.objects.root.parent / "work"
        self.work.mkdir(exist_ok=True)

    def _prepared_source(self, job, folder):
        dependencies = job.get("dependencies", {})
        prepared = dependencies.get("preprocess")
        if prepared is None:
            return pathlib.Path(self.service.objects.file(job["video_data"]["source"]))
        if isinstance(prepared, dict) and "segments" in prepared:
            keys = prepared["segments"]
        elif isinstance(prepared, dict):
            keys = [prepared[name] for name in sorted(prepared)]
        else:
            raise ValueError("invalid preprocessing output")
        if not isinstance(keys, list) or not keys:
            raise ValueError("preprocessing output has no segments")
        lines = ["ffconcat version 1.0"]
        for key in keys:
            path = pathlib.Path(self.service.objects.file(key)).resolve().as_posix()
            # FFmpeg concat uses shell-like single quote escaping. The paths
            # refer only to validated content hashes in this worker's store.
            quoted = path.replace("'", "'\\''")
            lines.append("file '" + quoted + "'")
        manifest = folder / "input.ffconcat"
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return manifest

    def _publish(self, job):
        outputs = self.service.outputs(job["video"])
        assets, playlist = {}, ["#EXTM3U", "#EXT-X-VERSION:3"]
        profiles = sorted(
            int(name.split("-")[1]) for name in outputs if name.startswith("encode-")
        )
        for profile in profiles:
            width, height, bitrate = PROFILES[profile]
            assets.update(outputs[f"encode-{profile}"])
            playlist += [
                f"#EXT-X-STREAM-INF:BANDWIDTH={bitrate + 96000},"
                f"RESOLUTION={width}x{height}",
                f"{profile}/index.m3u8",
            ]
        assets.update(outputs.get("thumbnail", {}))
        assets["key.bin"] = self.service.objects.put(
            bytes.fromhex(job["video_data"]["key_hex"])
        )
        assets["master.m3u8"] = self.service.objects.put(
            ("\n".join(playlist) + "\n").encode()
        )
        if not profiles or any(
            not self.service.objects.file(key).is_file() for key in assets.values()
        ):
            raise RuntimeError("publication output incomplete")
        return assets

    def execute(self, job):
        """Execute only in a validated attempt folder and clean it on every exit."""
        if any(
            not isinstance(job.get(field), str)
            or not re.fullmatch(r"[0-9a-f]{32}", job[field])
            for field in ("video", "lease")
        ):
            raise ValueError("invalid attempt identity")
        folder = self.work / job["video"] / job["lease"]
        folder.resolve().relative_to(self.work.resolve())
        folder.mkdir(parents=True, exist_ok=True)
        try:
            kind = job["kind"]
            if kind == "publish":
                return self._publish(job)
            source = self._prepared_source(job, folder)
            if kind == "inspect":
                return self.media.inspect(source, folder)
            if kind == "preprocess":
                return {
                    path.name: self.service.objects.put_file(path)
                    for path in self.media.preprocess(source, folder)
                }
            if kind == "thumbnail":
                return {
                    "thumbnail.png": self.service.objects.put_file(
                        self.media.thumbnail(source, folder)
                    )
                }
            if kind == "encode":
                files = self.media.encode(
                    source,
                    folder,
                    job["profile"],
                    bytes.fromhex(job["video_data"]["key_hex"]),
                )
                return {
                    f"{job['profile']}/{path.name}": self.service.objects.put_file(path)
                    for path in files
                }
            raise ValueError("unknown task type")
        finally:
            try:
                shutil.rmtree(folder)
            except OSError:
                # Cleanup must not turn a valid output into a retry or hide the
                # original error. Conservative GC reaps abandoned attempts.
                _LOG.warning(
                    "Could not remove attempt folder %s", folder, exc_info=True
                )

    @contextlib.contextmanager
    def _heartbeat(self, job):
        finished, lost = threading.Event(), threading.Event()

        def renew():
            while not finished.wait(min(30, self.lease / 3)):
                try:
                    if self.service.renew(job, self.lease):
                        continue
                except Exception:
                    _LOG.exception("Could not renew task %s", job["id"])
                lost.set()
                return

        thread = threading.Thread(target=renew, name=f"video-lease-{self.name}")
        thread.start()
        try:
            yield lost
        finally:
            finished.set()
            thread.join()

    def _claim(self):
        if not self._slots.acquire(blocking=False):
            return None
        try:
            options = {} if self.kinds is None else {"kinds": self.kinds}
            job = self.service.claim(
                self.name, self.lease, self.max_attempts, **options
            )
        except BaseException:
            self._slots.release()
            raise
        if job is None:
            self._slots.release()
        return job

    def _process(self, job):
        try:
            with self._heartbeat(job) as lost:
                context = (
                    self.media.operation(lost)
                    if hasattr(self.media, "operation")
                    else contextlib.nullcontext()
                )
                try:
                    with context:
                        output = self.execute(job)
                    if not lost.is_set():
                        self.service.complete(job, output)
                except Exception as error:
                    _LOG.exception("Task attempt failed: %s", job["name"])
                    if not lost.is_set():
                        self.service.fail(
                            job,
                            error,
                            isinstance(error, video_streaming.media.InvalidMedia),
                            self.max_attempts,
                            self.retry_base,
                        )
        finally:
            self._slots.release()

    def run_once(self, handle_completion=True):
        """Process one task synchronously, or return False when idle/at capacity."""
        if handle_completion:
            self.service.handle_completions()
        job = self._claim()
        if job is None:
            return False
        self._process(job)
        if handle_completion:
            self.service.handle_completions()
        return True

    def _poll(self, pool, once, stop):
        pending = set()
        try:
            while not stop.is_set():
                self.service.handle_completions()
                while len(pending) < self.concurrency and not stop.is_set():
                    job = self._claim()
                    if job is None:
                        break
                    try:
                        pending.add(pool.submit(self._process, job))
                    except BaseException:
                        self._slots.release()
                        raise
                if not pending:
                    if once:
                        return
                    stop.wait(0.2)
                    continue
                done, pending = concurrent.futures.wait(
                    pending, timeout=0.1, return_when=concurrent.futures.FIRST_COMPLETED
                )
                for future in done:
                    future.result()
        finally:
            # Every submitted operation remains owned until it finishes.
            for future in pending:
                future.result()

    def run(self, once=False, stop=None):
        """Poll with bounded local parallelism; stop drains active operations.

        once=True drains ready work, but does not wait for delayed retries.
        Storage failures back off unless once=True. Only one polling loop may
        own this Worker; run_once remains useful for deterministic processing.
        """
        if not isinstance(once, bool):
            raise ValueError("once must be a boolean")
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("worker already has a polling loop")
        stop = threading.Event() if stop is None else stop
        try:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.concurrency, thread_name_prefix="video-task"
            ) as pool:
                while not stop.is_set():
                    try:
                        self._poll(pool, once, stop)
                        return
                    except (video_streaming.database.MetadataUnavailable, OSError):
                        if once:
                            raise
                        stop.wait(1)
        finally:
            self._run_lock.release()
