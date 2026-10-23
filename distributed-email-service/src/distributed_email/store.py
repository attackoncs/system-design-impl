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
    terms,
)


class MailStore(Database):
    def __init__(self, path, shard=0, shards=1, replica=False, clock=time.time):
        super().__init__(path)
        self.shard, self.shards, self.replica, self.clock = (
            shard,
            shards,
            replica,
            clock,
        )
        integer(shards, "shards", 1, 32)
        integer(shard, "shard", 0, shards - 1)
        self.initialize("""
        CREATE TABLE IF NOT EXISTS metadata(id INTEGER PRIMARY KEY CHECK(id=1),shard INTEGER,shards INTEGER,
            replica INTEGER,lineage TEXT,position INTEGER);
        CREATE TABLE IF NOT EXISTS mails(local_seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,user TEXT,
            descriptor TEXT,read INTEGER,version INTEGER,deleted INTEGER);
        CREATE INDEX IF NOT EXISTS mailbox ON mails(user,local_seq);
        CREATE TABLE IF NOT EXISTS postings(mail TEXT,field TEXT,term TEXT,PRIMARY KEY(mail,field,term));
        CREATE INDEX IF NOT EXISTS inverted ON postings(field,term,mail);
        CREATE TABLE IF NOT EXISTS changes(seq INTEGER PRIMARY KEY AUTOINCREMENT,payload TEXT,hash TEXT);
        CREATE TABLE IF NOT EXISTS receipts(user TEXT,key TEXT,hash TEXT,mail TEXT,PRIMARY KEY(user,key));
        CREATE TABLE IF NOT EXISTS deliveries(user TEXT,id TEXT,hash TEXT,mail TEXT,PRIMARY KEY(user,id));
        CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY,user TEXT,descriptor TEXT);
        CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,user TEXT,mail TEXT,raw TEXT,sender TEXT,
            recipients TEXT,status TEXT,attempts INTEGER,due REAL,lease REAL,token TEXT,error TEXT);
        """)
        with self.transaction(True) as db:
            db.execute(
                "INSERT OR IGNORE INTO metadata VALUES(1,?,?,?,?,0)",
                (shard, shards, int(replica), "" if replica else uuid.uuid4().hex),
            )
            meta = db.execute("SELECT * FROM metadata").fetchone()
            if (meta["shard"], meta["shards"], meta["replica"]) != (
                shard,
                shards,
                int(replica),
            ):
                raise Error(409, "metadata topology/role changed")

    def owner(self, user):
        if shard_for(user, self.shards) != self.shard:
            raise Error(409, "wrong user shard")

    def writable(self):
        if self.replica:
            raise Error(409, "replicas are read-only; no automatic promotion")

    def status(self):
        with self.transaction() as db:
            meta = dict(db.execute("SELECT * FROM metadata").fetchone())
            meta.update(
                mails=db.execute(
                    "SELECT count(*) FROM mails WHERE deleted=0"
                ).fetchone()[0],
                pending=db.execute(
                    "SELECT count(*) FROM outbox WHERE status!='done'"
                ).fetchone()[0],
                errors=[
                    dict(r)
                    for r in db.execute(
                        "SELECT id,attempts,error FROM outbox WHERE status!='done' AND error!='' LIMIT 20"
                    )
                ],
            )
            return meta

    def get(self, user, mail, db=None):
        self.owner(user)
        if db is None:
            with self.transaction() as connection:
                return self.get(user, mail, connection)
        row = db.execute(
            "SELECT * FROM mails WHERE user=? AND id=? AND deleted=0", (user, mail)
        ).fetchone()
        if not row:
            raise Error(404, "message missing")
        result = dict(row)
        result["descriptor"] = json.loads(result["descriptor"])
        meta = db.execute("SELECT lineage,position FROM metadata").fetchone()
        result.update(lineage=meta["lineage"], position=meta["position"])
        return result

    def record(self, db, mail):
        row = dict(db.execute("SELECT * FROM mails WHERE id=?", (mail,)).fetchone())
        tokens = [
            list(r)
            for r in db.execute(
                "SELECT field,term FROM postings WHERE mail=? ORDER BY field,term",
                (mail,),
            )
        ]
        payload = dumps({"record": row, "terms": tokens})
        seq = db.execute(
            "INSERT INTO changes(payload,hash) VALUES(?,?)",
            (payload, digest(json.loads(payload))),
        ).lastrowid
        db.execute("UPDATE metadata SET position=?", (seq,))

    def insert_mail(self, db, user, mail, descriptor, body):
        db.execute(
            "INSERT INTO mails(id,user,descriptor,read,version,deleted) VALUES(?,?,?,0,1,0)",
            (mail, user, dumps(descriptor)),
        )
        postings = [
            (mail, field, term)
            for field, value in (
                ("subject", descriptor["subject"]),
                ("sender", descriptor["sender"]),
                ("body", body),
            )
            for term in terms(value)
        ]
        if len(postings) > 4096:
            raise Error(422, "message exceeds 4096 indexed field terms")
        db.executemany("INSERT INTO postings VALUES(?,?,?)", postings)
        self.record(db, mail)

    def upload(self, user, descriptor):
        self.owner(user)
        self.writable()
        upload = uuid.uuid4().hex
        with self.transaction(True) as db:
            db.execute(
                "INSERT INTO uploads VALUES(?,?,?)", (upload, user, dumps(descriptor))
            )
        return {"upload_id": upload, **descriptor}

    def attachments(self, user, ids):
        self.owner(user)
        if not isinstance(ids, list) or len(ids) > 3 or len(set(ids)) != len(ids):
            raise ValueError("at most three distinct attachment uploads")
        result = []
        with self.transaction() as db:
            for upload in ids:
                row = db.execute(
                    "SELECT descriptor FROM uploads WHERE id=? AND user=?",
                    (identifier(upload), user),
                ).fetchone()
                if not row:
                    raise Error(404, "attachment upload missing")
                result.append(json.loads(row[0]))
        return result

    def prior(self, user, key, fingerprint):
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM receipts WHERE user=? AND key=?", (user, key)
            ).fetchone()
            if row:
                if row["hash"] != fingerprint:
                    raise Error(409, "send idempotency payload conflict")
                return {"id": row["mail"], "duplicate": True}
        return None

    def enqueue(self, user, key, fingerprint, descriptor, body, raw):
        self.owner(user)
        self.writable()
        identifier(key)
        mail = digest([user, key, "sent"])
        with self.transaction(True) as db:
            old = db.execute(
                "SELECT * FROM receipts WHERE user=? AND key=?", (user, key)
            ).fetchone()
            if old:
                if old["hash"] != fingerprint:
                    raise Error(409, "send idempotency payload conflict")
                return {"id": old["mail"], "duplicate": True}
            self.insert_mail(db, user, mail, descriptor, body)
            db.execute(
                "INSERT INTO receipts VALUES(?,?,?,?)", (user, key, fingerprint, mail)
            )
            db.execute(
                "INSERT INTO outbox VALUES(?,?,?,?,?,?,'pending',0,?,0,'','')",
                (
                    descriptor["delivery_id"],
                    user,
                    mail,
                    raw,
                    descriptor["sender"],
                    dumps(descriptor["recipients"]),
                    self.clock(),
                ),
            )
            return {"id": mail, "duplicate": False}

    def receive(self, user, delivery, descriptor, body):
        self.owner(user)
        self.writable()
        fingerprint = digest({"descriptor": descriptor, "body": body})
        mail = digest([user, delivery, "inbox"])
        with self.transaction(True) as db:
            prior = db.execute(
                "SELECT * FROM deliveries WHERE user=? AND id=?", (user, delivery)
            ).fetchone()
            if prior:
                if prior["hash"] != fingerprint:
                    raise Error(409, "delivery payload conflict")
                return {"id": prior["mail"], "duplicate": True}
            self.insert_mail(db, user, mail, descriptor, body)
            db.execute(
                "INSERT INTO deliveries VALUES(?,?,?,?)",
                (user, delivery, fingerprint, mail),
            )
        return {"id": mail, "duplicate": False}

    def update(self, user, mail, version, read=None, delete=False):
        self.writable()
        integer(version, "version", 1)
        if not delete and type(read) is not bool:
            raise ValueError("read must be boolean")
        with self.transaction(True) as db:
            row = self.get(user, mail, db)
            if row["version"] != version:
                raise Error(409, "stale message version; refresh before writing")
            if delete:
                db.execute(
                    "UPDATE mails SET deleted=1,version=version+1 WHERE id=?", (mail,)
                )
                db.execute("DELETE FROM postings WHERE mail=?", (mail,))
            else:
                db.execute(
                    "UPDATE mails SET read=?,version=version+1 WHERE id=?", (read, mail)
                )
            self.record(db, mail)
            return {"id": mail, "version": version + 1, "deleted": delete}

    def listing(self, user, folder="inbox", read=None, before=2**53, q="", field="all"):
        self.owner(user)
        if (
            folder not in ("inbox", "sent", "spam", "quarantine")
            or read not in (None, "true", "false")
            or field not in ("all", "subject", "sender", "body")
        ):
            raise ValueError("invalid mailbox filter")
        integer(before, "before")
        if not isinstance(q, str) or len(q) > 100 or len(terms(q)) > 5:
            raise ValueError("search allows at most 100 characters and five terms")
        clauses, args = (
            [
                "m.user=?",
                "m.deleted=0",
                "m.local_seq<?",
                "json_extract(m.descriptor,'$.folder')=?",
            ],
            [user, before, folder],
        )
        if read is not None:
            clauses.append("m.read=?")
            args.append(int(read == "true"))
        for term in terms(q):
            clause = "EXISTS(SELECT 1 FROM postings p WHERE p.mail=m.id AND p.term=?"
            args.append(term)
            if field != "all":
                clause += " AND p.field=?"
                args.append(field)
            clauses.append(clause + ")")
        with self.transaction() as db:
            rows = db.execute(
                "SELECT m.id,m.local_seq FROM mails m WHERE "
                + " AND ".join(clauses)
                + " ORDER BY m.local_seq DESC LIMIT 51",
                args,
            ).fetchall()
            return {
                "messages": [self.get(user, r["id"], db) for r in rows[:50]],
                "next_before": rows[49]["local_seq"] if len(rows) > 50 else None,
                "position": db.execute("SELECT position FROM metadata").fetchone()[0],
                "lineage": db.execute("SELECT lineage FROM metadata").fetchone()[0],
                "replica": self.replica,
            }

    def claim(self):
        self.writable()
        with self.transaction(True) as db:
            now = self.clock()
            row = db.execute(
                "SELECT * FROM outbox WHERE (status='pending' AND due<=?) OR (status='inflight' AND lease<=?) ORDER BY due,id LIMIT 1",
                (now, now),
            ).fetchone()
            if not row:
                return None
            token = uuid.uuid4().hex
            db.execute(
                "UPDATE outbox SET status='inflight',attempts=attempts+1,lease=?,token=? WHERE id=?",
                (now + 2, token, row["id"]),
            )
            return {
                **dict(row),
                "token": token,
                "recipients": json.loads(row["recipients"]),
            }

    def settle(self, job, error=None):
        with self.transaction(True) as db:
            row = db.execute(
                "SELECT * FROM outbox WHERE id=? AND token=? AND status='inflight'",
                (job["id"], job["token"]),
            ).fetchone()
            if not row:
                return False
            if error is not None:
                db.execute(
                    "UPDATE outbox SET status='pending',due=?,error=? WHERE id=?",
                    (
                        self.clock() + min(10, 0.1 * 2 ** min(row["attempts"], 6)),
                        str(error)[:300],
                        row["id"],
                    ),
                )
            else:
                db.execute(
                    "UPDATE outbox SET status='done',error='' WHERE id=?", (row["id"],)
                )
                mail = db.execute(
                    "SELECT * FROM mails WHERE id=?", (row["mail"],)
                ).fetchone()
                descriptor = json.loads(mail["descriptor"])
                descriptor["delivery"] = "smtp-accepted"
                db.execute(
                    "UPDATE mails SET descriptor=?,version=version+1 WHERE id=?",
                    (dumps(descriptor), row["mail"]),
                )
                self.record(db, row["mail"])
        return True

    def changes(self, after):
        integer(after, "after")
        with self.transaction() as db:
            meta = dict(db.execute("SELECT * FROM metadata").fetchone())
            if after > meta["position"]:
                raise Error(409, "replica cursor ahead of source")
            changes = []
            size = 0
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
        if not self.replica or (batch["shard"], batch["shards"]) != (
            self.shard,
            self.shards,
        ):
            raise Error(409, "replica topology mismatch")
        with self.transaction(True) as db:
            meta = dict(db.execute("SELECT * FROM metadata").fetchone())
            if meta["lineage"] and meta["lineage"] != batch["lineage"]:
                raise Error(409, "source lineage changed; explicit rebuild required")
            position = meta["position"]
            for change in batch["changes"]:
                if change["seq"] <= position:
                    continue
                if (
                    change["seq"] != position + 1
                    or digest(change["payload"]) != change["hash"]
                ):
                    raise Error(409, "replication gap or checksum mismatch")
                row = change["payload"]["record"]
                self.owner(row["user"])
                db.execute(
                    "INSERT INTO mails VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET descriptor=excluded.descriptor,read=excluded.read,version=excluded.version,deleted=excluded.deleted",
                    tuple(
                        row[k]
                        for k in (
                            "local_seq",
                            "id",
                            "user",
                            "descriptor",
                            "read",
                            "version",
                            "deleted",
                        )
                    ),
                )
                db.execute("DELETE FROM postings WHERE mail=?", (row["id"],))
                db.executemany(
                    "INSERT INTO postings VALUES(?,?,?)",
                    [(row["id"], f, t) for f, t in change["payload"]["terms"]],
                )
                position = change["seq"]
            db.execute(
                "UPDATE metadata SET lineage=?,position=?", (batch["lineage"], position)
            )
        return position
