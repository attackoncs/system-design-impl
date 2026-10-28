"""Small chunks share a checksummed append file; CURRENT is authoritative."""

import math
import os
from pathlib import Path
import re
import struct
import time
import uuid
import zlib

from .common import CHUNK, Database, Error, checksum, integer, sha
from .node_lock import NodeLock

HEADER = struct.Struct(">4sI32sd")
FOOTER = struct.Struct(">I")
SMALL = 64 * 1024


class DataNode(Database):
    def __init__(self, root, clock=time.time):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.large = self.root / "large"
        self.large.mkdir(exist_ok=True)
        self.clock, self.lock = clock, NodeLock()
        self._needs_recovery = False
        self.gc_generation = uuid.uuid4().hex
        super().__init__(self.root / "index.sqlite")
        self.initialize(
            "CREATE TABLE IF NOT EXISTS chunks(hash TEXT PRIMARY KEY,kind TEXT,offset INTEGER,size INTEGER,created REAL);"
        )
        if not (self.root / "CURRENT").exists():
            pack = "pack-" + uuid.uuid4().hex + ".bin"
            (self.root / pack).touch()
            self.switch(pack)
        name = (self.root / "CURRENT").read_text().strip()
        if (
            not name.startswith("pack-")
            or Path(name).name != name
            or not name.endswith(".bin")
        ):
            raise Error(409, "invalid pack generation")
        self.pack = self.root / name
        self.recover()

    def switch(self, name):
        if (
            not isinstance(name, str)
            or not name.startswith("pack-")
            or Path(name).name != name
            or not name.endswith(".bin")
            or not (self.root / name).is_file()
        ):
            raise Error(409, "invalid pack generation")
        tmp = self.root / ("current-" + uuid.uuid4().hex + ".tmp")
        try:
            with tmp.open("x", encoding="ascii") as handle:
                handle.write(name)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.root / "CURRENT")
        finally:
            tmp.unlink(missing_ok=True)

    def _healthy(self):
        if self._needs_recovery:
            raise Error(503, "node requires successful recovery before serving")

    def _now(self):
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise ValueError("invalid node clock")
        return now

    def _append(self, key, data, created, *, delete=False):
        """Roll back an unacknowledged partial append before any later write."""
        with self.pack.open("r+b", buffering=0) as handle:
            handle.seek(0, os.SEEK_END)
            start = handle.tell()
            try:
                offset = self.record(handle, key, data, created, delete)
                handle.flush()
                os.fsync(handle.fileno())
                return offset
            except BaseException:
                try:
                    handle.truncate(start)
                    handle.flush()
                    os.fsync(handle.fileno())
                except BaseException:
                    self._needs_recovery = True
                raise

    @staticmethod
    def record(handle, key, data, created, delete=False):
        header = HEADER.pack(
            b"S3D1" if delete else b"S3P1", len(data), bytes.fromhex(key), created
        )
        offset = handle.tell() + HEADER.size
        raw = header + data + FOOTER.pack(zlib.crc32(header + data))
        if handle.write(raw) != len(raw):
            raise OSError("incomplete pack record write")
        return offset

    def recover(self):
        with self.lock:
            self.gc_generation = uuid.uuid4().hex
            self._needs_recovery = True
            self._recover()
            self._needs_recovery = False

    def _recover(self):
        """Rebuild the disposable index; truncate only an incomplete final record."""
        with self.lock, self.transaction(True) as db:
            self._needs_recovery = True
            db.execute("DELETE FROM chunks")
            with self.pack.open("r+b") as handle:
                while True:
                    start = handle.tell()
                    header = handle.read(HEADER.size)
                    if not header:
                        break
                    if len(header) < HEADER.size:
                        handle.truncate(start)
                        break
                    magic, size, key, created = HEADER.unpack(header)
                    if (
                        magic not in (b"S3P1", b"S3D1")
                        or size > SMALL
                        or (magic == b"S3D1" and size)
                        or not math.isfinite(created)
                        or created < 0
                    ):
                        raise Error(
                            409, "pack header corrupt; operator recovery required"
                        )
                    data = handle.read(size)
                    footer = handle.read(FOOTER.size)
                    if len(data) != size or len(footer) != FOOTER.size:
                        handle.truncate(start)
                        break
                    key = key.hex()
                    if FOOTER.unpack(footer)[0] != zlib.crc32(header + data):
                        db.execute("DELETE FROM chunks WHERE hash=?", (key,))
                        continue  # Leave corrupt record unreachable; another node can repair.
                    if magic == b"S3D1":
                        db.execute("DELETE FROM chunks WHERE hash=?", (key,))
                    elif sha(data) == key:
                        db.execute(
                            "INSERT OR REPLACE INTO chunks VALUES(?,'pack',?,?,?)",
                            (key, start + HEADER.size, size, created),
                        )
                handle.flush()
                os.fsync(handle.fileno())
            for path in self.large.glob("*.dat"):
                key = path.stem
                checksum(key)
                with path.open("rb") as handle:
                    data = handle.read(CHUNK + 1)
                if len(data) <= CHUNK and sha(data) == key:
                    db.execute(
                        "INSERT OR REPLACE INTO chunks VALUES(?,'large',0,?,?)",
                        (key, len(data), path.stat().st_mtime),
                    )

    def get(self, key):
        checksum(key)
        with self.lock.read(), self.transaction() as db:
            self._healthy()
            row = db.execute("SELECT * FROM chunks WHERE hash=?", (key,)).fetchone()
            if not row:
                raise Error(404, "chunk missing")
            if (
                row["kind"] not in ("large", "pack")
                or type(row["size"]) is not int
                or not 0 <= row["size"] <= CHUNK
            ):
                raise Error(409, "invalid chunk index")
            if row["kind"] == "large":
                try:
                    with (self.large / (key + ".dat")).open("rb") as handle:
                        data = handle.read(CHUNK + 1)
                except FileNotFoundError:
                    raise Error(404, "chunk file missing") from None
            else:
                with self.pack.open("rb") as handle:
                    handle.seek(row["offset"])
                    data = handle.read(row["size"])
            if len(data) != row["size"] or sha(data) != key:
                raise Error(409, "chunk integrity check failed")
            return data

    def put(self, key, data):
        checksum(key)
        if not isinstance(data, bytes) or len(data) > CHUNK or sha(data) != key:
            raise Error(400, "chunk size/checksum mismatch")
        with self.lock:
            self._healthy()
            # Even an existing immutable chunk may acquire a new metadata
            # reference. Invalidate collectors that captured an older live set
            # before accepting this publication intent.
            self.gc_generation = uuid.uuid4().hex
            try:
                if self.get(key) == data:
                    return {"hash": key, "size": len(data), "duplicate": True}
            except Error as exc:
                if exc.status not in (404, 409):
                    raise
            created = self._now()
            if len(data) <= SMALL:
                offset = self._append(key, data, created)
                kind = "pack"
            else:
                path = self.large / (key + ".dat")
                tmp = self.large / (uuid.uuid4().hex + ".tmp")
                try:
                    with tmp.open("xb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(tmp, path)
                finally:
                    tmp.unlink(missing_ok=True)
                offset, kind = 0, "large"
            try:
                with self.transaction(True) as db:
                    db.execute(
                        "INSERT OR REPLACE INTO chunks VALUES(?,?,?,?,?)",
                        (key, kind, offset, len(data), created),
                    )
            except BaseException:
                self._needs_recovery = True
                raise
            return {"hash": key, "size": len(data), "duplicate": False}

    def collect(self, live, grace=60, *, generation=None):
        integer(grace, "grace", 0, 86400)
        if generation is not None and (
            not isinstance(generation, str)
            or not re.fullmatch(r"[a-f0-9]{32}", generation)
        ):
            raise ValueError("invalid GC generation")
        if not isinstance(live, (list, tuple, set, frozenset)) or len(live) > 50000:
            raise ValueError("live references require at most 50000 hashes")
        live = {checksum(key) for key in live}
        removed = 0
        with self.lock:
            self._healthy()
            if generation is not None and generation != self.gc_generation:
                raise Error(409, "GC generation changed; rebuild live references")
            # A retried collector cannot act on a generation whose collection
            # may already have committed before its acknowledgement was lost.
            self.gc_generation = uuid.uuid4().hex
            now = self._now()
            with self.transaction() as db:
                candidates = [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM chunks WHERE created<=?", (now - grace,)
                    )
                    if r["hash"] not in live
                ]
            for row in candidates:
                if row["kind"] == "pack":
                    self._append(row["hash"], b"", now, delete=True)
                else:
                    (self.large / (row["hash"] + ".dat")).unlink(missing_ok=True)
                try:
                    with self.transaction(True) as db:
                        db.execute("DELETE FROM chunks WHERE hash=?", (row["hash"],))
                except BaseException:
                    self._needs_recovery = True
                    raise
                removed += 1
        return {"removed": removed}

    def compact(self):
        with self.lock:
            self._healthy()
            self.gc_generation = uuid.uuid4().hex
            old = self.pack
            new = self.root / ("pack-" + uuid.uuid4().hex + ".bin")
            with self.transaction() as db:
                rows = [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM chunks WHERE kind='pack' ORDER BY hash"
                    )
                ]
            with new.open("xb") as handle:
                for row in rows:
                    self.record(
                        handle, row["hash"], self.get(row["hash"]), row["created"]
                    )
                handle.flush()
                os.fsync(handle.fileno())
            self._needs_recovery = True
            try:
                self.switch(new.name)
            except BaseException:
                # Publication can succeed even if control is interrupted before
                # switch returns. Do not append acknowledged data to an older
                # generation after CURRENT has already moved forward.
                try:
                    current = (self.root / "CURRENT").read_text().strip()
                except OSError:
                    current = None
                if current == old.name:
                    self._needs_recovery = False
                elif current == new.name:
                    self.pack = new
                raise
            self.pack = new
            self.recover()
            old_size = old.stat().st_size
            for path in self.root.glob("pack-*.bin"):
                if path != new:
                    path.unlink()
            return {
                "packed_chunks": len(rows),
                "old_pack_bytes": old_size,
                "pack_bytes": new.stat().st_size,
            }

    def status(self):
        with self.lock.read(), self.transaction() as db:
            return {
                "chunks": db.execute("SELECT count(*) FROM chunks").fetchone()[0],
                "packed_chunks": db.execute(
                    "SELECT count(*) FROM chunks WHERE kind='pack'"
                ).fetchone()[0],
                "pack": self.pack.name,
                "pack_bytes": self.pack.stat().st_size,
                "large_files": len(list(self.large.glob("*.dat"))),
                "needs_recovery": self._needs_recovery,
                "gc_generation": self.gc_generation,
            }
