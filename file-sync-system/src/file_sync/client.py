"""Durable filesystem push/pull with bounded transport and delta caching."""

from functools import wraps
import hashlib
import math
import json
import os
import re
import stat
import threading
from pathlib import Path
import sqlite3
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
import uuid

from .paths import safe_path
from .manifests import normalize
from .client_transport import request as transport_request


def serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            if self._closed:
                raise RuntimeError("device client is closed")
            return method(self, *args, **kwargs)

    return call


class DeviceClient:
    def __init__(
        self, url, token, root, device, *, cache_bytes=64 * 1024**2, timeout=30
    ):
        if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 32 for c in url):
            raise ValueError("invalid API URL")
        parsed = urlsplit(url)
        try:
            valid = (
                parsed.scheme in ("http", "https")
                and parsed.hostname
                and parsed.port != 0
            )
        except ValueError:
            valid = False
        if (
            not valid
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid API URL")
        if (
            not isinstance(token, str)
            or not 1 <= len(token) <= 256
            or any(ord(c) < 32 for c in token)
        ):
            raise ValueError("invalid credential")
        if not isinstance(device, str) or not 1 <= len(device) <= 100:
            raise ValueError("invalid device id")
        if type(cache_bytes) is not int or cache_bytes < 0:
            raise ValueError("invalid cache capacity")
        try:
            valid_timeout = (
                type(timeout) in (int, float)
                and math.isfinite(timeout)
                and 0 < timeout <= threading.TIMEOUT_MAX
            )
        except OverflowError:
            valid_timeout = False
        if not valid_timeout:
            raise ValueError("invalid request timeout")
        self.url, self.token, self.device = url.rstrip("/"), token, device
        self.cache_bytes, self.timeout = cache_bytes, timeout
        self._lock, self._closed = threading.RLock(), False
        self._folder_handle, self.db = None, None
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.control = self.root / ".sync"
        if self.control.is_symlink():
            raise ValueError("state directory cannot be a symlink")
        self.control.mkdir(exist_ok=True)
        self.cache = self.control / "blocks"
        try:
            for target in (
                self.cache,
                self.control / "state.sqlite3",
                self.control / "lock",
            ):
                if target.is_symlink():
                    raise ValueError("state/cache paths cannot be symlinks")
            self.cache.mkdir(exist_ok=True)
            self._acquire_folder()
            for directory in (self.control, self.cache):
                for temporary in directory.iterdir():
                    if (
                        re.fullmatch(r"[0-9a-f]{32}\.tmp", temporary.name)
                        and not temporary.is_dir()
                    ):
                        temporary.unlink()
            self.db = sqlite3.connect(
                self.control / "state.sqlite3", check_same_thread=False
            )
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS files (
                    file TEXT PRIMARY KEY,path TEXT,revision TEXT,checksum TEXT,
                    space TEXT,version INTEGER,deleted INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE IF NOT EXISTS pending(path TEXT PRIMARY KEY,payload TEXT);
                CREATE TABLE IF NOT EXISTS local_copies(path TEXT PRIMARY KEY);
            """)
            if "deleted" not in {
                row[1] for row in self.db.execute("PRAGMA table_info(files)")
            }:
                self.db.execute(
                    "ALTER TABLE files ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0"
                )
            identity = json.dumps(
                [self.url, device, hashlib.sha256(token.encode()).hexdigest()]
            )
            existing = self.db.execute(
                "SELECT value FROM state WHERE key='identity'"
            ).fetchone()
            if existing and existing[0] != identity:
                raise ValueError("folder already belongs to another server/device")
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO state VALUES('identity',?)", (identity,)
                )
                self.db.execute("INSERT OR IGNORE INTO state VALUES('cursor','0')")
            block_size = self.request("GET", "/config")["block_size"]
            if type(block_size) is not int or not 1 <= block_size <= 4 * 1024**2:
                raise OSError("invalid server block size")
            self.block_size = block_size
            self.request("POST", "/device", {"device": device})
            bound = self.db.execute(
                "SELECT value FROM state WHERE key='space'"
            ).fetchone()
            self.space = bound[0] if bound else None
            self.transferred_up, self.transferred_down = 0, 0
            self._trim_cache()
        except BaseException:
            self.close()
            raise

    def _acquire_folder(self):
        stream = (self.control / "lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise RuntimeError("device folder is already in use") from None
        self._folder_handle = stream

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                if self.db is not None:
                    self.db.close()
            finally:
                if self._folder_handle is not None:
                    self._folder_handle.close()
                    self._folder_handle = None

    @serialized
    def request(self, method, route, value=None, binary=False):
        """Send one bounded request while owning the client operation lock."""
        return transport_request(
            self.url,
            self.token,
            method,
            route,
            value,
            binary,
            block_size=getattr(self, "block_size", 4 * 1024**2),
            timeout=self.timeout,
        )

    @serialized
    def path(self, relative):
        relative = safe_path(relative)
        path = self.root / relative
        path.resolve().relative_to(self.root)
        current = path
        while current != self.root:
            if current.is_symlink():
                raise ValueError("symlink paths are not synchronized")
            current = current.parent
        return path

    def _validate_metadata(self, meta):
        if not isinstance(meta, dict):
            raise OSError("invalid metadata object")
        for name, pattern in (
            ("file", r"[0-9a-f]{32}"),
            ("revision", r"[0-9a-f]{32}"),
            ("space", r"[0-9a-f]{32}"),
            ("checksum", r"[0-9a-f]{64}"),
        ):
            if (
                not isinstance(meta.get(name), str)
                or re.fullmatch(pattern, meta[name]) is None
            ):
                raise OSError("invalid metadata identity/checksum")
        safe_path(meta.get("path"))
        if (
            type(meta.get("version")) is not int
            or meta["version"] < 1
            or type(meta.get("size")) is not int
            or not 0 <= meta["size"] <= 10 * 1024**3
            or type(meta.get("deleted")) is not bool
            or not isinstance(meta.get("blocks"), list)
            or len(meta["blocks"]) > 10240
        ):
            raise OSError("invalid metadata bounds")
        try:
            normalize(meta["size"], meta["checksum"], meta["blocks"], self.block_size)
        except ValueError:
            raise OSError("invalid server block manifest") from None
        if meta["deleted"] and (
            meta["size"] or meta["checksum"] != hashlib.sha256(b"").hexdigest()
        ):
            raise OSError("invalid tombstone content")

    @staticmethod
    def _validate_batch(batch, cursor):
        if (
            not isinstance(batch, dict)
            or type(batch.get("cursor")) is not int
            or batch["cursor"] < cursor
            or not isinstance(batch.get("events"), list)
            or len(batch["events"]) > 1000
        ):
            raise OSError("invalid notification batch")
        previous = cursor
        for event in batch["events"]:
            if (
                not isinstance(event, dict)
                or type(event.get("seq")) is not int
                or not previous < event["seq"] <= batch["cursor"]
                or not isinstance(event.get("kind"), str)
            ):
                raise OSError("invalid notification order")
            previous = event["seq"]

    @staticmethod
    def _sync_directory(path):
        if os.name != "nt" and path.exists():
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _dirty(self, path, old, incoming):
        if not path.exists():
            return False
        if not path.is_file():
            return True
        digest = self.checksum(path)
        return digest != incoming and (old is None or digest != old[1])

    def _copy_conflict(self, source, destination, expected):
        temporary = self.control / (uuid.uuid4().hex + ".tmp")
        try:
            with source.open("rb") as reader, temporary.open("xb") as writer:
                for block in iter(lambda: reader.read(self.block_size), b""):
                    writer.write(block)
                writer.flush()
                os.fsync(writer.fileno())
            if self.checksum(temporary) != expected:
                raise OSError(
                    "file changed before conflict preservation; committed session retained"
                )
            if destination.exists():
                if self.checksum(destination) != expected:
                    raise OSError("conflict destination has different local data")
            else:
                os.replace(temporary, destination)
                self._sync_directory(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _cached(self, path, size):
        if path.is_symlink():
            raise ValueError("cache block cannot be a symlink")
        if not path.is_file() or path.stat().st_size != size:
            return None
        with path.open("rb") as stream:
            value = stream.read(self.block_size + 1)
        os.utime(path, None)
        return value

    def _cache_block(self, path, data):
        if len(data) > self.cache_bytes:
            return
        if self.control.is_symlink() or self.cache.is_symlink() or path.is_symlink():
            raise ValueError("cache path cannot be a symlink")
        temporary = self.cache / (uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        self._trim_cache()

    def _trim_cache(self):
        entries = []
        total = 0
        for path in self.cache.iterdir():
            if (
                re.fullmatch(r"[0-9a-f]{32}-[0-9a-f]{64}", path.name)
                and not path.is_symlink()
                and path.is_file()
            ):
                item = path.stat()
                entries.append((item.st_mtime_ns, path, item.st_size))
                total += item.st_size
        for _mtime, path, size in sorted(entries):
            if total <= self.cache_bytes:
                break
            path.unlink()
            total -= size

    def _reconcile_done(self, relative, checksum):
        pending = self.db.execute(
            "SELECT payload FROM pending WHERE path=?", (relative,)
        ).fetchone()
        if pending is None:
            return None
        value = json.loads(pending[0])
        try:
            session = self.request("POST", "/begin", value)
        except HTTPError as error:
            if error.code != 400:
                raise
            with self.db:
                self.db.execute("DELETE FROM pending WHERE path=?", (relative,))
            return None
        if session["state"] != "DONE":
            return None
        result = session["result"]
        meta = self.request(
            "GET",
            "/metadata?"
            + urlencode({"file": result["file"], "revision": result["revision"]}),
        )
        self._validate_metadata(meta)
        old = self.db.execute(
            "SELECT version FROM files WHERE file=?", (meta["file"],)
        ).fetchone()
        newer = old is not None and old[0] > meta["version"]
        if result["path"] != relative:
            self.apply(
                {
                    "kind": "upsert",
                    "file": result["file"],
                    "revision": result["revision"],
                }
            )
        else:
            self.remember(meta)
        with self.db:
            self.db.execute("DELETE FROM pending WHERE path=?", (relative,))
        return result if checksum == meta["checksum"] and not newer else None

    @staticmethod
    def checksum(path):
        digest = hashlib.sha256()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError("regular file required")
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @serialized
    def bind_space(self, space):
        """Permanently scope this device directory to one workspace."""
        if not isinstance(space, str) or re.fullmatch(r"[0-9a-f]{32}", space) is None:
            raise ValueError("invalid workspace identifier")
        bound = self.db.execute("SELECT value FROM state WHERE key='space'").fetchone()
        known = {row[0] for row in self.db.execute("SELECT DISTINCT space FROM files")}
        known.update(
            json.loads(row[0])["space"]
            for row in self.db.execute("SELECT payload FROM pending")
        )
        if bound and bound[0] != space or known - {space}:
            raise ValueError("device directory already contains another workspace")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO state VALUES('space',?)", (space,))
        self.space = space

    @serialized
    def remember(self, meta):
        self._validate_metadata(meta)
        with self.db:
            self.db.execute(
                "INSERT INTO files VALUES(?,?,?,?,?,?,?) ON CONFLICT(file) DO UPDATE SET path=excluded.path,revision=excluded.revision,checksum=excluded.checksum,space=excluded.space,version=excluded.version,deleted=excluded.deleted WHERE excluded.version>=files.version",
                (
                    meta["file"],
                    meta["path"],
                    meta["revision"],
                    meta["checksum"],
                    meta["space"],
                    meta["version"],
                    int(meta["deleted"]),
                ),
            )
            self.db.execute("DELETE FROM local_copies WHERE path=?", (meta["path"],))

    @serialized
    def push(self, space, relative):
        if self.space is not None and self.space != space:
            raise ValueError("device directory belongs to another workspace")
        path = self.path(relative)
        if not path.is_file():
            raise OSError("regular file required")
        before = path.stat()
        if (
            before.st_size > 10 * 1024**3
            or (before.st_size + self.block_size - 1) // self.block_size > 10240
        ):
            raise ValueError("file exceeds upload capacity")
        manifest, full = [], hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(self.block_size), b""):
                full.update(block)
                manifest.append(
                    {"digest": hashlib.sha256(block).hexdigest(), "size": len(block)}
                )
        if (before.st_size, before.st_mtime_ns) != (
            path.stat().st_size,
            path.stat().st_mtime_ns,
        ):
            raise OSError("file changed while hashing")
        reconciled = self._reconcile_done(relative, full.hexdigest())
        if reconciled is not None:
            return reconciled
        previous = self.db.execute(
            "SELECT revision,checksum,deleted FROM files WHERE space=? AND path=?",
            (space, relative),
        ).fetchone()
        if previous and not previous[2] and previous[1] == full.hexdigest():
            pending = self.db.execute(
                "SELECT payload FROM pending WHERE path=?", (relative,)
            ).fetchone()
            if pending is not None:
                old = self.request("POST", "/begin", json.loads(pending[0]))
                if old["state"] == "OPEN":
                    self.request("POST", "/cancel", {"upload": old["upload"]})
                with self.db:
                    self.db.execute("DELETE FROM pending WHERE path=?", (relative,))
            return {"unchanged": True}
        declaration = {
            "space": space,
            "path": relative,
            "size": before.st_size,
            "checksum": full.hexdigest(),
            "blocks": manifest,
            "base": previous[0] if previous else None,
        }
        pending = self.db.execute(
            "SELECT payload FROM pending WHERE path=?", (relative,)
        ).fetchone()
        value = json.loads(pending[0]) if pending else None
        if value is not None and any(value.get(k) != v for k, v in declaration.items()):
            old = self.request("POST", "/begin", value)
            if old["state"] == "OPEN":
                self.request("POST", "/cancel", {"upload": old["upload"]})
            value = None
        if value is None:
            value = dict(declaration, request=uuid.uuid4().hex)
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO pending VALUES(?,?)",
                    (relative, json.dumps(value)),
                )
        session = self.request("POST", "/begin", value)
        missing = set(session["missing"])
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(self.block_size), b""):
                digest = hashlib.sha256(block).hexdigest()
                if digest in missing:
                    self.request(
                        "PUT",
                        "/block?"
                        + urlencode({"upload": session["upload"], "digest": digest}),
                        block,
                        True,
                    )
                    self.transferred_up += len(block)
                    missing.remove(digest)
        if self.checksum(path) != value["checksum"]:
            raise OSError(
                "file changed while uploading; session retained for reconciliation"
            )
        result = self.request("POST", "/commit", {"upload": session["upload"]})
        meta = self.request(
            "GET",
            "/metadata?"
            + urlencode({"file": result["file"], "revision": result["revision"]}),
        )
        self._validate_metadata(meta)
        if result["path"] != relative:
            destination = self.path(result["path"])
            if destination.exists() and self.checksum(destination) != meta["checksum"]:
                raise OSError("conflict destination has different local data")
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Preserve the original dirty path until the canonical remote head is
            # pulled; this also makes a lost local-state update retryable.
            if not destination.exists():
                self._copy_conflict(path, destination, meta["checksum"])
        self.remember(meta)
        with self.db:
            self.db.execute("DELETE FROM pending WHERE path=?", (relative,))
        return result

    @serialized
    def preserve(self, path):
        target = path.with_name(
            path.stem[:40]
            + ".local-conflict-"
            + uuid.uuid4().hex[:12]
            + path.suffix[:15]
        )
        self.path(target.relative_to(self.root).as_posix())
        os.replace(path, target)
        self._sync_directory(path.parent)
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO local_copies VALUES(?)",
                (target.relative_to(self.root).as_posix(),),
            )

    @serialized
    def apply(self, event):
        if event["kind"] not in ("upsert", "delete"):
            return
        if self.space is not None and event.get("space", self.space) != self.space:
            return
        query = {"file": event["file"], "revision": event["revision"]}
        try:
            meta = self.request("GET", "/metadata?" + urlencode(query))
        except HTTPError as error:
            if error.code == 403:
                return  # Access was revoked; no new server data is downloaded.
            if error.code != 400:
                raise
            meta = self.request(
                "GET", "/metadata?" + urlencode({"file": event["file"]})
            )
        self._validate_metadata(meta)
        if self.space is not None and meta["space"] != self.space:
            return
        path = self.path(meta["path"])
        old = self.db.execute(
            "SELECT revision,checksum,version FROM files WHERE file=?", (meta["file"],)
        ).fetchone()
        if old and old[2] >= meta["version"]:
            return
        if meta["deleted"]:
            path = self.path(meta["path"])
            if self._dirty(path, old, meta["checksum"]):
                self.preserve(path)
            path.unlink(missing_ok=True)
            self._sync_directory(path.parent)
            self.remember(meta)
            return
        temporary = self.control / (uuid.uuid4().hex + ".tmp")
        digest = hashlib.sha256()
        try:
            with temporary.open("wb") as stream:
                for index, block in enumerate(meta["blocks"]):
                    cached = self.cache / (meta["space"] + "-" + block["digest"])
                    data = self._cached(cached, block["size"])
                    if (
                        data is None
                        or len(data) != block["size"]
                        or hashlib.sha256(data).hexdigest() != block["digest"]
                    ):
                        data = self.request(
                            "GET",
                            "/block?"
                            + urlencode(
                                {
                                    "file": meta["file"],
                                    "revision": meta["revision"],
                                    "index": index,
                                }
                            ),
                            binary=True,
                        )
                        if (
                            len(data) != block["size"]
                            or hashlib.sha256(data).hexdigest() != block["digest"]
                        ):
                            raise OSError("download checksum mismatch")
                        self._cache_block(cached, data)
                        self.transferred_down += len(data)
                    stream.write(data)
                    digest.update(data)
                stream.flush()
                os.fsync(stream.fileno())
            if (
                digest.hexdigest() != meta["checksum"]
                or temporary.stat().st_size != meta["size"]
            ):
                raise OSError("file checksum mismatch")
            path.parent.mkdir(parents=True, exist_ok=True)
            path = self.path(meta["path"])
            if self._dirty(path, old, meta["checksum"]):
                self.preserve(path)
            os.replace(temporary, path)
            self._sync_directory(path.parent)
            self.remember(meta)
        finally:
            temporary.unlink(missing_ok=True)

    @serialized
    def pull(self, wait=0):
        cursor = int(
            self.db.execute("SELECT value FROM state WHERE key='cursor'").fetchone()[0]
        )
        batch = self.request(
            "GET",
            "/changes?"
            + urlencode({"device": self.device, "cursor": cursor, "wait": wait}),
        )
        self._validate_batch(batch, cursor)
        for event in batch["events"]:
            self.apply(event)
        with self.db:
            self.db.execute(
                "UPDATE state SET value=? WHERE key='cursor'", (str(batch["cursor"]),)
            )
        self.request("POST", "/ack", {"device": self.device, "cursor": batch["cursor"]})
        return batch

    @serialized
    def delete(self, space, relative):
        row = self.db.execute(
            "SELECT file,revision FROM files WHERE space=? AND path=?",
            (space, relative),
        ).fetchone()
        if row is None:
            raise ValueError("file is not synchronized")
        try:
            result = self.request("POST", "/delete", {"file": row[0], "base": row[1]})
        except HTTPError as error:
            if error.code != 409:
                raise
            current = self.request("GET", "/metadata?" + urlencode({"file": row[0]}))
            self._validate_metadata(current)
            if not current["deleted"]:
                raise
            result = {"revision": current["revision"]}
        self.apply({"kind": "delete", "file": row[0], "revision": result["revision"]})
        return result
