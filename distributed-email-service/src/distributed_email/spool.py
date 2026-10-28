"""Durable inbound acceptance and fenced, bounded receive jobs."""

from email import policy
from email.parser import BytesParser
import json
import math
import re
import time
import uuid

from .common import Database, Error, digest, dumps, http_json, shard_for
from .mail import local_user, parse_received
from .objects import Objects


class Spool(Database):
    def __init__(self, path, *, clock=time.time, lease_seconds=90):
        if (
            type(lease_seconds) not in (int, float)
            or not math.isfinite(lease_seconds)
            or lease_seconds <= 0
        ):
            raise ValueError("positive finite receive lease required")
        super().__init__(path)
        self.clock, self.lease_seconds = clock, lease_seconds
        self.initialize("""
        CREATE TABLE IF NOT EXISTS spool(id TEXT PRIMARY KEY,hash TEXT,raw TEXT,sender TEXT,recipients TEXT,
            status TEXT,attempts INTEGER,due REAL,error TEXT);
        CREATE TABLE IF NOT EXISTS control(id INTEGER PRIMARY KEY CHECK(id=1),lose_ack INTEGER,scanner_down INTEGER);
        INSERT OR IGNORE INTO control VALUES(1,0,0);
        """)
        # Migrate existing durable spools without dropping acceptance receipts.
        with self.transaction(True) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(spool)")}
            if "lease" not in columns:
                db.execute("ALTER TABLE spool ADD COLUMN lease REAL NOT NULL DEFAULT 0")
            if "token" not in columns:
                db.execute(
                    "ALTER TABLE spool ADD COLUMN token TEXT NOT NULL DEFAULT ''"
                )
            db.execute(
                "CREATE INDEX IF NOT EXISTS spool_due ON spool(status,due,lease)"
            )

    def accept(self, config, raw, sender, recipients):
        if not isinstance(raw, bytes) or not 0 < len(raw) <= 8 * 1024 * 1024:
            raise ValueError("RFC message must be 1 byte..8 MiB")
        local_user(sender, config)
        if not isinstance(recipients, list) or not 1 <= len(recipients) <= 10:
            raise ValueError("1..10 distinct local recipients required")
        for recipient in recipients:
            local_user(recipient, config)
        if len(set(recipients)) != len(recipients):
            raise ValueError("duplicate recipient")
        message = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
        deliveries = message.get_all("X-Delivery-ID", [])
        if len(deliveries) != 1 or not re.fullmatch(
            r"[a-f0-9]{64}", str(deliveries[0])
        ):
            raise ValueError("local transport requires one stable X-Delivery-ID")
        delivery = str(deliveries[0])
        key = Objects(config).put(raw)["key"]
        fingerprint = digest([key, sender, recipients])
        with self.transaction(True) as db:
            prior = db.execute(
                "SELECT hash FROM spool WHERE id=?", (delivery,)
            ).fetchone()
            if prior and prior[0] != fingerprint:
                raise Error(409, "SMTP delivery ID payload conflict")
            db.execute(
                "INSERT OR IGNORE INTO spool(id,hash,raw,sender,recipients,status,attempts,due,error) "
                "VALUES(?,?,?,?,?,'pending',0,?,'')",
                (delivery, fingerprint, key, sender, dumps(recipients), self.clock()),
            )
            lost = db.execute("SELECT lose_ack FROM control").fetchone()[0]
        return delivery, lost

    def control(self, body=None):
        with self.transaction(True) as db:
            if body is not None:
                if (
                    not isinstance(body, dict)
                    or set(body) != {"lose_ack", "scanner_down"}
                    or any(type(v) is not bool for v in body.values())
                ):
                    raise ValueError("control requires boolean lose_ack,scanner_down")
                db.execute(
                    "UPDATE control SET lose_ack=?,scanner_down=?",
                    (body["lose_ack"], body["scanner_down"]),
                )
            return dict(db.execute("SELECT * FROM control").fetchone())

    def status(self):
        with self.transaction() as db:
            return {
                "unique_deliveries": db.execute(
                    "SELECT count(*) FROM spool"
                ).fetchone()[0],
                "pending": db.execute(
                    "SELECT count(*) FROM spool WHERE status='pending'"
                ).fetchone()[0],
                "rows": [
                    dict(r)
                    for r in db.execute(
                        "SELECT id,status,attempts,error FROM spool ORDER BY id LIMIT 100"
                    )
                ],
            }

    def claim(self):
        with self.transaction(True) as db:
            # Take the clock after obtaining the SQLite writer lock.
            now = self.clock()
            row = db.execute(
                "SELECT * FROM spool WHERE status='pending' AND due<=? AND lease<=? ORDER BY due,id LIMIT 1",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            token, lease = uuid.uuid4().hex, now + self.lease_seconds
            db.execute(
                "UPDATE spool SET token=?,lease=?,attempts=attempts+1 WHERE id=?",
                (token, lease, row["id"]),
            )
            return {
                **dict(row),
                "token": token,
                "lease": lease,
                "attempts": row["attempts"] + 1,
            }

    def settle(self, job, error=None, *, rejected=False):
        with self.transaction(True) as db:
            now = self.clock()
            status = (
                "rejected" if rejected else "pending" if error is not None else "done"
            )
            due = now + min(10, 0.1 * 2 ** min(job["attempts"] - 1, 6))
            return bool(
                db.execute(
                    "UPDATE spool SET status=?,due=?,error=?,lease=0,token='' "
                    "WHERE id=? AND status='pending' AND token=? AND lease>?",
                    (
                        status,
                        due,
                        "" if error is None else str(error)[:300],
                        job["id"],
                        job["token"],
                        now,
                    ),
                ).rowcount
            )

    def process_once(self, config, *, stop=None):
        if stop is not None and stop.is_set():
            return False
        job = self.claim()
        if job is None:
            return False
        deadline = time.monotonic() + min(60, self.lease_seconds * 2 / 3)
        try:
            if self.control()["scanner_down"]:
                raise Error(
                    503, "scanner unavailable; message remains in durable spool"
                )
            recipients = json.loads(job["recipients"])
            descriptor, body = parse_received(
                config,
                Objects(config).get(job["raw"], deadline=deadline, cancel=stop),
                job["sender"],
                recipients,
                job["id"],
                deadline=deadline,
                cancel=stop,
            )
            for recipient in recipients:
                user = local_user(recipient, config)
                url = config["primary_urls"][
                    shard_for(user, len(config["primary_urls"]))
                ]
                http_json(
                    url + "/internal/receive",
                    config["internal_token"],
                    {
                        "user": user,
                        "delivery": job["id"],
                        "descriptor": descriptor,
                        "body": body,
                    },
                    deadline=deadline,
                    cancel=stop,
                )
            self.settle(job)
        except ValueError as exc:
            self.settle(job, exc, rejected=True)
        except Exception as exc:
            self.settle(job, exc)
        return True
