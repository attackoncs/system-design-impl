import json
import time
import uuid

from .common import (
    Database,
    Error,
    digest,
    dumps,
    identifier,
    integer,
    shard_for,
)
from .metadata_records import (
    SCHEMA,
    manifest_copy,
    object_identity,
    publication,
    timestamp,
)

MAX_REFERENCE_ROWS = 50_000
MAX_REFERENCE_CHUNKS = 200_000
MAX_REFERENCE_HASHES = 50_000


class Metadata(Database):
    def __init__(self, path, shard=0, shards=1, clock=time.time):
        super().__init__(path)
        self.shard, self.shards, self.clock = shard, shards, clock
        integer(shards, "shards", 1, 32)
        integer(shard, "shard", 0, shards - 1)
        self.initialize(SCHEMA)
        with self.transaction(True) as db:
            db.execute("INSERT OR IGNORE INTO topology VALUES(1,?,?)", (shard, shards))
            row = db.execute("SELECT * FROM topology").fetchone()
            if (row["shard"], row["shards"]) != (shard, shards):
                raise Error(409, "metadata shard topology changed")

    def owner(self, bucket, key):
        object_identity(bucket, key)
        if shard_for(bucket, key, self.shards) != self.shard:
            raise Error(409, "wrong metadata shard")

    def publish(self, bucket, key, request, manifest, size, sha, marker=False):
        self.owner(bucket, key)
        manifest = publication(bucket, key, request, manifest, size, sha, marker)
        fingerprint = digest([manifest, size, sha, marker])
        with self.transaction(True) as db:
            prior = db.execute(
                "SELECT * FROM receipts WHERE bucket=? AND key=? AND request=?",
                (bucket, key, request),
            ).fetchone()
            if prior:
                if prior["hash"] != fingerprint:
                    raise Error(409, "idempotency key content conflict")
                return {"version": prior["version"], "duplicate": True}
            version = uuid.uuid4().hex
            db.execute(
                "INSERT INTO versions(id,bucket,key,marker,manifest,size,sha,created) VALUES(?,?,?,?,?,?,?,?)",
                (
                    version,
                    bucket,
                    key,
                    marker,
                    dumps(manifest),
                    size,
                    sha,
                    timestamp(self.clock),
                ),
            )
            db.execute(
                "INSERT INTO receipts VALUES(?,?,?,?,?)",
                (bucket, key, request, fingerprint, version),
            )
            return {"version": version, "duplicate": False}

    def get(self, bucket, key, version=None):
        self.owner(bucket, key)
        if version is not None:
            identifier(version)
        with self.transaction() as db:
            sql = "SELECT * FROM versions WHERE bucket=? AND key=? AND removed IS NULL"
            args = [bucket, key]
            if version is not None:
                sql += " AND id=?"
                args.append(version)
            row = db.execute(sql + " ORDER BY seq DESC LIMIT 1", args).fetchone()
            if not row:
                raise Error(404, "object version missing")
            result = dict(row)
            result["manifest"] = json.loads(result["manifest"])
            return result

    def remove(self, bucket, key, version):
        self.owner(bucket, key)
        identifier(version)
        with self.transaction(True) as db:
            row = db.execute(
                "SELECT id FROM versions WHERE bucket=? AND key=? AND id=?",
                (bucket, key, version),
            ).fetchone()
            if not row:
                raise Error(404, "object version missing")
            db.execute(
                "UPDATE versions SET removed=coalesce(removed,?) WHERE id=?",
                (timestamp(self.clock), version),
            )
        return {"removed": version}

    def listing(self, bucket, versions=False):
        identifier(bucket)
        if type(versions) is not bool:
            raise ValueError("versions must be boolean")
        with self.transaction() as db:
            if versions:
                rows = db.execute(
                    "SELECT * FROM versions WHERE bucket=? AND removed IS NULL ORDER BY key,seq DESC LIMIT 10001",
                    (bucket,),
                ).fetchall()
            else:
                rows = db.execute(
                    """SELECT v.* FROM versions v JOIN
                    (SELECT key,max(seq) seq FROM versions WHERE bucket=? AND removed IS NULL GROUP BY key) x
                    ON v.key=x.key AND v.seq=x.seq WHERE v.marker=0 ORDER BY v.key LIMIT 10001""",
                    (bucket,),
                ).fetchall()
            if len(rows) > 10000:
                raise Error(422, "listing exceeds reference budget")
            return {
                "rows": [
                    {
                        k: r[k]
                        for k in (
                            "key",
                            "id",
                            "seq",
                            "marker",
                            "size",
                            "sha",
                            "created",
                        )
                    }
                    for r in rows
                ]
            }

    def references(self, grace=60):
        integer(grace, "grace", 0, 86400)
        with self.transaction() as db:
            rows = db.execute(
                "SELECT manifest FROM versions WHERE removed IS NULL OR removed>?",
                (timestamp(self.clock) - grace,),
            )
            hashes, chunks = set(), 0
            for index, row in enumerate(rows):
                if index >= MAX_REFERENCE_ROWS:
                    raise Error(422, "maintenance version scan budget exceeded")
                try:
                    manifest = manifest_copy(json.loads(row[0]))
                except (ValueError, TypeError, KeyError):
                    raise Error(
                        409, "invalid stored manifest; collection blocked"
                    ) from None
                chunks += len(manifest)
                if chunks > MAX_REFERENCE_CHUNKS:
                    raise Error(422, "maintenance chunk scan budget exceeded")
                hashes.update(chunk["hash"] for chunk in manifest)
                if len(hashes) > MAX_REFERENCE_HASHES:
                    raise Error(422, "maintenance reference budget exceeded")
            return {"hashes": sorted(hashes)}

    def prune(self, bucket, keep):
        identifier(bucket)
        integer(keep, "keep", 1, 1000)
        with self.transaction(True) as db:
            now = timestamp(self.clock)
            count = db.execute(
                """
                UPDATE versions SET removed=? WHERE seq IN (
                    SELECT seq FROM (
                        SELECT seq,row_number() OVER (
                            PARTITION BY key ORDER BY seq DESC) AS ordinal
                        FROM versions WHERE bucket=? AND removed IS NULL
                    ) WHERE ordinal>?
                )
            """,
                (now, bucket, keep),
            ).rowcount
        return {"pruned": count}
