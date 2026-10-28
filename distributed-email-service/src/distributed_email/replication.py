"""Validated mailbox replication, including an atomic local change history."""

import json

from .common import Error, digest, dumps, identifier, integer, terms
from .mail_records import FIELDS, descriptor, object_key


ROW_FIELDS = ("local_seq", "id", "user", "descriptor", "read", "version", "deleted")


def validated_batch(batch, shard, shards):
    if not isinstance(batch, dict) or set(batch) != {
        "lineage",
        "shard",
        "shards",
        "head",
        "changes",
    }:
        raise ValueError("invalid replication batch fields")
    batch = json.loads(dumps(batch))
    identifier(batch["lineage"])
    integer(batch["shard"], "shard", 0, 31)
    integer(batch["shards"], "shards", 1, 32)
    integer(batch["head"], "head")
    if (batch["shard"], batch["shards"]) != (shard, shards):
        raise Error(409, "replica topology mismatch")
    changes = batch["changes"]
    if not isinstance(changes, list) or len(changes) > 100:
        raise ValueError("replication page exceeds 100 changes")
    if len(dumps(changes).encode()) > 2 * 1024 * 1024 + 1024:
        raise ValueError("replication page exceeds 2 MiB")
    previous = None
    for change in changes:
        if not isinstance(change, dict) or set(change) != {"seq", "payload", "hash"}:
            raise ValueError("invalid replication change fields")
        seq = integer(change["seq"], "change sequence", 1)
        if seq > batch["head"] or (previous is not None and seq != previous + 1):
            raise Error(409, "replication page sequence/head mismatch")
        previous = seq
        object_key(change["hash"])
        payload = change["payload"]
        if digest(payload) != change["hash"]:
            raise Error(409, "replication checksum mismatch")
        if not isinstance(payload, dict) or set(payload) != {"record", "terms"}:
            raise ValueError("invalid replication payload fields")
        row = payload["record"]
        if not isinstance(row, dict) or set(row) != set(ROW_FIELDS):
            raise ValueError("invalid replicated record fields")
        integer(row["local_seq"], "local sequence", 1)
        integer(row["version"], "version", 1)
        integer(row["read"], "read", 0, 1)
        integer(row["deleted"], "deleted", 0, 1)
        identifier(row["user"])
        object_key(row["id"])
        if not isinstance(row["descriptor"], str):
            raise ValueError("replicated descriptor must be JSON text")
        desc = descriptor(json.loads(row["descriptor"]))
        postings = payload["terms"]
        if not isinstance(postings, list) or len(postings) > 4096:
            raise ValueError("replicated index exceeds 4096 field terms")
        pairs = []
        for pair in postings:
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError("invalid replicated index entry")
            field, term = pair
            if (
                field not in FIELDS
                or not isinstance(term, str)
                or len(term) > 262144
                or terms(term) != [term]
            ):
                raise ValueError("invalid replicated index field/term")
            pairs.append((field, term))
        if len(set(pairs)) != len(pairs):
            raise ValueError("duplicate replicated index entry")
        if row["deleted"]:
            if pairs:
                raise ValueError("deleted message retains search index")
        else:
            for field in ("subject", "sender"):
                if {t for f, t in pairs if f == field} != set(terms(desc[field])):
                    raise ValueError("metadata and replicated index disagree")
    return batch


