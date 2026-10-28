"""Durable synthetic event consumer with source-scoped idempotency."""

from .common import Database, Error, checksum, encode
from .contracts import job as validate_job


class Consumer(Database):
    def __init__(self, path):
        super().__init__(path)
        self.initialize("""
        CREATE TABLE IF NOT EXISTS inbox(id TEXT PRIMARY KEY,hash TEXT,payload TEXT,attempts INTEGER);
        CREATE TABLE IF NOT EXISTS control(id INTEGER PRIMARY KEY CHECK(id=1),lose_ack INTEGER);
        INSERT OR IGNORE INTO control VALUES(1,0);
        """)

    def consume(self, job):
        job = validate_job(job)
        with self.transaction() as db:
            event_id = job["source_id"] + ":" + job["id"]
            old = db.execute("SELECT * FROM inbox WHERE id=?", (event_id,)).fetchone()
            if old is None:
                old = db.execute(
                    "SELECT * FROM inbox WHERE id=?", (job["id"],)
                ).fetchone()
                if old and old["hash"] == checksum(job["payload"]):
                    db.execute(
                        "UPDATE inbox SET id=? WHERE id=?", (event_id, job["id"])
                    )
            if old and old["hash"] != checksum(job["payload"]):
                raise Error(409, "consumer id reused with another payload")
            if old:
                db.execute(
                    "UPDATE inbox SET attempts=attempts+1 WHERE id=?", (event_id,)
                )
            else:
                db.execute(
                    "INSERT INTO inbox VALUES(?,?,?,1)",
                    (event_id, checksum(job["payload"]), encode(job["payload"])),
                )
            return not bool(db.execute("SELECT lose_ack FROM control").fetchone()[0])

    def control(self, lose_ack):
        if type(lose_ack) is not bool:
            raise ValueError("lose_ack must be boolean")
        with self.transaction() as db:
            db.execute("UPDATE control SET lose_ack=?", (lose_ack,))
        return {"updated": True}

    def status(self):
        with self.connection() as db:
            return {
                "unique_events": db.execute("SELECT count(*) FROM inbox").fetchone()[0],
                "attempts": db.execute(
                    "SELECT coalesce(sum(attempts),0) FROM inbox"
                ).fetchone()[0],
            }
