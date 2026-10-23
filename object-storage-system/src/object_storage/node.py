"""Small chunks share a checksummed append file; CURRENT is authoritative."""

import os
from pathlib import Path
import struct
import threading
import time
import uuid
import zlib

from .common import CHUNK, Database, Error, checksum, sha

HEADER = struct.Struct(">4sI32sd")
FOOTER = struct.Struct(">I")
SMALL = 64 * 1024


class DataNode(Database):
    def __init__(self, root, clock=time.time):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.large = self.root / "large"
        self.large.mkdir(exist_ok=True)
        self.clock, self.lock = clock, threading.RLock()
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
        tmp = self.root / ("current-" + uuid.uuid4().hex + ".tmp")
        with tmp.open("w") as handle:
            handle.write(name)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.root / "CURRENT")

    @staticmethod
    def record(handle, key, data, created, delete=False):
        header = HEADER.pack(
            b"S3D1" if delete else b"S3P1", len(data), bytes.fromhex(key), created
        )
        offset = handle.tell() + HEADER.size
        handle.write(header + data + FOOTER.pack(zlib.crc32(header + data)))
        return offset

    def recover(self):
        """Rebuild the disposable index; truncate only an incomplete final record."""
        with self.lock, self.transaction(True) as db:
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
                data = path.read_bytes()
                if len(data) <= CHUNK and sha(data) == key:
                    db.execute(
                        "INSERT OR REPLACE INTO chunks VALUES(?,'large',0,?,?)",
                        (key, len(data), path.stat().st_mtime),
                    )

    def get(self, key):
        checksum(key)
        with self.lock, self.transaction() as db:
            row = db.execute("SELECT * FROM chunks WHERE hash=?", (key,)).fetchone()
            if not row:
                raise Error(404, "chunk missing")
            if row["kind"] == "large":
                try:
                    data = (self.large / (key + ".dat")).read_bytes()
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
        if len(data) > CHUNK or sha(data) != key:
            raise Error(400, "chunk size/checksum mismatch")
        with self.lock:
            try:
                if self.get(key) == data:
                    return {"hash": key, "size": len(data), "duplicate": True}
            except Error as exc:
                if exc.status not in (404, 409):
                    raise
            created = self.clock()
            if len(data) <= SMALL:
                with self.pack.open("ab") as handle:
                    offset = self.record(handle, key, data, created)
                    handle.flush()
                    os.fsync(handle.fileno())
                kind = "pack"
            else:
                path = self.large / (key + ".dat")
                tmp = self.large / (uuid.uuid4().hex + ".tmp")
                with tmp.open("xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, path)
                offset, kind = 0, "large"
            with self.transaction(True) as db:
                db.execute(
                    "INSERT OR REPLACE INTO chunks VALUES(?,?,?,?,?)",
                    (key, kind, offset, len(data), created),
                )
            return {"hash": key, "size": len(data), "duplicate": False}

    def collect(self, live, grace=60):
        live = {checksum(key) for key in live}
        removed = 0
        with self.lock:
            with self.transaction() as db:
                candidates = [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM chunks WHERE created<=?", (self.clock() - grace,)
                    )
                    if r["hash"] not in live
                ]
            for row in candidates:
                if row["kind"] == "pack":
                    with self.pack.open("ab") as handle:
                        self.record(handle, row["hash"], b"", self.clock(), True)
                        handle.flush()
                        os.fsync(handle.fileno())
                else:
                    (self.large / (row["hash"] + ".dat")).unlink(missing_ok=True)
                with self.transaction(True) as db:
                    db.execute("DELETE FROM chunks WHERE hash=?", (row["hash"],))
                removed += 1
        return {"removed": removed}

    def compact(self):
        with self.lock:
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
            self.switch(new.name)
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
        with self.lock, self.transaction() as db:
            return {
                "chunks": db.execute("SELECT count(*) FROM chunks").fetchone()[0],
                "packed_chunks": db.execute(
                    "SELECT count(*) FROM chunks WHERE kind='pack'"
                ).fetchone()[0],
                "pack": self.pack.name,
                "pack_bytes": self.pack.stat().st_size,
                "large_files": len(list(self.large.glob("*.dat"))),
            }