class Replication:
    """Mixin for the storage facade; all mutations use its SQLite transaction."""

    def changes(self, after):
        integer(after, "after")
        with self.transaction() as db:
            meta = dict(db.execute("SELECT * FROM metadata").fetchone())
            if after > meta["position"]:
                raise Error(409, "replica cursor ahead of source")
            changes, size = [], 0
            for row in db.execute(
                "SELECT * FROM changes WHERE seq>? ORDER BY seq LIMIT 100", (after,)
            ):
                entry = {
                    "seq": row["seq"],
                    "payload": json.loads(row["payload"]),
                    "hash": row["hash"],
                }
                encoded = len(dumps(entry).encode())
                if changes and size + encoded > 2 * 1024 * 1024:
                    break
                changes.append(entry)
                size += encoded
            return {
                "lineage": meta["lineage"],
                "shard": self.shard,
                "shards": self.shards,
                "head": meta["position"],
                "changes": changes,
            }

    def apply(self, batch):
        if not self.replica:
            raise Error(409, "replica topology mismatch")
        batch = validated_batch(batch, self.shard, self.shards)
        with self.transaction(True) as db:
            meta = dict(db.execute("SELECT * FROM metadata").fetchone())
            if meta["lineage"] and meta["lineage"] != batch["lineage"]:
                raise Error(409, "source lineage changed; explicit rebuild required")
            position = meta["position"]
            for change in batch["changes"]:
                payload = dumps(change["payload"])
                if change["seq"] <= position:
                    old = db.execute(
                        "SELECT payload,hash FROM changes WHERE seq=?", (change["seq"],)
                    ).fetchone()
                    if old and (old["payload"], old["hash"]) != (
                        payload,
                        change["hash"],
                    ):
                        raise Error(409, "replication replay content changed")
                    continue
                if change["seq"] != position + 1:
                    raise Error(409, "replication gap")
                row = change["payload"]["record"]
                self.owner(row["user"])
                old = db.execute(
                    "SELECT * FROM mails WHERE id=?", (row["id"],)
                ).fetchone()
                if old:
                    if (
                        (old["local_seq"], old["user"])
                        != (row["local_seq"], row["user"])
                        or row["version"] != old["version"] + 1
                        or (old["deleted"] and not row["deleted"])
                    ):
                        raise Error(
                            409, "replicated immutable identity/version changed"
                        )
                    old_descriptor = json.loads(old["descriptor"])
                    new_descriptor = json.loads(row["descriptor"])
                    if {k: v for k, v in old_descriptor.items() if k != "delivery"} != {
                        k: v for k, v in new_descriptor.items() if k != "delivery"
                    }:
                        raise Error(409, "replicated immutable content changed")
                    if old_descriptor.get("delivery") != new_descriptor.get(
                        "delivery"
                    ) and (
                        old_descriptor["folder"] != "sent"
                        or new_descriptor.get("delivery") != "smtp-accepted"
                    ):
                        raise Error(409, "invalid replicated delivery transition")
                    if not row["deleted"]:
                        old_terms = {
                            tuple(r)
                            for r in db.execute(
                                "SELECT field,term FROM postings WHERE mail=?",
                                (row["id"],),
                            )
                        }
                        if old_terms != {
                            tuple(pair) for pair in change["payload"]["terms"]
                        }:
                            raise Error(
                                409, "replicated immutable search terms changed"
                            )
                elif row["version"] != 1 or row["read"] or row["deleted"]:
                    raise Error(409, "replication must begin with message creation")
                collision = db.execute(
                    "SELECT id FROM mails WHERE local_seq=?", (row["local_seq"],)
                ).fetchone()
                if collision and collision["id"] != row["id"]:
                    raise Error(409, "replicated creation sequence collision")
                db.execute(
                    "INSERT INTO mails VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET descriptor=excluded.descriptor,read=excluded.read,version=excluded.version,deleted=excluded.deleted",
                    tuple(row[k] for k in ROW_FIELDS),
                )
                db.execute("DELETE FROM postings WHERE mail=?", (row["id"],))
                db.executemany(
                    "INSERT INTO postings VALUES(?,?,?)",
                    [(row["id"], f, t) for f, t in change["payload"]["terms"]],
                )
                db.execute(
                    "INSERT INTO changes(seq,payload,hash) VALUES(?,?,?)",
                    (change["seq"], payload, change["hash"]),
                )
                position = change["seq"]
            db.execute(
                "UPDATE metadata SET lineage=?,position=?", (batch["lineage"], position)
            )
        return position
