"""Resumable uploads, persistent leased DAG and completion outbox."""

import base64
import collections
import hashlib
import hmac
import json
import math
import re
import secrets
import shutil
import sqlite3
import threading
import time
import uuid

import video_streaming.database
import video_streaming.storage

PROFILES = {
    144: (256, 144, 200000),
    240: (426, 240, 400000),
    360: (640, 360, 800000),
    480: (854, 480, 1400000),
    720: (1280, 720, 2800000),
    1080: (1920, 1080, 5000000),
}


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
    ):
        if (
            not profiles
            or len(set(profiles)) != len(profiles)
            or any(p not in PROFILES for p in profiles)
        ):
            raise ValueError("invalid renditions")
        self.profiles, self.clock, self.max_bytes = (
            tuple(profiles),
            clock,
            max_bytes,
        )
        self.objects = (
            objects
            if isinstance(objects, video_streaming.storage.LocalObjects)
            else video_streaming.storage.LocalObjects(objects)
        )
        self.lock = threading.RLock()
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
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            "\n"
            "            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIM"
            "ARY KEY,value TEXT NOT NULL);\n"
            "            CREATE TABLE IF NOT EXISTS credentials(token TEXT"
            " PRIMARY KEY,owner TEXT NOT NULL);\n"
            "            CREATE TABLE IF NOT EXISTS videos(id TEXT PRIMARY"
            " KEY,owner TEXT,title TEXT,size INTEGER,\n"
            "                part_size INTEGER,state TEXT,source TEXT,asse"
            "ts TEXT,key_hex TEXT,created REAL);\n"
            "            CREATE TABLE IF NOT EXISTS parts(video TEXT,idx I"
            "NTEGER,key TEXT,size INTEGER,PRIMARY KEY(video,idx));\n"
            "            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY K"
            "EY,video TEXT,name TEXT,kind TEXT,profile INTEGER,\n"
            "                deps TEXT,state TEXT,attempts INTEGER,due REA"
            "L,lease TEXT,expires REAL,output TEXT,error TEXT,\n"
            "                priority INTEGER,UNIQUE(video,name));\n"
            "            CREATE TABLE IF NOT EXISTS completions(video TEXT"
            " PRIMARY KEY,assets TEXT,handled INTEGER);\n"
            "        "
        )
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

    def close(self):
        with self.lock:
            self.db.close()

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
        data = (
            base64.urlsafe_b64encode(
                json.dumps(value, separators=(",", ":")).encode()
            )
            .decode()
            .rstrip("=")
        )
        return (
            data
            + "."
            + hmac.new(self.secret, data.encode(), hashlib.sha256).hexdigest()
        )

    def verify(self, token, kind):
        """Validate a signed grant and return its decoded claims.

        Args:
            token: Signed upload or playback grant.
            kind: Required grant kind, either "upload" or "play".

        Returns:
            A claims dictionary whose signature, kind, and expiry were checked.

        Raises:
            PermissionError: The grant is invalid, expired, or of another kind.
        """
        try:
            if not isinstance(token, str) or len(token) > 2048:
                raise ValueError()
            data, signature = token.split(".")
            expected = hmac.new(
                self.secret, data.encode(), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError()
            claims = json.loads(
                base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
            )
            if claims["kind"] != kind or claims["expires"] <= self.clock():
                raise ValueError()
            return claims
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise PermissionError("invalid or expired grant") from None

    def _video(self, video):
        row = self.db.execute(
            "SELECT * FROM videos WHERE id=?", (video,)
        ).fetchone()
        if row is None:
            raise ValueError("video not found")
        return dict(row)

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
            or (not isinstance(size, int) or isinstance(size, bool))
            or not 1 <= size <= self.max_bytes
            or (not isinstance(part_size, int) or isinstance(part_size, bool))
            or not 1 <= part_size <= 8 * 1024**2
            or math.ceil(size / part_size) > 4096
            or (not isinstance(ttl, int) or isinstance(ttl, bool))
            or not 1 <= ttl <= 86400
        ):
            raise ValueError("invalid upload size/title/grant lifetime")
        video = uuid.uuid4().hex
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO videos"
                " VALUES(?,?,?,?,?,'UPLOADING',NULL,'{}',?,?)",
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
        if (
            not isinstance(ttl, int) or isinstance(ttl, bool)
        ) or not 1 <= ttl <= 86400:
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
                for i in range(math.ceil(value["size"] / value["part_size"]))
            ],
        }

    def put_part(self, grant, data, checksum=None):
        """Store one authorized part, rejecting retries with different content.

        Args:
            grant: Unexpired signed upload grant for this part.
            data: Bytes whose length matches the grant's declared part size.
            checksum: Optional expected SHA-256 hex digest.

        Returns:
            True if stored for the first time, False for an identical retry.

        Raises:
            PermissionError: The grant is invalid or expired.
            ValueError: Content, checksum, retry, or upload state is invalid.
        """
        claims = self.verify(grant, "upload")
        if not isinstance(data, bytes) or len(data) != claims["size"]:
            raise ValueError("part length mismatch")
        key = hashlib.sha256(data).hexdigest()
        if checksum is not None and checksum != key:
            raise ValueError("part checksum mismatch")
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            video = self._video(claims["video"])
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
            self.objects.put(data)
            self.db.execute(
                "INSERT INTO parts VALUES(?,?,?,?)",
                (video["id"], claims["part"], key, len(data)),
            )
            return True

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
        return {k: value[k] for k in ("id", "title", "size", "state")} | {
            "uploaded_parts": parts
        }

    def finish(self, token, video, checksum=None, priority=0):
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
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise ValueError("invalid task priority")
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
                return video
            rows = self.db.execute(
                "SELECT idx,key,size FROM parts WHERE video=? ORDER BY idx",
                (video,),
            ).fetchall()
            if [r[0] for r in rows] != list(
                range(math.ceil(value["size"] / value["part_size"]))
            ):
                raise ValueError("missing upload parts")
        key, size = self.objects.assemble([r[1] for r in rows])
        if size != value["size"] or checksum and checksum != key:
            raise ValueError("source checksum/size mismatch")
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            state = self._video(video)["state"]
            if state in ("PROCESSING", "READY"):
                return video
            if state != "UPLOADING":
                raise ValueError("video removed during finalization")
            jobs = [
                ("inspect", "inspect", None, []),
                ("thumbnail", "thumbnail", None, ["inspect"]),
            ]
            jobs += [
                (f"encode-{p}", "encode", p, ["inspect"]) for p in self.profiles
            ]
            jobs += [
                (
                    "publish",
                    "publish",
                    None,
                    [job[0] for job in jobs if job[0] != "inspect"],
                )
            ]
            for name, kind, profile, deps in jobs:
                self.db.execute(
                    "INSERT INTO jobs"
                    " VALUES(?,?,?,?,?,?,'QUEUED',0,0,NULL,0,NULL,NULL,?)",
                    (
                        uuid.uuid4().hex,
                        video,
                        name,
                        kind,
                        profile,
                        json.dumps(deps),
                        priority,
                    ),
                )
            self.db.execute(
                "UPDATE videos SET state='PROCESSING',source=? WHERE id=?",
                (key, video),
            )
        return video

    def _cancel(self, video):
        self.db.execute(
            "UPDATE videos SET state='FAILED' WHERE id=? AND"
            " state='PROCESSING'",
            (video,),
        )
        self.db.execute(
            "UPDATE jobs SET state='CANCELLED' WHERE video=? AND state IN"
            " ('QUEUED','RUNNING')",
            (video,),
        )

    def claim(self, worker, lease_seconds=120, max_attempts=3):
        """Lease one ready job after recovering expired attempts.

        Args:
            worker: Diagnostic worker identifier attached to the lease.
            lease_seconds: Positive duration for the exclusive execution lease.
            max_attempts: Maximum execution attempts before cancelling the DAG.

        Returns:
            A job dictionary containing lease and video_data, or None if none
            is ready.
        """
        if lease_seconds <= 0 or max_attempts < 1:
            raise ValueError("invalid lease/attempt bounds")
        now = self.clock()
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            for row in self.db.execute(
                "SELECT * FROM jobs WHERE state='RUNNING' AND expires<=?",
                (now,),
            ).fetchall():
                if row["attempts"] >= max_attempts:
                    self.db.execute(
                        "UPDATE jobs SET state='FAILED',error='lease attempts"
                        " exhausted' WHERE id=?",
                        (row["id"],),
                    )
                    self._cancel(row["video"])
                else:
                    self.db.execute(
                        "UPDATE jobs SET state='QUEUED',lease=NULL,due=? WHERE"
                        " id=?",
                        (now, row["id"]),
                    )
            for row in self.db.execute(
                "SELECT * FROM jobs WHERE state='QUEUED' AND due<=? ORDER BY"
                " priority DESC,rowid",
                (now,),
            ).fetchall():
                states = dict(
                    self.db.execute(
                        "SELECT name,state FROM jobs WHERE video=?",
                        (row["video"],),
                    )
                )
                if not all(
                    states.get(dep) == "DONE" for dep in json.loads(row["deps"])
                ):
                    continue
                token = uuid.uuid4().hex
                self.db.execute(
                    "UPDATE jobs SET"
                    " state='RUNNING',lease=?,expires=?,attempts=attempts+1"
                    " WHERE id=?",
                    (token, now + lease_seconds, row["id"]),
                )
                job = dict(row)
                job.update(
                    lease=token,
                    expires=now + lease_seconds,
                    attempts=row["attempts"] + 1,
                    worker=worker,
                )
                job["video_data"] = self._video(row["video"])
                return job
        return None

    def complete(self, job, output):
        """Persist output and completion events only for the current live
        lease.

        Args:
            job: Job dictionary returned by claim, including its lease token.
            output: JSON-serializable execution result or published asset
                mapping.

        Returns:
            True if accepted, False if the lease is stale or expired.
        """
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            current = self.db.execute(
                "SELECT * FROM jobs WHERE id=?", (job["id"],)
            ).fetchone()
            if (
                current["state"] != "RUNNING"
                or current["lease"] != job["lease"]
                or current["expires"] <= self.clock()
                or self._video(job["video"])["state"] != "PROCESSING"
            ):
                return False
            self.db.execute(
                "UPDATE jobs SET state='DONE',output=? WHERE id=?",
                (json.dumps(output), job["id"]),
            )
            if job["kind"] == "publish":
                self.db.execute(
                    "INSERT OR IGNORE INTO completions VALUES(?,?,0)",
                    (job["video"], json.dumps(output)),
                )
            return True

    def fail(self, job, error, fatal=False, max_attempts=3, retry_base=1):
        """Record an attempt failure and retry with exponential backoff.

        Args:
            job: Claimed job, including its lease token and attempt count.
            error: Failure whose string representation is retained, capped at
                300 chars.
            fatal: Cancel the DAG without retrying when True.
            max_attempts: Attempt limit before terminal failure.
            retry_base: Initial retry delay in seconds.

        Returns:
            True if recorded, False if the lease is stale or expired.
        """
        with self.lock, self.db:
            current = self.db.execute(
                "SELECT state,lease,expires FROM jobs WHERE id=?", (job["id"],)
            ).fetchone()
            if (
                current["state"] != "RUNNING"
                or current["lease"] != job["lease"]
                or current["expires"] <= self.clock()
            ):
                return False
            failed = fatal or job["attempts"] >= max_attempts
            self.db.execute(
                "UPDATE jobs SET state=?,error=?,due=?,lease=NULL WHERE id=?",
                (
                    "FAILED" if failed else "QUEUED",
                    str(error)[:300],
                    self.clock() + retry_base * 2 ** (job["attempts"] - 1),
                    job["id"],
                ),
            )
            if failed:
                self._cancel(job["video"])
            return True

    def outputs(self, video):
        """Return completed job outputs keyed by task name for video."""
        with self.lock:
            return {
                row["name"]: json.loads(row["output"])
                for row in self.db.execute(
                    "SELECT name,output FROM jobs WHERE video=? AND"
                    " state='DONE'",
                    (video,),
                )
            }

    def handle_completions(self):
        """Publish available completion outbox entries and return the count
        handled.
        """
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            rows = self.db.execute(
                "SELECT * FROM completions WHERE handled=0"
            ).fetchall()
            handled = 0
            for row in rows:
                assets = json.loads(row["assets"])
                if self._video(row["video"])["state"] == "PROCESSING" and (
                    not assets
                    or "master.m3u8" not in assets
                    or any(
                        not self.objects.file(key).is_file()
                        for key in assets.values()
                    )
                ):
                    # Preserve the outbox until object storage recovers.
                    continue
                self.db.execute(
                    "UPDATE videos SET state='READY',assets=? WHERE id=? AND"
                    " state='PROCESSING'",
                    (row["assets"], row["video"]),
                )
                self.db.execute(
                    "UPDATE completions SET handled=1 WHERE video=?",
                    (row["video"],),
                )
                handled += 1
            return handled

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
        if (
            not isinstance(ttl, int) or isinstance(ttl, bool)
        ) or not 1 <= ttl <= 3600:
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
            value = self._video(claims["video"])
            if value["state"] != "READY":
                raise PermissionError("video unavailable")
            key = json.loads(value["assets"]).get(resource)
            if key is None:
                raise ValueError("asset not found")
            return self.objects.file(key)

    def remove(self, token, video):
        """Mark an owned video REMOVED and cancel its queued and running
        jobs.
        """
        owner = self.authenticate(token)
        with self.lock, self.db:
            value = self._video(video)
            if value["owner"] != owner:
                raise PermissionError("not the owner")
            self.db.execute(
                "UPDATE videos SET state='REMOVED' WHERE id=?", (video,)
            )
            self.db.execute(
                "UPDATE jobs SET state='CANCELLED' WHERE video=? AND state IN"
                " ('QUEUED','RUNNING')",
                (video,),
            )

    def read_small(self, path):
        """Return an object's bytes, retaining it within the bounded byte
        cache.
        """
        key = path.name
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            value = path.read_bytes()
            if len(value) <= self.cache_bytes:
                self.cache[key] = value
                self.cached_bytes += len(value)
                while self.cached_bytes > self.cache_bytes:
                    _, old = self.cache.popitem(last=False)
                    self.cached_bytes -= len(old)
            return value

    def cleanup(self, min_age=3600, upload_age=86401):
        """Local conservative GC; young writes and active task attempts are
        retained.
        """
        if min_age < 300 or upload_age < 86401:
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
            references = set()

            def collect(value):
                if isinstance(value, dict):
                    for child in value.values():
                        collect(child)
                elif isinstance(value, str) and re.fullmatch(
                    r"[0-9a-f]{64}", value
                ):
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
                "SELECT output FROM jobs WHERE output IS NOT NULL AND video IN"
                " (SELECT id FROM videos WHERE state NOT IN"
                " ('REMOVED','EXPIRED'))"
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
                for r in self.db.execute(
                    "SELECT lease FROM jobs WHERE state='RUNNING' AND"
                    " expires>?",
                    (self.clock(),),
                )
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
            self.cache.clear()
            self.cached_bytes = 0
        return {
            "expired_uploads": expired,
            "removed_objects": deleted,
            "removed_attempts": attempts,
        }
