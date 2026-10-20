"""Transactional revisions, upload reservations, ACLs and durable device events."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import PurePosixPath
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
import uuid

from .storage import BlockStore, CorruptBlock


class Conflict(ValueError):
    pass


def safe_path(value):
    if not isinstance(value, str) or not value or len(value) > 240 or value != unicodedata.normalize("NFC", value):
        raise ValueError("invalid relative path")
    parts = value.split("/")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{n}" for p in ("COM", "LPT") for n in range(1, 10)}
    if any(p in ("", ".", "..", ".sync") or p.endswith((".", " ")) or
           re.search(r'[<>:"\\|?*\x00-\x1f]', p) or p.split(".")[0].upper() in reserved for p in parts):
        raise ValueError("unsafe relative path")
    return value


class Drive:
    def __init__(self, db, objects, block_size=4 * 1024 ** 2, clock=time.time):
        if type(block_size) is not int or not 1 <= block_size <= 4 * 1024 ** 2:
            raise ValueError("block size must be 1..4 MiB")
        self.clock, self.block_size = clock, block_size
        self.lock = threading.RLock()
        self.db = sqlite3.connect(db, timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS users(name TEXT PRIMARY KEY,quota INTEGER);
            CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user TEXT);
            CREATE TABLE IF NOT EXISTS spaces(id TEXT PRIMARY KEY,owner TEXT,name TEXT);
            CREATE TABLE IF NOT EXISTS members(space TEXT,user TEXT,role TEXT,PRIMARY KEY(space,user));
            CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY,space TEXT,path TEXT,head TEXT,UNIQUE(space,path));
            CREATE TABLE IF NOT EXISTS revisions(version INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,file TEXT,size INTEGER,checksum TEXT,blocks TEXT,deleted INTEGER,created REAL);
            CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY,user TEXT,space TEXT,path TEXT,base TEXT,
                size INTEGER,checksum TEXT,blocks TEXT,state TEXT,result TEXT,created REAL,request TEXT,UNIQUE(user,request));
            CREATE TABLE IF NOT EXISTS blocks(space TEXT,digest TEXT,size INTEGER,key TEXT,refs INTEGER,created REAL,PRIMARY KEY(space,digest));
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,user TEXT,kind TEXT,space TEXT,file TEXT,revision TEXT,path TEXT);
            CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY,user TEXT,cursor INTEGER,delivered INTEGER);
            CREATE INDEX IF NOT EXISTS event_user ON events(user,seq);
        """)
        with self.transaction():
            self.db.execute("INSERT OR IGNORE INTO settings VALUES('master',?)", (secrets.token_hex(32),))
            self.db.execute("INSERT OR IGNORE INTO settings VALUES('block_size',?)", (str(block_size),))
        configured = int(self.db.execute("SELECT value FROM settings WHERE key='block_size'").fetchone()[0])
        if configured != block_size:
            self.db.close()
            raise ValueError("block size differs from persisted configuration")
        master = bytes.fromhex(self.db.execute("SELECT value FROM settings WHERE key='master'").fetchone()[0])
        self.store = BlockStore(objects, master, block_size)

    @contextmanager
    def transaction(self):
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            yield

    def close(self):
        self.db.close()

    @contextmanager
    def read(self):
        with self.lock:
            if self.db.in_transaction:
                yield
            else:
                with self.db:
                    self.db.execute("BEGIN")
                    yield

    def issue(self, user, quota=10 * 1024 ** 3):
        if not isinstance(user, str) or not 1 <= len(user) <= 100 or type(quota) is not int or quota < 0:
            raise ValueError("invalid user/quota")
        token = secrets.token_urlsafe(32)
        with self.transaction():
            self.db.execute("INSERT OR IGNORE INTO users VALUES(?,?)", (user, quota))
            self.db.execute("INSERT INTO tokens VALUES(?,?)", (hashlib.sha256(token.encode()).hexdigest(), user))
        return token

    def auth(self, token):
        if not isinstance(token, str) or not 1 <= len(token) <= 256:
            raise PermissionError("invalid credential")
        with self.lock:
            row = self.db.execute("SELECT user FROM tokens WHERE token=?", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if row is None:
            raise PermissionError("invalid credential")
        return row[0]

    def access(self, user, space, write=False):
        row = self.db.execute("SELECT role FROM members WHERE space=? AND user=?", (space, user)).fetchone()
        if row is None or write and row[0] not in ("owner", "write"):
            raise PermissionError("workspace permission required")

    def create_space(self, token, name):
        user = self.auth(token)
        if not isinstance(name, str) or not 1 <= len(name) <= 100:
            raise ValueError("invalid workspace name")
        space = uuid.uuid4().hex
        with self.transaction():
            self.db.execute("INSERT INTO spaces VALUES(?,?,?)", (space, user, name))
            self.db.execute("INSERT INTO members VALUES(?,?,'owner')", (space, user))
        return space

    def device(self, token, device_id):
        user = self.auth(token)
        if not isinstance(device_id, str) or not 1 <= len(device_id) <= 100:
            raise ValueError("invalid device id")
        with self.transaction():
            row = self.db.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            if row is not None and row["user"] != user:
                raise PermissionError("device belongs to another user")
            self.db.execute("INSERT OR IGNORE INTO devices VALUES(?,?,0,0)", (device_id, user))
            return dict(self.db.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone())

    def quota(self, owner):
        used = self.db.execute("SELECT COALESCE(SUM(r.size),0) FROM revisions r JOIN files f ON r.file=f.id JOIN spaces s ON f.space=s.id WHERE s.owner=?", (owner,)).fetchone()[0]
        reserved = self.db.execute("SELECT COALESCE(SUM(u.size),0) FROM uploads u JOIN spaces s ON u.space=s.id WHERE s.owner=? AND u.state='OPEN'", (owner,)).fetchone()[0]
        maximum = self.db.execute("SELECT quota FROM users WHERE name=?", (owner,)).fetchone()[0]
        return {"used": used, "reserved": reserved, "limit": maximum}

    def begin(self, token, space, path, size, checksum, blocks, base=None, request=None):
        user = self.auth(token)
        path = safe_path(path)
        if (type(size) is not int or not 0 <= size <= 10 * 1024 ** 3 or not re.fullmatch(r"[0-9a-f]{64}", str(checksum))
                or not isinstance(blocks, list) or len(blocks) > 10240
                or base is not None and not isinstance(base, str)
                or not isinstance(request, str) or not 1 <= len(request) <= 100):
            raise ValueError("invalid upload declaration")
        normalized = []
        for index, block in enumerate(blocks):
            if (not isinstance(block, dict) or set(block) != {"digest", "size"}
                    or not re.fullmatch(r"[0-9a-f]{64}", str(block["digest"]))
                    or type(block["size"]) is not int or not 0 < block["size"] <= self.block_size
                    or index < len(blocks) - 1 and block["size"] != self.block_size):
                raise ValueError("invalid block manifest")
            normalized.append({"digest": block["digest"], "size": block["size"]})
        if sum(b["size"] for b in normalized) != size:
            raise ValueError("manifest size mismatch")
        encoded = json.dumps(normalized, sort_keys=True)
        with self.transaction():
            self.access(user, space, True)
            prior = self.db.execute("SELECT * FROM uploads WHERE user=? AND request=?", (user, request)).fetchone()
            if prior is not None:
                if (prior["space"], prior["path"], prior["size"], prior["checksum"], prior["blocks"], prior["base"]) != (space, path, size, checksum, encoded, base):
                    raise Conflict("request ID reused with a different upload")
                if prior["state"] == "CANCELLED":
                    raise ValueError("upload cancelled")
                upload = prior["id"]
            else:
                owner = self.db.execute("SELECT owner FROM spaces WHERE id=?", (space,)).fetchone()[0]
                quota = self.quota(owner)
                if quota["used"] + quota["reserved"] + size > quota["limit"]:
                    raise ValueError("quota exceeded")
                upload = uuid.uuid4().hex
                self.db.execute("INSERT INTO uploads VALUES(?,?,?,?,?,?,?,?,'OPEN',NULL,?,?)",
                    (upload, user, space, path, base, size, checksum, encoded, self.clock(), request))
        return self.upload_status(token, upload)

    def session(self, user, upload):
        row = self.db.execute("SELECT * FROM uploads WHERE id=?", (upload,)).fetchone()
        if row is None:
            raise ValueError("unknown upload")
        if row["user"] != user:
            raise PermissionError("not session owner")
        self.access(user, row["space"], True)
        return row

    def upload_status(self, token, upload):
        user = self.auth(token)
        with self.lock:
            row = self.session(user, upload)
            missing = []
            if row["state"] == "OPEN":
                for block in json.loads(row["blocks"]):
                    stored = self.db.execute("SELECT * FROM blocks WHERE space=? AND digest=?", (row["space"], block["digest"])).fetchone()
                    if stored is None or not self.store.exists(stored["key"]):
                        missing.append(block["digest"])
            return {"upload": upload, "state": row["state"], "missing": sorted(set(missing)),
                    "result": json.loads(row["result"]) if row["result"] else None}

    def put(self, token, upload, digest, data):
        user = self.auth(token)
        with self.transaction():
            row = self.session(user, upload)
            if row["state"] != "OPEN":
                raise ValueError("upload is not open")
            match = next((b for b in json.loads(row["blocks"]) if b["digest"] == digest), None)
            if match is None or not isinstance(data, bytes) or len(data) != match["size"]:
                raise ValueError("undeclared/invalid block")
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("checksum mismatch")
            old = self.db.execute("SELECT * FROM blocks WHERE space=? AND digest=?", (row["space"], digest)).fetchone()
            if old is not None and self.store.exists(old["key"]):
                try:
                    self.store.get(row["space"], digest, len(data))
                    return False
                except CorruptBlock:
                    pass  # Verified incoming plaintext can repair the same digest.
            key = self.store.put(row["space"], digest, data)
            self.db.execute("INSERT INTO blocks VALUES(?,?,?,?,0,?) ON CONFLICT(space,digest) DO UPDATE SET key=excluded.key,size=excluded.size",
                            (row["space"], digest, len(data), key, self.clock()))
            return True

    def emit(self, kind, space, file=None, revision=None, path=None, users=None):
        users = users if users is not None else [r[0] for r in self.db.execute("SELECT user FROM members WHERE space=?", (space,))]
        for user in users:
            self.db.execute("INSERT INTO events(user,kind,space,file,revision,path) VALUES(?,?,?,?,?,?)", (user, kind, space, file, revision, path))

    def commit(self, token, upload):
        user = self.auth(token)
        with self.transaction():
            row = self.session(user, upload)
            if row["state"] == "DONE":
                return json.loads(row["result"])
            if row["state"] != "OPEN":
                raise ValueError("upload cancelled")
            manifest = json.loads(row["blocks"])
            digest = hashlib.sha256()
            for block in manifest:
                digest.update(self.store.get(row["space"], block["digest"], block["size"]))
                if self.db.execute("SELECT 1 FROM blocks WHERE space=? AND digest=?", (row["space"], block["digest"])).fetchone() is None:
                    raise ValueError("block metadata missing")
            if digest.hexdigest() != row["checksum"]:
                raise ValueError("file checksum mismatch")
            current = self.db.execute("SELECT * FROM files WHERE space=? AND path=?", (row["space"], row["path"])).fetchone()
            conflict = (current["head"] if current else None) != row["base"]
            path = row["path"]
            if conflict:
                parent = PurePosixPath(path)
                path = str(parent.with_name(parent.stem[:40] + ".conflict-" + upload[:12] + parent.suffix[:15]))
                current = None
                if self.db.execute("SELECT 1 FROM files WHERE space=? AND path=?", (row["space"], path)).fetchone():
                    raise Conflict("conflict path collision")
            file = current["id"] if current else uuid.uuid4().hex
            safe_path(path)
            for other in self.db.execute("SELECT f.id,f.path FROM files f JOIN revisions r ON f.head=r.id WHERE f.space=? AND r.deleted=0", (row["space"],)):
                if other["id"] != file:
                    a, b = other["path"].casefold(), path.casefold()
                    if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                        raise Conflict("file/directory or case-insensitive path collision")
            revision = uuid.uuid4().hex
            if current is None:
                self.db.execute("INSERT INTO files VALUES(?,?,?,NULL)", (file, row["space"], path))
            self.db.execute("INSERT INTO revisions(id,file,size,checksum,blocks,deleted,created) VALUES(?,?,?,?,?,0,?)", (revision, file, row["size"], row["checksum"], row["blocks"], self.clock()))
            for block in manifest:
                self.db.execute("UPDATE blocks SET refs=refs+1 WHERE space=? AND digest=?", (row["space"], block["digest"]))
            self.db.execute("UPDATE files SET head=? WHERE id=?", (revision, file))
            result = {"file": file, "revision": revision, "path": path, "conflict": conflict}
            self.db.execute("UPDATE uploads SET state='DONE',result=? WHERE id=?", (json.dumps(result), upload))
            self.emit("upsert", row["space"], file, revision, path)
            return result

    def metadata(self, token, file, revision=None):
        user = self.auth(token)
        with self.read():
            row = self.db.execute("SELECT * FROM files WHERE id=?", (file,)).fetchone()
            if row is None:
                raise ValueError("unknown file")
            self.access(user, row["space"])
            version = self.db.execute("SELECT * FROM revisions WHERE id=? AND file=?", (revision or row["head"], file)).fetchone()
            if version is None:
                raise ValueError("unknown revision")
            return {"file": file, "space": row["space"], "path": row["path"], "revision": version["id"],
                    "version": version["version"], "size": version["size"], "checksum": version["checksum"], "deleted": bool(version["deleted"]),
                    "blocks": json.loads(version["blocks"])}

    def get_block(self, token, file, revision, index):
        with self.read():
            meta = self.metadata(token, file, revision)
            if type(index) is not int or not 0 <= index < len(meta["blocks"]):
                raise ValueError("invalid block index")
            block = meta["blocks"][index]
            return self.store.get(meta["space"], block["digest"], block["size"])

    def list_files(self, token, space):
        user = self.auth(token)
        with self.read():
            self.access(user, space)
            return [self.metadata(token, r[0]) for r in self.db.execute("SELECT id FROM files WHERE space=? ORDER BY path", (space,)).fetchall()]

    def revisions(self, token, file):
        with self.read():
            self.metadata(token, file)
            return [dict(row) for row in self.db.execute("SELECT id,size,checksum,deleted,created FROM revisions WHERE file=? ORDER BY rowid DESC", (file,))]

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
            self.db.execute("INSERT INTO revisions(id,file,size,checksum,blocks,deleted,created) VALUES(?,?,0,?,'[]',1,?)", (revision, file, hashlib.sha256(b"").hexdigest(), self.clock()))
            self.db.execute("UPDATE files SET head=? WHERE id=?", (revision, file))
            self.emit("delete", meta["space"], file, revision, meta["path"])
            return revision

    def restore(self, token, file, revision, base, request):
        meta = self.metadata(token, file, revision)
        if meta["deleted"]:
            raise ValueError("cannot restore tombstone content")
        session = self.begin(token, meta["space"], meta["path"], meta["size"], meta["checksum"], meta["blocks"], base, request)
        return self.commit(token, session["upload"])

    def share(self, token, space, recipient, role):
        user = self.auth(token)
        if role not in ("read", "write", "revoke"):
            raise ValueError("invalid role")
        with self.transaction():
            row = self.db.execute("SELECT owner FROM spaces WHERE id=?", (space,)).fetchone()
            if row is None or row[0] != user or recipient == user:
                raise PermissionError("only owner may manage sharing")
            if self.db.execute("SELECT 1 FROM users WHERE name=?", (recipient,)).fetchone() is None:
                raise ValueError("unknown recipient")
            if role == "revoke":
                self.db.execute("DELETE FROM members WHERE space=? AND user=?", (space, recipient))
                self.emit("revoke", space, users=[recipient])
            else:
                self.db.execute("INSERT INTO members VALUES(?,?,?) ON CONFLICT(space,user) DO UPDATE SET role=excluded.role", (space, recipient, role))
                for file in self.db.execute("SELECT id,head,path FROM files WHERE space=?", (space,)).fetchall():
                    self.emit("upsert", space, file[0], file[1], file[2], [recipient])
                self.emit("share", space, users=[recipient])

    def changes(self, token, device, cursor, wait=0, limit=100):
        user = self.auth(token)
        if type(cursor) is not int or cursor < 0 or not 0 <= wait <= 25 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid cursor/wait/limit")
        deadline = time.monotonic() + wait
        while True:
            with self.transaction():
                row = self.db.execute("SELECT * FROM devices WHERE id=? AND user=?", (device, user)).fetchone()
                high = self.db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
                if row is None:
                    raise PermissionError("device registration required")
                if cursor > high:
                    raise ValueError("cursor beyond log")
                events = [dict(r) for r in self.db.execute("SELECT * FROM events WHERE user=? AND seq>? ORDER BY seq LIMIT ?", (user, cursor, limit))]
                delivered = events[-1]["seq"] if events else high
                self.db.execute("UPDATE devices SET delivered=MAX(delivered,?) WHERE id=?", (delivered, device))
            if events or time.monotonic() >= deadline:
                return {"events": events, "cursor": delivered}
            time.sleep(min(.1, max(0, deadline - time.monotonic())))

    def ack(self, token, device, cursor):
        user = self.auth(token)
        if type(cursor) is not int or cursor < 0:
            raise ValueError("invalid cursor")
        with self.transaction():
            row = self.db.execute("SELECT * FROM devices WHERE id=? AND user=?", (device, user)).fetchone()
            if row is None:
                raise PermissionError("unknown device")
            if cursor > row["delivered"]:
                raise ValueError("cursor not delivered")
            self.db.execute("UPDATE devices SET cursor=MAX(cursor,?) WHERE id=?", (cursor, device))

    def cancel(self, token, upload):
        user = self.auth(token)
        with self.transaction():
            row = self.session(user, upload)
            if row["state"] == "OPEN":
                self.db.execute("UPDATE uploads SET state='CANCELLED' WHERE id=?", (upload,))

    def prune(self, token, file, keep=5):
        user = self.auth(token)
        if type(keep) is not int or keep < 1:
            raise ValueError("keep at least one revision")
        with self.transaction():
            meta = self.metadata(token, file)
            role = self.db.execute("SELECT role FROM members WHERE space=? AND user=?", (meta["space"], user)).fetchone()[0]
            if role != "owner":
                raise PermissionError("owner required")
            rows = self.db.execute("SELECT id,blocks FROM revisions WHERE file=? ORDER BY rowid DESC", (file,)).fetchall()
            remove = [r for r in rows[keep:] if r["id"] != meta["revision"]]
            for row in remove:
                for block in json.loads(row["blocks"]):
                    self.db.execute("UPDATE blocks SET refs=refs-1 WHERE space=? AND digest=?", (meta["space"], block["digest"]))
                self.db.execute("DELETE FROM revisions WHERE id=?", (row["id"],))
            return len(remove)

    def cleanup(self, grace=3600, upload_age=86400):
        if grace < 300 or upload_age < 3600:
            raise ValueError("cleanup grace too short")
        with self.transaction():
            self.db.execute("UPDATE uploads SET state='CANCELLED' WHERE state='OPEN' AND created<?", (self.clock() - upload_age,))
            pinned = {(r["space"], b["digest"]) for r in self.db.execute("SELECT space,blocks FROM uploads WHERE state='OPEN'") for b in json.loads(r["blocks"])}
            removed = 0
            for row in self.db.execute("SELECT * FROM blocks WHERE refs=0 AND created<?", (self.clock() - grace,)).fetchall():
                if (row["space"], row["digest"]) not in pinned:
                    self.store.path(row["key"]).unlink(missing_ok=True)
                    (self.store.cold / row["key"]).unlink(missing_ok=True)
                    self.db.execute("DELETE FROM blocks WHERE space=? AND digest=?", (row["space"], row["digest"]))
                    removed += 1
            keys = {r[0] for r in self.db.execute("SELECT key FROM blocks")}
            for directory in (self.store.root, self.store.cold):
                for path in directory.iterdir():
                    if path.is_file() and path.name not in keys and time.time() - path.stat().st_mtime > grace:
                        path.resolve().relative_to(self.store.root)
                        path.unlink()
                        removed += 1
            return {"removed_objects": removed}
