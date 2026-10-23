"""Explicit filesystem push/pull client with persisted cursors and delta caching."""

import hashlib
import json
import os
from pathlib import Path
import sqlite3
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
import uuid

from .service import safe_path


class DeviceClient:
    def __init__(self, url, token, root, device):
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            raise ValueError("invalid API URL")
        self.url, self.token, self.device = url.rstrip("/"), token, device
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.control = self.root / ".sync"
        if self.control.is_symlink():
            raise ValueError("state directory cannot be a symlink")
        self.control.mkdir(exist_ok=True)
        self.cache = self.control / "blocks"
        if self.cache.is_symlink() or (self.control / "state.sqlite3").is_symlink():
            raise ValueError("state/cache paths cannot be symlinks")
        self.cache.mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.control / "state.sqlite3")
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS files(file TEXT PRIMARY KEY,path TEXT,revision TEXT,checksum TEXT,space TEXT,version INTEGER); CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE IF NOT EXISTS pending(path TEXT PRIMARY KEY,payload TEXT);"
        )
        identity = json.dumps(
            [self.url, device, hashlib.sha256(token.encode()).hexdigest()]
        )
        existing = self.db.execute(
            "SELECT value FROM state WHERE key='identity'"
        ).fetchone()
        if existing and existing[0] != identity:
            self.db.close()
            raise ValueError("folder already belongs to another server/device")
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO state VALUES('identity',?)", (identity,)
            )
            self.db.execute("INSERT OR IGNORE INTO state VALUES('cursor','0')")
        self.block_size = self.request("GET", "/config")["block_size"]
        self.request("POST", "/device", {"device": device})
        self.transferred_up, self.transferred_down = 0, 0

    def close(self):
        self.db.close()

    def request(self, method, route, value=None, binary=False):
        body = (
            value
            if binary
            else json.dumps(value).encode()
            if value is not None
            else None
        )
        request = Request(
            self.url + route,
            data=body,
            method=method,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/octet-stream"
                if binary
                else "application/json",
            },
        )
        with urlopen(request, timeout=30) as response:
            limit = self.block_size + 1 if binary and method == "GET" else 4 * 1024**2
            data = response.read(limit + 1)
            if len(data) > limit:
                raise OSError("response exceeded bound")
            return data if binary else json.loads(data)

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

    @staticmethod
    def checksum(path):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def remember(self, meta):
        with self.db:
            self.db.execute(
                "INSERT INTO files VALUES(?,?,?,?,?,?) ON CONFLICT(file) DO UPDATE SET path=excluded.path,revision=excluded.revision,checksum=excluded.checksum,space=excluded.space,version=excluded.version",
                (
                    meta["file"],
                    meta["path"],
                    meta["revision"],
                    meta["checksum"],
                    meta["space"],
                    meta["version"],
                ),
            )

    def push(self, space, relative):
        path = self.path(relative)
        if not path.is_file():
            raise ValueError("regular file required")
        before = path.stat()
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
        previous = self.db.execute(
            "SELECT revision,checksum FROM files WHERE space=? AND path=?",
            (space, relative),
        ).fetchone()
        if previous and previous[1] == full.hexdigest():
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
        meta = self.request("GET", "/metadata?" + urlencode({"file": result["file"]}))
        if result["path"] != relative:
            destination = self.path(result["path"])
            if destination.exists() and self.checksum(destination) != meta["checksum"]:
                raise OSError("conflict destination has different local data")
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Preserve the original dirty path until the canonical remote head is
            # pulled; this also makes a lost local-state update retryable.
            if not destination.exists():
                with path.open("rb") as source, destination.open("xb") as output:
                    for block in iter(lambda: source.read(self.block_size), b""):
                        output.write(block)
        self.remember(meta)
        with self.db:
            self.db.execute("DELETE FROM pending WHERE path=?", (relative,))
        return result

    def preserve(self, path):
        target = path.with_name(
            path.stem[:40]
            + ".local-conflict-"
            + uuid.uuid4().hex[:12]
            + path.suffix[:15]
        )
        self.path(target.relative_to(self.root).as_posix())
        os.replace(path, target)

    def apply(self, event):
        if event["kind"] not in ("upsert", "delete"):
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
        path = self.path(meta["path"])
        old = self.db.execute(
            "SELECT revision,checksum,version FROM files WHERE file=?", (meta["file"],)
        ).fetchone()
        if old and old[2] >= meta["version"]:
            return
        dirty = (
            path.exists()
            and (old is None or self.checksum(path) != old[1])
            and self.checksum(path) != meta["checksum"]
        )
        if meta["deleted"]:
            if dirty:
                self.preserve(path)
            path.unlink(missing_ok=True)
            self.remember(meta)
            return
        temporary = self.control / (uuid.uuid4().hex + ".tmp")
        digest = hashlib.sha256()
        try:
            with temporary.open("wb") as stream:
                for index, block in enumerate(meta["blocks"]):
                    cached = self.cache / (meta["space"] + "-" + block["digest"])
                    data = cached.read_bytes() if cached.exists() else None
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
                        cached.write_bytes(data)
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
            if dirty:
                self.preserve(path)
            os.replace(temporary, path)
            self.remember(meta)
        finally:
            temporary.unlink(missing_ok=True)

    def pull(self, wait=0):
        cursor = int(
            self.db.execute("SELECT value FROM state WHERE key='cursor'").fetchone()[0]
        )
        batch = self.request(
            "GET",
            "/changes?"
            + urlencode({"device": self.device, "cursor": cursor, "wait": wait}),
        )
        for event in batch["events"]:
            self.apply(event)
            with self.db:
                self.db.execute(
                    "UPDATE state SET value=? WHERE key='cursor'", (str(event["seq"]),)
                )
        with self.db:
            self.db.execute(
                "UPDATE state SET value=? WHERE key='cursor'", (str(batch["cursor"]),)
            )
        self.request("POST", "/ack", {"device": self.device, "cursor": batch["cursor"]})
        return batch

    def delete(self, space, relative):
        row = self.db.execute(
            "SELECT file,revision FROM files WHERE space=? AND path=?",
            (space, relative),
        ).fetchone()
        if row is None:
            raise ValueError("file is not synchronized")
        result = self.request("POST", "/delete", {"file": row[0], "base": row[1]})
        self.path(relative).unlink(missing_ok=True)
        return result
