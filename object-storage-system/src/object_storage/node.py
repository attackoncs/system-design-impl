"""Small chunks share checksummed append-only pack files; CURRENT is authoritative.

CURRENT lists the pack set, one name per line: sealed packs oldest first, then
the single active pack that receives appends. Before an append would grow the
active pack past ``pack_limit`` it is sealed (fsynced, made read-only) and a
fresh active pack is published. Each chunk row names the pack holding it.

Invariant: a pack is sealed only when the index covers every byte of it, and a
sealed pack never changes again. ``sealed_packs`` mirrors the sealed part of
CURRENT (name and size); ``pack_state`` records how far the active pack is
indexed and only advances in the same SQLite transaction as the chunk-row
change of a record that was already fsynced. When both match CURRENT, a
restart scans only the active pack's un-indexed tail. Any mismatch (including
a crash inside rotation or compaction) falls back to a full rebuild that
replays every pack listed in CURRENT in order, so later tombstones and
re-uploads win over earlier records.
"""

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
PACK_LIMIT = 256 * 1024 * 1024


def _pack_name(name):
    return (
        isinstance(name, str)
        and name.startswith("pack-")
        and name.endswith(".bin")
        and Path(name).name == name
    )


class DataNode(Database):
    def __init__(self, root, clock=time.time, pack_limit=PACK_LIMIT):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.large = self.root / "large"
        self.large.mkdir(exist_ok=True)
        self.clock, self.lock = clock, NodeLock()
        self.pack_limit = integer(pack_limit, "pack_limit", 1, 2**40)
        self._needs_recovery = False
        self.gc_generation = uuid.uuid4().hex
        super().__init__(self.root / "index.sqlite")
        self.initialize(
            "CREATE TABLE IF NOT EXISTS chunks(hash TEXT PRIMARY KEY,kind TEXT,offset INTEGER,size INTEGER,created REAL,pack TEXT);"
            "CREATE TABLE IF NOT EXISTS pack_state(id INTEGER PRIMARY KEY CHECK(id=1),pack TEXT,last INTEGER,end INTEGER);"
            "CREATE TABLE IF NOT EXISTS sealed_packs(seq INTEGER PRIMARY KEY,name TEXT UNIQUE,size INTEGER);"
        )
        if not (self.root / "CURRENT").exists():
            self.switch(self._new_pack().name)
        self._load()
        with self.transaction(True) as db:
            # Indexes written before rotation have no pack column. Their rows
            # all belong to the legacy single pack, which is CURRENT's only
            # entry; ALTER and UPDATE commit together. A stale legacy index
            # still fails the pack_state check below and is fully rebuilt.
            columns = [row["name"] for row in db.execute("PRAGMA table_info(chunks)")]
            if "pack" not in columns:
                db.execute("ALTER TABLE chunks ADD COLUMN pack TEXT")
                db.execute(
                    "UPDATE chunks SET pack=? WHERE kind='pack'", (self.pack.name,)
                )
        self.recover(full=False)

    def _manifest(self):
        """Return CURRENT as (sealed names oldest first, active name)."""
        names = (self.root / "CURRENT").read_text(encoding="ascii").split()
        if (
            not names
            or len(set(names)) != len(names)
            or not all(_pack_name(name) for name in names)
        ):
            raise Error(409, "invalid pack generation")
        return tuple(names[:-1]), names[-1]

    def _load(self):
        sealed, active = self._manifest()
        for name in (*sealed, active):
            if not (self.root / name).is_file():
                raise Error(409, "pack listed in CURRENT is missing: " + name)
        self.sealed, self.pack = sealed, self.root / active

    def _sync_dir(self):
        descriptor = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _new_pack(self):
        path = self.root / ("pack-" + uuid.uuid4().hex + ".bin")
        with path.open("xb") as handle:
            os.fsync(handle.fileno())
        self._sync_dir()
        return path

    def switch(self, names):
        """Atomically publish a pack set: sealed names in order, active last."""
        names = [names] if isinstance(names, str) else list(names)
        if (
            not names
            or len(set(names)) != len(names)
            or not all(_pack_name(n) and (self.root / n).is_file() for n in names)
        ):
            raise Error(409, "invalid pack generation")
        tmp = self.root / ("current-" + uuid.uuid4().hex + ".tmp")
        try:
            with tmp.open("x", encoding="ascii") as handle:
                handle.write("\n".join(names) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.root / "CURRENT")
        finally:
            tmp.unlink(missing_ok=True)
        self._sync_dir()

    def _publish(self, names, created=()):
        """Switch CURRENT to names and leave the node unhealthy until reindexed.

        If the switch fails, CURRENT is re-read: an unchanged pack set keeps
        serving (unpublished new files are removed); a published one is
        adopted, so later writes never go to a pack that is no longer active.
        """
        old = (self.sealed, self.pack.name)
        self._needs_recovery = True
        try:
            self.switch(names)
        except BaseException:
            try:
                current = self._manifest()
            except (OSError, Error):
                current = None
            if current == old:
                self._needs_recovery = False
                for path in created:
                    path.unlink(missing_ok=True)
            elif current is not None:
                self._load()
            raise
        self.sealed, self.pack = tuple(names[:-1]), self.root / names[-1]

    def _rotate(self):
        """Seal the active pack and start an empty one (caller holds the lock).

        Order: make the index cover the whole (already fsynced) pack, create and
        fsync the new empty pack, publish CURRENT, then commit sealed_packs and
        pack_state together. A crash before the publish leaves the old layout
        plus an unlisted empty file that startup deletes; a crash after it
        leaves CURRENT ahead of the index, which forces a full rebuild.
        """
        old = self.pack
        with self.transaction(True) as db:
            indexed = self._indexed(db)
            if indexed is None:
                self._needs_recovery = True
                raise Error(503, "active pack index state lost; recovery required")
            # Establish the seal invariant: apply any complete records past
            # the indexed prefix (e.g. a corrupt final record found by a
            # restart scan) so every byte of the sealed pack is accounted for.
            self._scan(db, old, *indexed)
        size = old.stat().st_size
        new = self._new_pack()
        self._publish([*self.sealed, old.name, new.name], created=[new])
        try:
            with self.transaction(True) as db:
                db.execute(
                    "INSERT INTO sealed_packs(name,size) VALUES(?,?)", (old.name, size)
                )
                db.execute(
                    "INSERT OR REPLACE INTO pack_state VALUES(1,?,NULL,0)", (new.name,)
                )
        except BaseException:
            self._needs_recovery = True
            raise
        self._needs_recovery = False
        old.chmod(0o444)

    def _healthy(self):
        if self._needs_recovery:
            raise Error(503, "node requires successful recovery before serving")

    def _now(self):
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise ValueError("invalid node clock")
        return now

    def _append(self, key, data, created, *, delete=False):
        """Append to the active pack, rotating first if it would overflow.

        Roll back an unacknowledged partial append before any later write.
        """
        size = self.pack.stat().st_size
        if size and size + HEADER.size + len(data) + FOOTER.size > self.pack_limit:
            self._rotate()
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

    def recover(self, full=True):
        with self.lock:
            self.gc_generation = uuid.uuid4().hex
            self._needs_recovery = True
            self._recover(full)
            self._needs_recovery = False

    def _advance(self, db, offset, size):
        """Extend the indexed prefix only when this record directly follows it."""
        start = offset - HEADER.size
        db.execute(
            "UPDATE pack_state SET last=?,end=? WHERE id=1 AND pack=? AND end=?",
            (start, offset + size + FOOTER.size, self.pack.name, start),
        )

    def _indexed(self, db):
        """Return (last record start, end) if the stored prefix matches the pack."""
        row = db.execute("SELECT * FROM pack_state WHERE id=1").fetchone()
        if not row or row["pack"] != self.pack.name or type(row["end"]) is not int:
            return None
        last, end = row["last"], row["end"]
        if end == 0 and last is None:
            return None, 0
        if type(last) is not int or not 0 <= last < end:
            return None
        with self.pack.open("rb") as handle:
            handle.seek(last)
            header = handle.read(HEADER.size)
            if len(header) != HEADER.size:
                return None
            magic, size, _, _ = HEADER.unpack(header)
            if (
                magic not in (b"S3P1", b"S3D1")
                or last + HEADER.size + size + FOOTER.size != end
            ):
                return None
            data = handle.read(size)
            footer = handle.read(FOOTER.size)
        if len(footer) != FOOTER.size or FOOTER.unpack(footer)[0] != zlib.crc32(
            header + data
        ):
            return None
        return last, end

    def _sealed_indexed(self, db):
        """True if sealed_packs matches CURRENT's sealed list and file sizes."""
        rows = db.execute("SELECT name,size FROM sealed_packs ORDER BY seq").fetchall()
        return tuple(row["name"] for row in rows) == self.sealed and all(
            (self.root / row["name"]).stat().st_size == row["size"] for row in rows
        )

    def _scan(self, db, path, last=None, end=0, active=True):
        """Apply records from offset end onward; return (last, end) of the scan.

        Only the active pack may end in an incomplete record (a write that was
        never acknowledged), which is truncated. Sealed packs were fully
        indexed when sealed, so a short sealed pack needs operator recovery.
        """
        with path.open("r+b" if active else "rb") as handle:
            handle.seek(end)
            while True:
                start = handle.tell()
                header = handle.read(HEADER.size)
                if not header:
                    break
                if len(header) < HEADER.size:
                    if not active:
                        raise Error(409, "sealed pack truncated: " + path.name)
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
                    raise Error(409, "pack header corrupt; operator recovery required")
                data = handle.read(size)
                footer = handle.read(FOOTER.size)
                if len(data) != size or len(footer) != FOOTER.size:
                    if not active:
                        raise Error(409, "sealed pack truncated: " + path.name)
                    handle.truncate(start)
                    break
                key = key.hex()
                if FOOTER.unpack(footer)[0] != zlib.crc32(header + data):
                    db.execute("DELETE FROM chunks WHERE hash=?", (key,))
                    continue  # Leave corrupt record unreachable; another node can repair.
                last, end = start, handle.tell()
                if magic == b"S3D1":
                    db.execute("DELETE FROM chunks WHERE hash=?", (key,))
                elif sha(data) == key:
                    db.execute(
                        "INSERT OR REPLACE INTO chunks(hash,kind,offset,size,created,pack)"
                        " VALUES(?,'pack',?,?,?,?)",
                        (key, start + HEADER.size, size, created, path.name),
                    )
            if active:
                handle.flush()
                os.fsync(handle.fileno())
        return last, end

    def _recover(self, full=True):
        """Index the active pack's tail after the verified prefix, or rebuild.

        A full rebuild runs when requested, for an old index without pack
        state, or when sealed_packs/pack_state do not match CURRENT. It replays
        every listed pack in order. Files not listed in CURRENT are leftovers of
        an interrupted rotation or compaction and are deleted.
        """
        with self.lock, self.transaction(True) as db:
            self._needs_recovery = True
            self._load()
            prefix = None if full or not self._sealed_indexed(db) else self._indexed(db)
            if prefix is None:
                db.execute("DELETE FROM chunks")
                db.execute("DELETE FROM sealed_packs")
                for name in self.sealed:
                    path = self.root / name
                    self._scan(db, path, active=False)
                    db.execute(
                        "INSERT INTO sealed_packs(name,size) VALUES(?,?)",
                        (name, path.stat().st_size),
                    )
            last, end = self._scan(db, self.pack, *(prefix or (None, 0)))
            db.execute(
                "INSERT OR REPLACE INTO pack_state VALUES(1,?,?,?)",
                (self.pack.name, last, end),
            )
            listed = {*self.sealed, self.pack.name}
            for path in self.root.glob("pack-*.bin"):
                if path.name not in listed:
                    path.unlink()
            for path in self.root.glob("current-*.tmp"):
                path.unlink()
            for name in self.sealed:
                (self.root / name).chmod(0o444)
            # Indexed large files get a size check (get() verifies SHA-256 on
            # every read); unindexed files, e.g. a crash before the index
            # commit, are hashed. Rows whose file vanished are dropped.
            indexed = {
                row["hash"]: row["size"]
                for row in db.execute(
                    "SELECT hash,size FROM chunks WHERE kind='large'"
                )
            }
            for path in self.large.glob("*.dat"):
                key = path.stem
                checksum(key)
                if indexed.pop(key, None) == path.stat().st_size:
                    continue
                with path.open("rb") as handle:
                    data = handle.read(CHUNK + 1)
                if len(data) <= CHUNK and sha(data) == key:
                    db.execute(
                        "INSERT OR REPLACE INTO chunks(hash,kind,offset,size,created,pack)"
                        " VALUES(?,'large',0,?,?,NULL)",
                        (key, len(data), path.stat().st_mtime),
                    )
                else:
                    indexed[key] = None
            for key in indexed:
                db.execute("DELETE FROM chunks WHERE hash=? AND kind='large'", (key,))

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
                if row["pack"] != self.pack.name and row["pack"] not in self.sealed:
                    raise Error(409, "invalid chunk index")
                with (self.root / row["pack"]).open("rb") as handle:
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
                kind, pack = "pack", self.pack.name  # _append may have rotated.
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
                offset, kind, pack = 0, "large", None
            try:
                with self.transaction(True) as db:
                    db.execute(
                        "INSERT OR REPLACE INTO chunks(hash,kind,offset,size,created,pack)"
                        " VALUES(?,?,?,?,?,?)",
                        (key, kind, offset, len(data), created, pack),
                    )
                    if kind == "pack":
                        self._advance(db, offset, len(data))
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
                offset = None
                if row["kind"] == "pack":
                    # The tombstone always goes to the active pack, even when
                    # the chunk lives in a sealed one; replay order applies it.
                    offset = self._append(row["hash"], b"", now, delete=True)
                else:
                    (self.large / (row["hash"] + ".dat")).unlink(missing_ok=True)
                try:
                    with self.transaction(True) as db:
                        db.execute("DELETE FROM chunks WHERE hash=?", (row["hash"],))
                        if offset is not None:
                            self._advance(db, offset, 0)
                except BaseException:
                    self._needs_recovery = True
                    raise
                removed += 1
        return {"removed": removed}

    def _write_packs(self, rows):
        """Write live rows into new packs of at most pack_limit bytes each."""
        paths, handle = [], None
        try:
            for row in rows:
                data = self.get(row["hash"])
                needed = HEADER.size + len(data) + FOOTER.size
                if handle is None or (
                    handle.tell() and handle.tell() + needed > self.pack_limit
                ):
                    if handle is not None:
                        os.fsync(handle.fileno())
                        handle.close()
                    paths.append(self.root / ("pack-" + uuid.uuid4().hex + ".bin"))
                    handle = paths[-1].open("xb")
                self.record(handle, row["hash"], data, row["created"])
                handle.flush()
            if handle is not None:
                os.fsync(handle.fileno())
                handle.close()
            self._sync_dir()
        except BaseException:
            if handle is not None:
                handle.close()
            for path in paths:
                path.unlink(missing_ok=True)
            raise
        return paths

    def compact(self):
        """Merge the live records of all sealed packs into new sealed packs.

        The active pack is sealed first (if non-empty), so compaction only
        reads immutable packs and never rewrites a pack that receives appends.
        All sealed packs are merged together, which makes dropping their
        tombstones safe: no older copy of a deleted chunk survives anywhere.
        New packs are fsynced before CURRENT names them; a full rebuild then
        reindexes the new layout and deletes the unlisted inputs.
        """
        with self.lock:
            self._healthy()
            self.gc_generation = uuid.uuid4().hex
            if self.pack.stat().st_size:
                self._rotate()
            old = self.sealed
            old_size = sum((self.root / name).stat().st_size for name in old)
            with self.transaction() as db:
                rows = [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM chunks WHERE kind='pack' ORDER BY hash"
                    )
                ]
            if any(row["pack"] not in old for row in rows):
                raise Error(409, "invalid chunk index")
            new = self._write_packs(rows)
            self._publish([path.name for path in new] + [self.pack.name], new)
            self.recover()
            return {
                "packed_chunks": len(rows),
                "old_pack_bytes": old_size,
                "pack_bytes": sum(path.stat().st_size for path in new),
                "sealed_packs": len(new),
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
                "sealed_packs": len(self.sealed),
                "sealed_bytes": sum(
                    (self.root / name).stat().st_size for name in self.sealed
                ),
                "large_files": len(list(self.large.glob("*.dat"))),
                "needs_recovery": self._needs_recovery,
                "gc_generation": self.gc_generation,
            }
