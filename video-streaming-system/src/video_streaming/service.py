"""Resumable uploads, persistent leased DAG and completion outbox."""

import collections
import hashlib
import json
import math
import re
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
from types import MappingProxyType

from . import grants

import video_streaming.database
import video_streaming.storage
from video_streaming.lifecycle import Operations, owned
from video_streaming.pipeline import Pipeline, PROFILES as PROFILES
from video_streaming.scheduling import JobQueue
from video_streaming.schema import SCHEMA


class VideoService:
    """Persistent upload, transcode, and playback state transitions.

    A service serializes operations on its connection. Independent services may
    share PostgreSQL and an object store; grants use the persisted signing key.
    """

    def __init__(
        self,
        db,
        objects,
        profiles=(240, 360),
        clock=time.time,
        max_bytes=1024**3,
        cache_bytes=8 * 1024**2,
        *,
        pipeline=None,
    ):
        self.pipeline = (
            Pipeline(profiles) if pipeline is None else Pipeline.from_config(pipeline)
        )
        if type(max_bytes) is not int or not 1 <= max_bytes <= 1024**3:
            raise ValueError("invalid upload limit")
        if type(cache_bytes) is not int or cache_bytes < 0:
            raise ValueError("invalid cache byte capacity")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.profiles, self.clock, self.max_bytes = (
            self.pipeline.profiles,
            clock,
            max_bytes,
        )
        self.objects = (
            objects
            if isinstance(objects, video_streaming.storage.LocalObjects)
            else video_streaming.storage.LocalObjects(objects)
        )
        self.lock = threading.RLock()
        self._operations = Operations(self.lock)
        self._closed = False
        self._cache_loads = tuple(threading.Lock() for _ in range(64))
        self._cache_epoch = 0
        self._asset_maps = collections.OrderedDict()
        self.cache, self.cache_bytes, self.cached_bytes = (
            collections.OrderedDict(),
            cache_bytes,
            0,
        )
        if isinstance(db, str) and (
            db.startswith(("postgres://", "postgresql://")) or "host=" in db
        ):
            self.db = video_streaming.database.Postgres(db)
        else:
            self.db = sqlite3.connect(db, check_same_thread=False, timeout=10)
            self.db.row_factory = sqlite3.Row
            deadline = time.monotonic() + 10
            while True:
                try:
                    self.db.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as error:
                    if (
                        "locked" not in str(error).lower()
                        or time.monotonic() >= deadline
                    ):
                        self.db.close()
                        raise
                    time.sleep(0.01)
        try:
            self.db.executescript(SCHEMA)
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO settings VALUES('signing',?)",
                    (secrets.token_hex(32),),
                )
            self.secret = bytes.fromhex(
                self.db.execute(
                    "SELECT value FROM settings WHERE key='signing'"
                ).fetchone()[0]
            )
        except BaseException:
            self.db.close()
            raise
        self.jobs = JobQueue(self.db, self.lock, self.clock, self._video, self.objects)

    def close(self):
        """Reject new work and drain accepted I/O before closing metadata."""
        with self._operations.condition:
            self._operations.drain()
            if not self._closed:
                self.db.close()
                self._closed = True
                self.cache.clear()
                self._asset_maps.clear()
                self.cached_bytes = 0

    @owned
    def issue(self, owner):
        """Issue an opaque bearer token for owner and persist only its hash."""
        if not isinstance(owner, str) or not 1 <= len(owner) <= 200:
            raise ValueError("invalid owner")
        token = secrets.token_urlsafe(32)
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO credentials VALUES(?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), owner),
            )
        return token

    @owned
    def authenticate(self, token):
        """Return the credential owner or raise PermissionError for an invalid
        token.
        """
        if not isinstance(token, str) or not 1 <= len(token) <= 256:
            raise PermissionError("invalid credential")
        with self.lock:
            row = self.db.execute(
                "SELECT owner FROM credentials WHERE token=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
        if not row:
            raise PermissionError("invalid credential")
        return row[0]

    def _sign(self, value):
        return grants.sign(self.secret, value)

    @owned
    def verify(self, token, kind):
        """Validate a signed grant using the configured clock and secret."""
        return grants.verify(self.secret, self.clock, token, kind)

    def _video(self, video):
        if not isinstance(video, str) or re.fullmatch(r"[0-9a-f]{32}", video) is None:
            raise ValueError("invalid video identifier")
        row = self.db.execute("SELECT * FROM videos WHERE id=?", (video,)).fetchone()
        if row is None:
            raise ValueError("video not found")
        return dict(row)

    def _check_plan(self, video, plan):
        actual = {
            (row["name"], row["kind"], row["profile"], tuple(json.loads(row["deps"])))
            for row in self.db.execute(
                "SELECT name,kind,profile,deps FROM jobs WHERE video=?", (video,)
            )
        }
        expected = {
            (task.name, task.kind, task.profile, task.dependencies)
            for task in plan.tasks()
        }
        if actual != expected:
            raise ValueError("conflicting finalization pipeline")

    @owned
    def update_metadata(self, token, video, title):
        """Change an owned title independently of upload/encoding progress."""
        if not isinstance(title, str) or not title.strip() or len(title) > 500:
            raise ValueError("invalid video title")
        owner = self.authenticate(token)
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            value = self._video(video)
            if value["owner"] != owner:
                raise PermissionError("not the owner")
            if value["state"] not in ("UPLOADING", "PROCESSING", "READY"):
                raise ValueError("video metadata cannot be updated")
            self.db.execute("UPDATE videos SET title=? WHERE id=?", (title, video))
        return self.status(token, video)

    @owned
    def create(self, token, title, size, part_size=4 * 1024**2, ttl=3600):
        """Create a resumable upload and issue one signed grant per part.

        Args:
            token: Owner bearer credential.
            title: Nonempty video title of at most 500 characters.
            size: Total source size in bytes, within the configured upload
                limit.
            part_size: Bytes per part, at most 8 MiB; no more than 4096 parts.
            ttl: Upload grant lifetime in seconds, between 1 and 86400.

        Returns:
            A dictionary containing video_id and an ordered list of signed
            parts.

        Raises:
            PermissionError: The credential is invalid.
            ValueError: The title, size, part count, or grant lifetime is
                invalid.
        """
        owner = self.authenticate(token)
        if (
            not isinstance(title, str)
            or not 1 <= len(title) <= 500
            or not title.strip()
            or (not isinstance(size, int) or isinstance(size, bool))
            or not 1 <= size <= self.max_bytes
            or (not isinstance(part_size, int) or isinstance(part_size, bool))
            or not 1 <= part_size <= 8 * 1024**2
            or ((size + part_size - 1) // part_size) > 4096
            or (not isinstance(ttl, int) or isinstance(ttl, bool))
            or not 1 <= ttl <= 86400
        ):
            raise ValueError("invalid upload size/title/grant lifetime")
        video = uuid.uuid4().hex
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO videos VALUES(?,?,?,?,?,'UPLOADING',NULL,'{}',?,?)",
                (
                    video,
                    owner,
                    title,
                    size,
                    part_size,
                    secrets.token_hex(16),
                    self.clock(),
                ),
            )
        return self.upload_grants(token, video, ttl)

    @owned
    def upload_grants(self, token, video, ttl=3600):
        """Refresh upload grants and the incomplete upload's retention
        timestamp.

        Args:
            token: Owner bearer credential.
            video: Identifier of an upload still accepting parts.
            ttl: Grant lifetime in seconds, between 1 and 86400.

        Returns:
            A dictionary containing video_id and one signed grant per part.

        Raises:
            PermissionError: The credential does not identify the owner.
            ValueError: The upload state or lifetime is invalid.
        """
        owner = self.authenticate(token)
        if (not isinstance(ttl, int) or isinstance(ttl, bool)) or not 1 <= ttl <= 86400:
            raise ValueError("invalid grant lifetime")
        with self.lock, self.db:
            value = self._video(video)
            if value["owner"] != owner:
                raise PermissionError("not the owner")
            if value["state"] != "UPLOADING":
                raise ValueError("upload no longer accepts parts")
            self.db.execute(
                "UPDATE videos SET created=? WHERE id=? AND state='UPLOADING'",
                (self.clock(), video),
            )
        return {
            "video_id": video,
            "parts": [
                self._sign(
                    {
                        "kind": "upload",
                        "video": video,
                        "part": i,
                        "size": min(
                            value["part_size"],
                            value["size"] - i * value["part_size"],
                        ),
                        "expires": self.clock() + ttl,
                    }
                )
                for i in range(
                    ((value["size"] + value["part_size"] - 1) // value["part_size"])
                )
            ],
        }

    @owned
    def put_part(self, grant, data, checksum=None):
        """Persist bytes outside the metadata lock; commit a verified reference.

        Retry identity and state are rechecked in the final write transaction,
        which also serializes reference acceptance with conservative GC.
        """
        claims = self.verify(grant, "upload")
        if (
            type(claims.get("part")) is not int
            or not 0 <= claims["part"] < 4096
            or type(claims.get("size")) is not int
            or not 1 <= claims["size"] <= 8 * 1024**2
        ):
            raise PermissionError("invalid upload claims")
        if not isinstance(data, bytes) or len(data) != claims["size"]:
            raise ValueError("part length mismatch")
        key = hashlib.sha256(data).hexdigest()
        if checksum is not None and checksum != key:
            raise ValueError("part checksum mismatch")
        with self.lock:
            value = self._video(claims["video"])
            if value["state"] in ("REMOVED", "EXPIRED"):
                raise ValueError("video no longer accepts uploads")
            expected = min(
                value["part_size"], value["size"] - claims["part"] * value["part_size"]
            )
            if expected != claims["size"]:
                raise PermissionError("upload claims do not match this part")
            old = self.db.execute(
                "SELECT key FROM parts WHERE video=? AND idx=?",
                (value["id"], claims["part"]),
            ).fetchone()
            if old and old[0] != key:
                raise ValueError("conflicting part retry")
        if self.objects.put(data) != key:
            raise OSError("object store returned a different checksum")
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            video = self._video(value["id"])
            if video["state"] in ("REMOVED", "EXPIRED"):
                raise ValueError("video no longer accepts uploads")
            old = self.db.execute(
                "SELECT key FROM parts WHERE video=? AND idx=?",
                (video["id"], claims["part"]),
            ).fetchone()
            if old:
                if old[0] != key:
                    raise ValueError("conflicting part retry")
                return False
            if video["state"] != "UPLOADING":
                raise ValueError("upload no longer accepts parts")
            if not self.objects.file(key).is_file():
                raise OSError("uploaded object unavailable; retry")
            self.db.execute(
                "INSERT INTO parts VALUES(?,?,?,?)",
                (video["id"], claims["part"], key, len(data)),
            )
            return True

    @owned
    def status(self, token, video):
        """Return video metadata and uploaded part indices to its owner.

        Args:
            token: Owner bearer credential.
            video: Video identifier.

        Returns:
            A dictionary with id, title, size, state, and uploaded_parts.

        Raises:
            PermissionError: The credential does not identify the owner.
        """
        owner = self.authenticate(token)
        with self.lock:
            value = self._video(video)
            if value["owner"] != owner:
                raise PermissionError("not the owner")
            parts = [
                r[0]
                for r in self.db.execute(
                    "SELECT idx FROM parts WHERE video=? ORDER BY idx", (video,)
                )
            ]
            failure = None
            if value["state"] == "FAILED":
                row = self.db.execute(
                    "SELECT name,error,attempts FROM jobs WHERE video=? AND state='FAILED' LIMIT 1",
                    (video,),
                ).fetchone()
                if row is not None:
                    failure = {
                        "task": row["name"],
                        "message": row["error"],
                        "attempts": row["attempts"],
                    }
        result = {k: value[k] for k in ("id", "title", "size", "state")}
        result["uploaded_parts"] = parts
        if failure is not None:
            result["error"] = failure
        return result

    @owned
    def finish(self, token, video, checksum=None, priority=0, *, pipeline=None):
        """Assemble a complete upload and atomically enqueue its transcode DAG.

        Args:
            token: Owner bearer credential.
            video: Video identifier to finalize.
            checksum: Optional expected SHA-256 digest of the complete source.
            priority: Integer scheduling priority; larger values run first.

        Returns:
            The video identifier, including on an identical finalization retry.

        Raises:
            PermissionError: The credential does not identify the owner.
            ValueError: Parts are missing, checksums differ, or the state is
                invalid.
        """
        if type(priority) is not int or not -(2**31) <= priority < 2**31:
            raise ValueError("invalid task priority")
        if checksum is not None and (
            not isinstance(checksum, str)
            or re.fullmatch(r"[0-9a-f]{64}", checksum) is None
        ):
            raise ValueError("invalid source checksum")
        plan = self.pipeline if pipeline is None else Pipeline.from_config(pipeline)
        owner = self.authenticate(token)
        with self.lock:
            value = self._video(video)
            if value["owner"] != owner:
                raise PermissionError("not the owner")
            if value["state"] != "UPLOADING":
                if (
                    value["state"] not in ("PROCESSING", "READY")
                    or checksum
                    and checksum != value["source"]
                ):
                    raise ValueError("upload cannot be finalized")
                if pipeline is not None:
                    self._check_plan(video, plan)
                return video
            rows = self.db.execute(
                "SELECT idx,key,size FROM parts WHERE video=? ORDER BY idx",
                (video,),
            ).fetchall()
            if [r[0] for r in rows] != list(
                range(((value["size"] + value["part_size"] - 1) // value["part_size"]))
            ):
                raise ValueError("missing upload parts")
        key, size = self.objects.assemble([r[1] for r in rows])
        if size != value["size"] or checksum and checksum != key:
            raise ValueError("source checksum/size mismatch")
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            state = self._video(video)["state"]
            if state in ("PROCESSING", "READY"):
                if pipeline is not None:
                    self._check_plan(video, plan)
                return video
            if state != "UPLOADING":
                raise ValueError("video removed during finalization")
            if not self.objects.file(key).is_file():
                raise OSError("assembled source unavailable; retry")
            self.jobs.enqueue(video, plan, priority)
            self.db.execute(
                "UPDATE videos SET state='PROCESSING',source=? WHERE id=?",
                (key, video),
            )
        return video

    @owned
    def claim(self, worker, lease_seconds=120, max_attempts=3, *, kinds=None):
        """Lease one ready capability-matched task, recovering expired attempts."""
        return self.jobs.claim(worker, lease_seconds, max_attempts, kinds=kinds)

    @owned
    def renew(self, job, lease_seconds=120):
        """Extend only the current unexpired lease of a processing video."""
        return self.jobs.renew(job, lease_seconds)

    @owned
    def complete(self, job, output):
        """Persist output and publication outbox only for a live current lease."""
        return self.jobs.complete(job, output)

    @owned
    def fail(self, job, error, fatal=False, max_attempts=3, retry_base=1):
        """Record a fenced failure using the persisted attempt count."""
        return self.jobs.fail(job, error, fatal, max_attempts, retry_base)

    @owned
    def outputs(self, video):
        """Return durable completed outputs by task name."""
        return self.jobs.outputs(video)

    @owned
    def handle_completions(self, limit=64):
        """Validate a bounded outbox batch outside the connection/write lock.

        DONE job outputs already retain these objects during GC. The final
        transaction checks current video/outbox state, so removal always wins.
        """
        if type(limit) is not int or not 1 <= limit <= 1024:
            raise ValueError("invalid completion batch limit")
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM completions WHERE handled=0 LIMIT ?", (limit,)
            ).fetchall()
        handled = 0
        for row in rows:
            assets = json.loads(row["assets"])
            available = (
                bool(assets)
                and "master.m3u8" in assets
                and all(self.objects.file(key).is_file() for key in assets.values())
            )
            with self.lock, self.db:
                self.db.execute("BEGIN IMMEDIATE")
                current = self.db.execute(
                    "SELECT assets,handled FROM completions WHERE video=?",
                    (row["video"],),
                ).fetchone()
                if (
                    current is None
                    or current["handled"]
                    or current["assets"] != row["assets"]
                ):
                    continue
                value = self._video(row["video"])
                if value["state"] == "PROCESSING":
                    if not available:
                        continue
                    self.db.execute(
                        "UPDATE videos SET state='READY',assets=? WHERE id=?",
                        (row["assets"], row["video"]),
                    )
                self.db.execute(
                    "UPDATE completions SET handled=1 WHERE video=?", (row["video"],)
                )
                handled += 1
        return handled

    @owned
    def playback(self, token, video, ttl=300):
        """Issue a signed playback grant for a ready video owned by the caller.

        Args:
            token: Owner bearer credential.
            video: Identifier of a READY video.
            ttl: Grant lifetime in seconds, between 1 and 3600.

        Returns:
            A signed playback grant.

        Raises:
            PermissionError: The caller is not the owner.
            ValueError: The video is not ready or the lifetime is invalid.
        """
        owner = self.authenticate(token)
        if (not isinstance(ttl, int) or isinstance(ttl, bool)) or not 1 <= ttl <= 3600:
            raise ValueError("invalid playback lifetime")
        with self.lock:
            value = self._video(video)
            if value["owner"] != owner:
                raise PermissionError("not the owner")
            if value["state"] != "READY":
                raise ValueError("video not ready")
        return self._sign(
            {"kind": "play", "video": video, "expires": self.clock() + ttl}
        )

    @owned
    def asset(self, grant, resource):
        """Resolve an asset through a valid playback grant and current video
        state.

        Args:
            grant: Signed, unexpired playback grant.
            resource: Asset name in the published manifest, such as master.m3u8.

        Returns:
            A local Path or remote object proxy supporting binary reads.

        Raises:
            PermissionError: The grant is invalid or the video was removed.
            ValueError: The resource is not a published asset.
        """
        claims = self.verify(grant, "play")
        with self.lock:
            video = claims["video"]
            if (
                not isinstance(video, str)
                or re.fullmatch(r"[0-9a-f]{32}", video) is None
            ):
                raise PermissionError("invalid playback claims")
            current = self.db.execute(
                "SELECT state FROM videos WHERE id=?", (video,)
            ).fetchone()
            if current is None or current[0] != "READY":
                raise PermissionError("video unavailable")
            if video not in self._asset_maps:
                # READY object maps are immutable. Authorization/state is still
                # read from the database on every request, including cache hits.
                self._asset_maps[video] = MappingProxyType(
                    json.loads(self._video(video)["assets"])
                )
                while len(self._asset_maps) > 128:
                    self._asset_maps.popitem(last=False)
            self._asset_maps.move_to_end(video)
            key = self._asset_maps[video].get(resource)
            if key is None:
                raise ValueError("asset not found")
        return self.objects.file(key)

    @owned
    def remove(self, token, video):
        """Mark an owned video REMOVED and cancel its queued and running
        jobs.
        """
        owner = self.authenticate(token)
        with self.lock, self.db:
            value = self._video(video)
            if value["owner"] != owner:
                raise PermissionError("not the owner")
            self.db.execute("UPDATE videos SET state='REMOVED' WHERE id=?", (video,))
            self._asset_maps.pop(video, None)
            self.db.execute(
                "UPDATE jobs SET state='CANCELLED' WHERE video=? AND state IN"
                " ('QUEUED','RUNNING')",
                (video,),
            )

    @owned
    def read_small(self, path, maximum=None):
        """Coalesce cold reads without holding the service's metadata/cache lock.

        A remote object must still exist at the authoritative origin on cache
        hits. maximum optionally rejects oversized manifests before reading.
        """
        if maximum is not None and (type(maximum) is not int or maximum < 0):
            raise ValueError("invalid object read limit")
        key = path.name
        if not path.is_file():
            raise OSError("object unavailable")
        if maximum is not None and path.stat().st_size > maximum:
            raise ValueError("object exceeds read limit")
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                value = self.cache[key]
                if maximum is not None and len(value) > maximum:
                    raise ValueError("object exceeds read limit")
                return value
        with self._cache_loads[hash(key) % len(self._cache_loads)]:
            with self.lock:
                if key in self.cache:
                    value = self.cache[key]
                    if maximum is not None and len(value) > maximum:
                        raise ValueError("object exceeds read limit")
                    self.cache.move_to_end(key)
                    return value
                epoch = self._cache_epoch
            if maximum is None:
                value = path.read_bytes()
            else:
                with path.open("rb") as stream:
                    value = stream.read(maximum + 1)
                if len(value) > maximum:
                    raise ValueError("object exceeds read limit")
            with self.lock:
                if epoch == self._cache_epoch and len(value) <= self.cache_bytes:
                    self.cache[key] = value
                    self.cached_bytes += len(value)
                    while self.cached_bytes > self.cache_bytes:
                        _, old = self.cache.popitem(last=False)
                        self.cached_bytes -= len(old)
            return value

    @owned
    def cleanup(self, min_age=3600, upload_age=86401):
        """Local conservative GC; young writes and active task attempts are
        retained.
        """
        try:
            valid = all(
                type(value) in (int, float) and math.isfinite(value)
                for value in (min_age, upload_age)
            )
        except OverflowError:
            valid = False
        if not valid or min_age < 300 or upload_age < 86401:
            raise ValueError("cleanup grace periods too short")
        deleted, expired, attempts = 0, 0, 0
        wall_now = time.time()
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            expired = self.db.execute(
                "UPDATE videos SET state='EXPIRED' WHERE state='UPLOADING' AND"
                " created<?",
                (self.clock() - upload_age,),
            ).rowcount
            self.db.execute(
                "DELETE FROM parts WHERE video IN (SELECT id FROM videos WHERE"
                " state IN ('EXPIRED','REMOVED'))"
            )
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            references = set()

            def collect(value):
                if isinstance(value, dict):
                    for child in value.values():
                        collect(child)
                elif isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
                    references.add(value)

            for row in self.db.execute(
                "SELECT source,assets FROM videos WHERE state NOT IN"
                " ('REMOVED','EXPIRED')"
            ):
                collect(row["source"])
                collect(json.loads(row["assets"]))
            for row in self.db.execute("SELECT key FROM parts"):
                references.add(row[0])
            for row in self.db.execute(
                "SELECT j.output FROM jobs j JOIN videos v ON v.id=j.video "
                "WHERE j.output IS NOT NULL AND v.state NOT IN ('REMOVED','EXPIRED') "
                "AND NOT (j.kind='preprocess' AND v.state IN ('READY','FAILED'))"
            ):
                collect(json.loads(row[0]))
            for row in self.db.execute(
                "SELECT assets FROM completions WHERE video IN (SELECT id FROM"
                " videos WHERE state NOT IN ('REMOVED','EXPIRED'))"
            ):
                collect(json.loads(row[0]))
            if hasattr(self.objects, "sweep"):
                deleted += self.objects.sweep(references, wall_now - min_age)
            for path in self.objects.root.iterdir():
                if (
                    path.name not in references
                    and path.is_file()
                    and wall_now - path.stat().st_mtime > min_age
                ):
                    path.resolve().relative_to(self.objects.root.resolve())
                    try:
                        path.unlink()
                        deleted += 1
                    except PermissionError:
                        # Let an in-flight Windows reader finish before retry.
                        pass
            live = {
                r[0]
                for r in self.db.execute("SELECT lease FROM jobs WHERE state='RUNNING'")
            }
            work = self.objects.root.parent / "work"
            if work.exists():
                for video in work.iterdir():
                    if not video.is_dir() or not re.fullmatch(
                        r"[0-9a-f]{32}", video.name
                    ):
                        continue
                    for path in video.iterdir():
                        if (
                            path.is_dir()
                            and re.fullmatch(r"[0-9a-f]{32}", path.name)
                            and path.name not in live
                            and wall_now - path.stat().st_mtime > min_age
                        ):
                            path.resolve().relative_to(work.resolve())
                            shutil.rmtree(path)
                            attempts += 1
            self._cache_epoch += 1
            self.cache.clear()
            self.cached_bytes = 0
        return {
            "expired_uploads": expired,
            "removed_objects": deleted,
            "removed_attempts": attempts,
        }
