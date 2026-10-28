"""Transactional revisions, upload reservations, ACLs and durable device events."""

from contextlib import contextmanager
import hashlib
import math
from collections import OrderedDict
import json
from pathlib import PurePosixPath
import re
import secrets
import sqlite3
import threading
import time
import uuid

from .storage import BlockStore, CorruptBlock
from .paths import safe_path as safe_path
from .manifests import normalize
from .notifications import EventLog
from .lifecycle import Operations, owned
from .schema import SCHEMA


class Conflict(ValueError):
    pass


class Drive:
    def __init__(self, db, objects, block_size=4 * 1024**2, clock=time.time):
        if type(block_size) is not int or not 1 <= block_size <= 4 * 1024**2:
            raise ValueError("block size must be 1..4 MiB")
        self.clock, self.block_size = clock, block_size
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.lock = threading.RLock()
        self._operations = Operations(self.lock)
        self._closed = False
        self._event_epoch = 0
        self._manifests = OrderedDict()
        self._manifest_bytes = 0
        self.db = sqlite3.connect(db, timeout=10, check_same_thread=False)
        try:
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
                        raise
                    time.sleep(0.01)
            self.db.executescript(SCHEMA)
            with self.transaction():
                self.db.execute(
                    "INSERT OR IGNORE INTO settings VALUES('master',?)",
                    (secrets.token_hex(32),),
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO settings VALUES('block_size',?)",
                    (str(block_size),),
                )
            configured = int(
                self.db.execute(
                    "SELECT value FROM settings WHERE key='block_size'"
                ).fetchone()[0]
            )
            if configured != block_size:
                self.db.close()
                raise ValueError("block size differs from persisted configuration")
            master = bytes.fromhex(
                self.db.execute(
                    "SELECT value FROM settings WHERE key='master'"
                ).fetchone()[0]
            )
            self.store = BlockStore(objects, master, block_size)
            self.events = EventLog(
                self.db,
                self.read,
                self.transaction,
                self._operations.condition,
                self._operations.stopping,
            )
        except BaseException:
            self.db.close()
            raise

    @contextmanager
    def transaction(self):
        with self._operations.condition:
            epoch = self._event_epoch
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                yield
            if self._event_epoch != epoch:
                self._operations.condition.notify_all()

    def close(self):
        """Wake long polls and drain accepted I/O before closing metadata."""
        with self._operations.condition:
            self._operations.drain()
            if not self._closed:
                self.db.close()
                self._closed = True
                self._manifests.clear()
                self._manifest_bytes = 0

    @contextmanager
    def read(self):
        with self.lock:
            if self.db.in_transaction:
                yield
            else:
                with self.db:
                    self.db.execute("BEGIN")
                    yield

    @owned
    def issue(self, user, quota=10 * 1024**3):
        if (
            not isinstance(user, str)
            or not 1 <= len(user) <= 100
            or type(quota) is not int
            or not 0 <= quota < 2**63
        ):
            raise ValueError("invalid user/quota")
        token = secrets.token_urlsafe(32)
        with self.transaction():
            self.db.execute("INSERT OR IGNORE INTO users VALUES(?,?)", (user, quota))
            self.db.execute(
                "INSERT INTO tokens VALUES(?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), user),
            )
        return token

    @owned
    def auth(self, token):
        if not isinstance(token, str) or not 1 <= len(token) <= 256:
            raise PermissionError("invalid credential")
        with self.lock:
            row = self.db.execute(
                "SELECT user FROM tokens WHERE token=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
        if row is None:
            raise PermissionError("invalid credential")
        return row[0]

    @owned
    def access(self, user, space, write=False):
        with self.read():
            row = self.db.execute(
                "SELECT role FROM members WHERE space=? AND user=?", (space, user)
            ).fetchone()
            if row is None or write and row[0] not in ("owner", "write"):
                raise PermissionError("workspace permission required")

    @owned
    def create_space(self, token, name):
        user = self.auth(token)
        if not isinstance(name, str) or not 1 <= len(name) <= 100:
            raise ValueError("invalid workspace name")
        space = uuid.uuid4().hex
        with self.transaction():
            self.db.execute("INSERT INTO spaces VALUES(?,?,?)", (space, user, name))
            self.db.execute("INSERT INTO members VALUES(?,?,'owner')", (space, user))
        return space

    @owned
    def device(self, token, device_id):
        """Register an owned device and recover its durable acknowledgement."""
        return self.events.device(self.auth(token), device_id)

    @owned
    def quota(self, owner):
        with self.read():
            used = self.db.execute(
                "SELECT COALESCE(SUM(r.size),0) FROM revisions r JOIN files f ON r.file=f.id JOIN spaces s ON f.space=s.id WHERE s.owner=?",
                (owner,),
            ).fetchone()[0]
            reserved = self.db.execute(
                "SELECT COALESCE(SUM(u.size),0) FROM uploads u JOIN spaces s ON u.space=s.id WHERE s.owner=? AND u.state='OPEN'",
                (owner,),
            ).fetchone()[0]
            maximum = self.db.execute(
                "SELECT quota FROM users WHERE name=?", (owner,)
            ).fetchone()[0]
            return {"used": used, "reserved": reserved, "limit": maximum}

    @owned
    def begin(
        self, token, space, path, size, checksum, blocks, base=None, request=None
    ):
        user = self.auth(token)
        path = safe_path(path)
        if (
            base is not None
            and (
                not isinstance(base, str) or re.fullmatch(r"[0-9a-f]{32}", base) is None
            )
            or not isinstance(request, str)
            or not 1 <= len(request) <= 100
        ):
            raise ValueError("invalid base/request")
        encoded = normalize(size, checksum, blocks, self.block_size)
        with self.transaction():
            self.access(user, space, True)
            prior = self.db.execute(
                "SELECT * FROM uploads WHERE user=? AND request=?", (user, request)
            ).fetchone()
            if prior is not None:
                if (
                    prior["space"],
                    prior["path"],
                    prior["size"],
                    prior["checksum"],
                    prior["blocks"],
                    prior["base"],
                ) != (space, path, size, checksum, encoded, base):
                    raise Conflict("request ID reused with a different upload")
                if prior["state"] == "CANCELLED":
                    raise ValueError("upload cancelled")
                upload = prior["id"]
            else:
                owner = self.db.execute(
                    "SELECT owner FROM spaces WHERE id=?", (space,)
                ).fetchone()[0]
                quota = self.quota(owner)
                if quota["used"] + quota["reserved"] + size > quota["limit"]:
                    raise ValueError("quota exceeded")
                upload = uuid.uuid4().hex
                self.db.execute(
                    "INSERT INTO uploads VALUES(?,?,?,?,?,?,?,?,'OPEN',NULL,?,?)",
                    (
                        upload,
                        user,
                        space,
                        path,
                        base,
                        size,
                        checksum,
                        encoded,
                        self.clock(),
                        request,
                    ),
                )
                self.emit("pending", space, path=path)
        return self.upload_status(token, upload)

    @owned
    def session(self, user, upload):
        row = self.db.execute("SELECT * FROM uploads WHERE id=?", (upload,)).fetchone()
        if row is None:
            raise ValueError("unknown upload")
        if row["user"] != user:
            raise PermissionError("not session owner")
        self.access(user, row["space"], True)
        return row

    def _block_rows(self, space, manifest):
        """Read only declared digests in bounded parameter batches."""
        digests = sorted({block["digest"] for block in manifest})
        rows = {}
        for start in range(0, len(digests), 256):
            batch = digests[start : start + 256]
            marks = ",".join("?" for _ in batch)
            for row in self.db.execute(
                "SELECT * FROM blocks WHERE space=? AND digest IN (" + marks + ")",
                (space, *batch),
            ):
                rows[row["digest"]] = row
        return rows

    @owned
    def upload(self, token, space, path, data, base=None, request=None):
        """Simple single-block upload using the same resumable acceptance path."""
        if not isinstance(data, bytes) or len(data) > self.block_size:
            raise ValueError("simple upload exceeds one block")
        checksum = hashlib.sha256(data).hexdigest()
        blocks = [{"digest": checksum, "size": len(data)}] if data else []
        session = self.begin(
            token,
            space,
            path,
            len(data),
            checksum,
            blocks,
            base,
            uuid.uuid4().hex if request is None else request,
        )
        if session["state"] == "OPEN" and session["missing"]:
            self.put(token, session["upload"], checksum, data)
        return self.commit(token, session["upload"])

    @owned
    def upload_status(self, token, upload):
        """Report missing content without holding the metadata lock during I/O."""
        user = self.auth(token)
        with self.read():
            row = self.session(user, upload)
            manifest = json.loads(row["blocks"]) if row["state"] == "OPEN" else []
            stored = self._block_rows(row["space"], manifest)
        missing = sorted(
            {
                block["digest"]
                for block in manifest
                if block["digest"] not in stored
                or not self.store.exists(stored[block["digest"]]["key"])
            }
        )
        with self.read():
            row = self.session(user, upload)
            return {
                "upload": upload,
                "state": row["state"],
                "missing": missing if row["state"] == "OPEN" else [],
                "result": json.loads(row["result"]) if row["result"] else None,
            }

    @owned
    def put(self, token, upload, digest, data):
        """Compress/encrypt outside the DB lock, then fence reference acceptance."""
        user = self.auth(token)
        with self.read():
            row = self.session(user, upload)
            if row["state"] != "OPEN":
                raise ValueError("upload is not open")
            match = next(
                (b for b in json.loads(row["blocks"]) if b["digest"] == digest), None
            )
            if (
                match is None
                or not isinstance(data, bytes)
                or len(data) != match["size"]
            ):
                raise ValueError("undeclared/invalid block")
            old = self.db.execute(
                "SELECT * FROM blocks WHERE space=? AND digest=?",
                (row["space"], digest),
            ).fetchone()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("checksum mismatch")
        reused = False
        if old is not None and self.store.exists(old["key"]):
            try:
                self.store.get(row["space"], digest, len(data))
                reused = True
            except CorruptBlock:
                pass
        key = old["key"] if reused else self.store.put(row["space"], digest, data)
        with self.transaction():
            current = self.session(user, upload)
            if current["state"] != "OPEN":
                raise ValueError("upload is not open")
            if not self.store.exists(key):
                raise OSError("written block disappeared; retry")
            self.db.execute(
                "INSERT INTO blocks VALUES(?,?,?,?,0,?) ON CONFLICT(space,digest) "
                "DO UPDATE SET key=excluded.key,size=excluded.size",
                (row["space"], digest, len(data), key, self.clock()),
            )
            return not reused

    @owned
    def emit(self, kind, space, file=None, revision=None, path=None, users=None):
        """Append events inside the current metadata transaction."""
        self.events.emit(kind, space, file, revision, path, users)
        self._event_epoch += 1

    @owned
    def commit(self, token, upload):
        user = self.auth(token)
        with self.read():
            snapshot = self.session(user, upload)
            if snapshot["state"] == "DONE":
                return json.loads(snapshot["result"])
            if snapshot["state"] != "OPEN":
                raise ValueError("upload cancelled")
            manifest = json.loads(snapshot["blocks"])
        digest = hashlib.sha256()
        for block in manifest:
            digest.update(
                self.store.get(snapshot["space"], block["digest"], block["size"])
            )
        if digest.hexdigest() != snapshot["checksum"]:
            raise ValueError("file checksum mismatch")
        with self.transaction():
            row = self.session(user, upload)
            if row["state"] == "DONE":
                return json.loads(row["result"])
            if row["state"] != "OPEN":
                raise ValueError("upload cancelled")
            stored_rows = self._block_rows(row["space"], manifest)
            for block in manifest:
                stored = stored_rows.get(block["digest"])
                if stored is None or stored["size"] != block["size"]:
                    raise ValueError("block metadata missing")
                if not self.store.exists(stored["key"]):
                    raise OSError("validated block disappeared; retry")
            current = self.db.execute(
                "SELECT * FROM files WHERE space=? AND path=?",
                (row["space"], row["path"]),
            ).fetchone()
            conflict = (current["head"] if current else None) != row["base"]
            path = row["path"]
            if conflict:
                parent = PurePosixPath(path)
                path = str(
                    parent.with_name(
                        parent.stem[:40]
                        + ".conflict-"
                        + upload[:12]
                        + parent.suffix[:15]
                    )
                )
                if len(path) > 240:
                    # A near-limit parent path may leave no room for the suffix.
                    path = PurePosixPath(path).name
                current = None
                if self.db.execute(
                    "SELECT 1 FROM files WHERE space=? AND path=?", (row["space"], path)
                ).fetchone():
                    raise Conflict("conflict path collision")
            file = current["id"] if current else uuid.uuid4().hex
            safe_path(path)
            for other in self.db.execute(
                "SELECT f.id,f.path FROM files f JOIN revisions r ON f.head=r.id WHERE f.space=? AND r.deleted=0",
                (row["space"],),
            ):
                if other["id"] != file:
                    a, b = other["path"].casefold(), path.casefold()
                    if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                        raise Conflict(
                            "file/directory or case-insensitive path collision"
                        )
            revision = uuid.uuid4().hex
            if current is None:
                self.db.execute(
                    "INSERT INTO files VALUES(?,?,?,NULL)", (file, row["space"], path)
                )
            self.db.execute(
                "INSERT INTO revisions(id,file,size,checksum,blocks,deleted,created) VALUES(?,?,?,?,?,0,?)",
                (
                    revision,
                    file,
                    row["size"],
                    row["checksum"],
                    row["blocks"],
                    self.clock(),
                ),
            )
            for block in manifest:
                self.db.execute(
                    "UPDATE blocks SET refs=refs+1 WHERE space=? AND digest=?",
                    (row["space"], block["digest"]),
                )
            self.db.execute("UPDATE files SET head=? WHERE id=?", (revision, file))
            result = {
                "file": file,
                "revision": revision,
                "path": path,
                "conflict": conflict,
            }
            self.db.execute(
                "UPDATE uploads SET state='DONE',result=? WHERE id=?",
                (json.dumps(result), upload),
            )
            self.emit("upsert", row["space"], file, revision, path)
            return result

    def _revision(self, user, file, revision):
        """Resolve live authorization/head and cache only immutable block tuples."""
        row = self.db.execute("SELECT * FROM files WHERE id=?", (file,)).fetchone()
        if row is None:
            raise ValueError("unknown file")
        self.access(user, row["space"])
        version = self.db.execute(
            "SELECT id,version,size,checksum,deleted,created FROM revisions WHERE id=? AND file=?",
            (revision or row["head"], file),
        ).fetchone()
        if version is None:
            raise ValueError("unknown revision")
        key = version["id"]
        if key not in self._manifests:
            encoded = self.db.execute(
                "SELECT blocks FROM revisions WHERE id=?", (key,)
            ).fetchone()[0]
            blocks = tuple(
                (block["digest"], block["size"]) for block in json.loads(encoded)
            )
            weight = len(encoded.encode("utf-8"))
            if weight <= 8 * 1024**2:
                self._manifests[key] = (weight, blocks)
                self._manifest_bytes += weight
                while len(self._manifests) > 128 or self._manifest_bytes > 8 * 1024**2:
                    _, (removed, _) = self._manifests.popitem(last=False)
                    self._manifest_bytes -= removed
            return row, version, blocks
        self._manifests.move_to_end(key)
        return row, version, self._manifests[key][1]

    @owned
    def metadata(self, token, file, revision=None):
        user = self.auth(token)
        with self.read():
            row, version, blocks = self._revision(user, file, revision)
            return {
                "file": file,
                "space": row["space"],
                "path": row["path"],
                "revision": version["id"],
                "version": version["version"],
                "size": version["size"],
                "checksum": version["checksum"],
                "deleted": bool(version["deleted"]),
                "blocks": [{"digest": digest, "size": size} for digest, size in blocks],
            }

    @owned
    def get_block(self, token, file, revision, index):
        """Read outside metadata locks; cache cannot bypass the live ACL check."""
        user = self.auth(token)
        with self.read():
            row, _version, blocks = self._revision(user, file, revision)
            space = row["space"]
            if type(index) is not int or not 0 <= index < len(blocks):
                raise ValueError("invalid block index")
            digest, size = blocks[index]
        data = self.store.get(space, digest, size)
        with self.read():
            self.access(user, space)
        return data

    @owned
    def list_files(self, token, space):
        user = self.auth(token)
        with self.read():
            self.access(user, space)
            return [
                self.metadata(token, r[0])
                for r in self.db.execute(
                    "SELECT id FROM files WHERE space=? ORDER BY path", (space,)
                ).fetchall()
            ]

    @owned
    def revisions(self, token, file, limit=None):
        """Read history, optionally bounded to the most recent revisions."""
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 1000):
            raise ValueError("invalid revision limit")
        with self.read():
            self.metadata(token, file)
            sql = "SELECT id,size,checksum,deleted,created FROM revisions WHERE file=? ORDER BY rowid DESC"
            params = (file,)
            if limit is not None:
                sql += " LIMIT ?"
                params += (limit,)
            return [dict(row) for row in self.db.execute(sql, params)]

    @owned
    def delete(self, token, file, base):
        user = self.auth(token)
        with self.transaction():
            meta = self.metadata(token, file)
            self.access(user, meta["space"], True)
            if meta["revision"] != base:
                raise Conflict("stale delete")
            if meta["deleted"]:
                return meta["revision"]
            revision = uuid.uuid4().hex
            self.db.execute(
                "INSERT INTO revisions(id,file,size,checksum,blocks,deleted,created) VALUES(?,?,0,?,'[]',1,?)",
                (revision, file, hashlib.sha256(b"").hexdigest(), self.clock()),
            )
            self.db.execute("UPDATE files SET head=? WHERE id=?", (revision, file))
            self.emit("delete", meta["space"], file, revision, meta["path"])
            return revision

    @owned
    def restore(self, token, file, revision, base, request):
        meta = self.metadata(token, file, revision)
        if meta["deleted"]:
            raise ValueError("cannot restore tombstone content")
        session = self.begin(
            token,
            meta["space"],
            meta["path"],
            meta["size"],
            meta["checksum"],
            meta["blocks"],
            base,
            request,
        )
        return self.commit(token, session["upload"])

    @owned
    def share(self, token, space, recipient, role):
        user = self.auth(token)
        if role not in ("read", "write", "revoke"):
            raise ValueError("invalid role")
        with self.transaction():
            row = self.db.execute(
                "SELECT owner FROM spaces WHERE id=?", (space,)
            ).fetchone()
            if row is None or row[0] != user or recipient == user:
                raise PermissionError("only owner may manage sharing")
            if (
                self.db.execute(
                    "SELECT 1 FROM users WHERE name=?", (recipient,)
                ).fetchone()
                is None
            ):
                raise ValueError("unknown recipient")
            if role == "revoke":
                self.db.execute(
                    "DELETE FROM members WHERE space=? AND user=?", (space, recipient)
                )
                self.emit("revoke", space, users=[recipient])
            else:
                self.db.execute(
                    "INSERT INTO members VALUES(?,?,?) ON CONFLICT(space,user) DO UPDATE SET role=excluded.role",
                    (space, recipient, role),
                )
                for file in self.db.execute(
                    "SELECT id,head,path FROM files WHERE space=?", (space,)
                ).fetchall():
                    self.emit("upsert", space, file[0], file[1], file[2], [recipient])
                self.emit("share", space, users=[recipient])

    @owned
    def changes(self, token, device, cursor, wait=0, limit=100, *, cancel=None):
        """Read-only polling; persist delivery only when a response is ready."""
        return self.events.changes(
            self.auth(token), device, cursor, wait, limit, cancel=cancel
        )

    @owned
    def ack(self, token, device, cursor):
        """Acknowledge only monotonic, previously delivered durable positions."""
        return self.events.ack(self.auth(token), device, cursor)

    @owned
    def cancel(self, token, upload):
        user = self.auth(token)
        with self.transaction():
            row = self.session(user, upload)
            if row["state"] == "OPEN":
                self.db.execute(
                    "UPDATE uploads SET state='CANCELLED' WHERE id=?", (upload,)
                )
                self.emit("cancelled", row["space"], path=row["path"])

    @owned
    def prune(self, token, file, keep=5):
        user = self.auth(token)
        if type(keep) is not int or keep < 1:
            raise ValueError("keep at least one revision")
        with self.transaction():
            meta = self.metadata(token, file)
            role = self.db.execute(
                "SELECT role FROM members WHERE space=? AND user=?",
                (meta["space"], user),
            ).fetchone()[0]
            if role != "owner":
                raise PermissionError("owner required")
            rows = self.db.execute(
                "SELECT id,blocks FROM revisions WHERE file=? ORDER BY rowid DESC",
                (file,),
            ).fetchall()
            remove = [r for r in rows[keep:] if r["id"] != meta["revision"]]
            for row in remove:
                for block in json.loads(row["blocks"]):
                    self.db.execute(
                        "UPDATE blocks SET refs=refs-1 WHERE space=? AND digest=?",
                        (meta["space"], block["digest"]),
                    )
                self.db.execute("DELETE FROM revisions WHERE id=?", (row["id"],))
            return len(remove)

    @owned
    def cleanup(self, grace=3600, upload_age=86400):
        try:
            valid = all(
                type(value) in (int, float) and math.isfinite(value)
                for value in (grace, upload_age)
            )
        except OverflowError:
            valid = False
        if not valid or grace < 300 or upload_age < 3600:
            raise ValueError("cleanup grace too short")
        with self.transaction():
            expired = self.db.execute(
                "SELECT space,path FROM uploads WHERE state='OPEN' AND created<?",
                (self.clock() - upload_age,),
            ).fetchall()
            for row in expired:
                self.emit("cancelled", row["space"], path=row["path"])
            self.db.execute(
                "UPDATE uploads SET state='CANCELLED' WHERE state='OPEN' AND created<?",
                (self.clock() - upload_age,),
            )
        with self.transaction():
            pinned = {
                (r["space"], b["digest"])
                for r in self.db.execute(
                    "SELECT space,blocks FROM uploads WHERE state='OPEN'"
                )
                for b in json.loads(r["blocks"])
            }
            removed = 0
            for row in self.db.execute(
                "SELECT * FROM blocks WHERE refs=0 AND created<?",
                (self.clock() - grace,),
            ).fetchall():
                if (row["space"], row["digest"]) not in pinned:
                    self.store.remove(row["key"])
                    self.db.execute(
                        "DELETE FROM blocks WHERE space=? AND digest=?",
                        (row["space"], row["digest"]),
                    )
                    removed += 1
            keys = {r[0] for r in self.db.execute("SELECT key FROM blocks")}
            for directory in (self.store.root, self.store.cold):
                for path in directory.iterdir():
                    if (
                        path.is_file()
                        and path.name not in keys
                        and time.time() - path.stat().st_mtime > grace
                    ):
                        path.resolve().relative_to(self.store.root)
                        if re.fullmatch(r"[0-9a-f]{64}", path.name):
                            self.store.remove(path.name)
                        else:
                            path.unlink()
                        removed += 1
            return {"removed_objects": removed}
