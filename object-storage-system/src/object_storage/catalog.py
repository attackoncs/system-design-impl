"""Bucket ownership and durable multipart-upload control records."""

import json
import re
import threading
import time
import uuid

from .common import (
    Database,
    Error,
    checksum,
    digest,
    dumps,
    identifier,
    integer,
    object_key,
    sha,
)
from .metadata_records import MAX_OBJECT_BYTES, manifest_copy, publication, timestamp

MAX_BUCKETS = 1000


def bucket_name(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", value
    ):
        raise ValueError("bucket requires 3..63 lowercase letters/digits/hyphens")
    return value


class Catalog(Database):
    def __init__(self, path, clock=time.time):
        super().__init__(path)
        self.clock, self.lock = clock, threading.RLock()
        self.initialize("""
            CREATE TABLE IF NOT EXISTS buckets(name TEXT PRIMARY KEY,id TEXT UNIQUE,user TEXT);
            CREATE INDEX IF NOT EXISTS buckets_by_owner ON buckets(user,name);
            CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY,bucket TEXT,user TEXT,key TEXT,request TEXT,
                sha TEXT,size INTEGER,state TEXT,expires REAL,result TEXT,manifest TEXT,UNIQUE(bucket,key,request));
            CREATE TABLE IF NOT EXISTS parts(upload TEXT,number INTEGER,sha TEXT,size INTEGER,manifest TEXT,
                PRIMARY KEY(upload,number));
            CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY,user TEXT,bucket TEXT,prefix TEXT,
                versions INTEGER,page_size INTEGER,expires REAL,rows TEXT,
                delimiter TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS snapshot_expiry ON snapshots(expires);
            CREATE TABLE IF NOT EXISTS publications(
                bucket TEXT,key TEXT,request TEXT,hash TEXT,payload TEXT,
                done INTEGER NOT NULL DEFAULT 0 CHECK(done IN (0,1)),created REAL,
                PRIMARY KEY(bucket,key,request));
            CREATE INDEX IF NOT EXISTS pending_publications ON publications(done,created);
        """)
        # Old recursive-listing tokens retain their meaning after upgrading.
        with self.transaction(True) as db:
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(snapshots)")
            }
            if "delimiter" not in columns:
                db.execute(
                    "ALTER TABLE snapshots ADD COLUMN delimiter TEXT NOT NULL DEFAULT ''"
                )

    @staticmethod
    def _owned_bucket(db, bucket, user):
        row = db.execute(
            "SELECT * FROM buckets WHERE id=? AND user=?", (bucket, user)
        ).fetchone()
        if not row:
            raise Error(404, "bucket missing")
        return dict(row)

    def create(self, name, user):
        bucket_name(name)
        identifier(user)
        with self.transaction(True) as db:
            old = db.execute("SELECT * FROM buckets WHERE name=?", (name,)).fetchone()
            if old:
                if old["user"] != user:
                    raise Error(409, "bucket name unavailable")
                return dict(old)
            if (
                db.execute(
                    "SELECT count(*) FROM buckets WHERE user=?", (user,)
                ).fetchone()[0]
                >= MAX_BUCKETS
            ):
                raise Error(422, "owner bucket budget exceeded")
            value = {"name": name, "id": uuid.uuid4().hex, "user": user}
            db.execute("INSERT INTO buckets VALUES(?,?,?)", tuple(value.values()))
            return value

    def listing(self, user):
        identifier(user)
        with self.transaction() as db:
            rows = db.execute(
                "SELECT * FROM buckets WHERE user=? ORDER BY name LIMIT ?",
                (user, MAX_BUCKETS + 1),
            ).fetchall()
            if len(rows) > MAX_BUCKETS:
                raise Error(422, "owner bucket budget exceeded")
            return {"buckets": [dict(row) for row in rows]}

    def bucket(self, name, user):
        bucket_name(name)
        identifier(user)
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM buckets WHERE name=? AND user=?", (name, user)
            ).fetchone()
            if not row:
                raise Error(404, "bucket missing")
            return dict(row)

    @staticmethod
    def _upload(db, upload, user):
        row = db.execute(
            "SELECT * FROM uploads WHERE id=? AND user=?", (upload, user)
        ).fetchone()
        if not row:
            raise Error(404, "upload missing")
        value = dict(row)
        value["parts"] = [
            {**dict(part), "manifest": json.loads(part["manifest"])}
            for part in db.execute(
                "SELECT * FROM parts WHERE upload=? ORDER BY number", (upload,)
            )
        ]
        return value

    def start(self, bucket, user, body):
        identifier(bucket)
        identifier(user)
        if not isinstance(body, dict):
            raise ValueError("upload requires key,request_key,sha256,size")
        body = dict(body)
        if set(body) != {"key", "request_key", "sha256", "size"}:
            raise ValueError("upload requires key,request_key,sha256,size")
        key = object_key(body["key"])
        request = identifier(body["request_key"])
        expected = checksum(body["sha256"])
        size = integer(body["size"], "size", 0, MAX_OBJECT_BYTES)
        if size == 0 and expected != sha(b""):
            raise ValueError("empty object checksum mismatch")
        with self.transaction(True) as db:
            self._owned_bucket(db, bucket, user)
            old = db.execute(
                "SELECT * FROM uploads WHERE bucket=? AND key=? AND request=?",
                (bucket, key, request),
            ).fetchone()
            if old:
                if (old["sha"], old["size"], old["user"]) != (expected, size, user):
                    raise Error(409, "upload idempotency content conflict")
                return self._upload(db, old["id"], user)
            upload = uuid.uuid4().hex
            db.execute(
                "INSERT INTO uploads VALUES(?,?,?,?,?,?,?,'active',?,NULL,NULL)",
                (
                    upload,
                    bucket,
                    user,
                    key,
                    request,
                    expected,
                    size,
                    timestamp(self.clock) + 3600,
                ),
            )
            return self._upload(db, upload, user)

    def upload(self, upload, user):
        identifier(upload)
        identifier(user)
        with self.transaction() as db:
            return self._upload(db, upload, user)

    def add_part(self, upload, user, number, data, manifest):
        identifier(upload)
        identifier(user)
        integer(number, "part number", 1, 64)
        if not isinstance(data, bytes) or not 1 <= len(data) <= 1024 * 1024:
            raise ValueError("part requires 1..1048576 bytes")
        manifest = manifest_copy(manifest)
        offset = 0
        for chunk in manifest:
            end = offset + chunk["size"]
            if sha(data[offset:end]) != chunk["hash"]:
                raise Error(400, "part manifest checksum mismatch")
            offset = end
        if offset != len(data):
            raise Error(400, "part manifest size mismatch")
        checksum_value = sha(data)
        with self.transaction(True) as db:
            state = self._upload(db, upload, user)
            # The check must happen after acquiring the writer lock: abort,
            # completion or expiry may have won while an upload waited.
            if state["state"] != "active" or state["expires"] <= timestamp(self.clock):
                raise Error(409, "upload is not active")
            old = db.execute(
                "SELECT * FROM parts WHERE upload=? AND number=?", (upload, number)
            ).fetchone()
            if old:
                if (old["sha"], old["size"]) != (checksum_value, len(data)):
                    raise Error(409, "part number content is immutable")
                return {"number": number, "sha256": old["sha"], "duplicate": True}
            if sum(part["size"] for part in state["parts"]) + len(data) > state["size"]:
                raise Error(409, "parts exceed declared object size")
            db.execute(
                "INSERT INTO parts VALUES(?,?,?,?,?)",
                (upload, number, checksum_value, len(data), dumps(manifest)),
            )
        return {"number": number, "sha256": checksum_value, "duplicate": False}

    def prepare_publication(
        self, bucket, key, request, manifest, size, sha, marker=False
    ):
        """Persist an immutable recovery intent before sending a metadata RPC.

        A timed-out RPC may still commit remotely. Such an intent stays pending
        until a retry establishes the same durable metadata receipt, preventing
        collection from reasoning from an incomplete publication set.
        """
        frozen = publication(bucket, key, request, manifest, size, sha, marker)
        payload = {
            "bucket": bucket,
            "key": key,
            "request": request,
            "manifest": frozen,
            "size": size,
            "sha": sha,
            "marker": marker,
        }
        fingerprint = digest(payload)
        raw = dumps(payload)
        with self.transaction(True) as db:
            prior = db.execute(
                "SELECT hash,done FROM publications WHERE bucket=? AND key=? AND request=?",
                (bucket, key, request),
            ).fetchone()
            if prior:
                if prior["hash"] != fingerprint:
                    raise Error(409, "publication idempotency content conflict")
                return {"done": bool(prior["done"]), "duplicate": True}
            db.execute(
                """INSERT INTO publications(bucket,key,request,hash,payload,done,created)
                          VALUES(?,?,?,?,?,0,?)""",
                (bucket, key, request, fingerprint, raw, timestamp(self.clock)),
            )
        return {"done": False, "duplicate": False}

    def finish_publication(self, bucket, key, request):
        identifier(bucket)
        object_key(key)
        identifier(request)
        with self.transaction(True) as db:
            row = db.execute(
                "SELECT done FROM publications WHERE bucket=? AND key=? AND request=?",
                (bucket, key, request),
            ).fetchone()
            if row is None:
                raise Error(404, "publication intent missing")
            db.execute(
                "UPDATE publications SET done=1 WHERE bucket=? AND key=? AND request=?",
                (bucket, key, request),
            )
        return {"done": True}

    def pending_publications(self, limit=128):
        """Return the complete bounded recovery set, or block maintenance."""
        integer(limit, "publication limit", 1, 128)
        with self.transaction() as db:
            rows = db.execute(
                """
                SELECT bucket,key,request,hash,payload FROM publications WHERE done=0
                ORDER BY created,bucket,key,request LIMIT ?
            """,
                (limit + 1,),
            ).fetchall()
        if len(rows) > limit:
            raise Error(422, "pending publication budget exceeded; collection blocked")
        result = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
                if not isinstance(payload, dict) or set(payload) != {
                    "bucket",
                    "key",
                    "request",
                    "manifest",
                    "size",
                    "sha",
                    "marker",
                }:
                    raise ValueError("intent fields")
                if (payload["bucket"], payload["key"], payload["request"]) != (
                    row["bucket"],
                    row["key"],
                    row["request"],
                ):
                    raise ValueError("intent identity")
                payload["manifest"] = publication(
                    payload["bucket"],
                    payload["key"],
                    payload["request"],
                    payload["manifest"],
                    payload["size"],
                    payload["sha"],
                    payload["marker"],
                )
                if digest(payload) != row["hash"]:
                    raise ValueError("intent checksum")
            except (ValueError, TypeError, KeyError):
                raise Error(
                    409, "invalid publication intent; collection blocked"
                ) from None
            result.append(payload)
        return result
