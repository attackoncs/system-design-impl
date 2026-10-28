"""Bounded immutable notification grouping, lease claims and fenced results."""

import json
import uuid

from .common import digest, dumps


class Delivery:
    def _materialize(self, db, now):
        groups = {}
        for event in db.execute(
            "SELECT * FROM events WHERE status='pending' AND batch IS NULL AND due<=? ORDER BY due,id LIMIT 1000",
            (now,),
        ).fetchall():
            alert = db.execute(
                "SELECT transition FROM alerts WHERE id=?", (event["alert_id"],)
            ).fetchone()
            if not alert or alert[0] != event["transition"]:
                db.execute(
                    "UPDATE events SET status='superseded' WHERE id=?", (event["id"],)
                )
                continue
            payload = json.loads(event["payload"])
            due = self._silenced_until(db, payload["labels"], now)
            if due > now:
                db.execute("UPDATE events SET due=? WHERE id=?", (due, event["id"]))
                continue
            key = (event["group_key"], event["destination"], event["channel"])
            groups.setdefault(key, []).append(event)
        for (group, destination, channel), events in groups.items():
            for start in range(0, len(events), 100):
                batch = events[start : start + 100]
                id = digest(sorted(row["id"] for row in batch))
                payload = {
                    "notification_id": id,
                    "events": [json.loads(row["payload"]) for row in batch],
                }
                db.execute(
                    "INSERT OR IGNORE INTO deliveries(id,group_key,destination,channel,payload,due,attempts,lease,lease_until,status,error) VALUES(?,?,?,?,?,?,0,NULL,0,'pending','')",
                    (
                        id,
                        group,
                        destination,
                        channel,
                        dumps(payload),
                        now,
                    ),
                )
                for event in batch:
                    db.execute(
                        "UPDATE events SET batch=? WHERE id=?", (id, event["id"])
                    )

    def notify_once(self, provider):
        with self.store.transaction(True) as db:
            now = self._now()
            self._materialize(db, now)
            # Count attempts at claim, so a worker crash cannot retry forever
            # without consuming its bounded delivery budget.
            exhausted = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM deliveries WHERE status='pending' AND attempts>=5 AND lease_until<=? LIMIT 1000",
                    (now,),
                )
            ]
            for exhausted_id in exhausted:
                db.execute(
                    "UPDATE deliveries SET status='dead',lease=NULL,lease_until=0,error='delivery lease expired at attempt limit' WHERE id=?",
                    (exhausted_id,),
                )
                db.execute(
                    "UPDATE events SET status='dead' WHERE batch=?", (exhausted_id,)
                )
            task = db.execute(
                """SELECT d.* FROM deliveries d WHERE d.status='pending' AND d.attempts<5 AND d.due<=? AND d.lease_until<=?
              AND NOT EXISTS(SELECT 1 FROM deliveries old WHERE old.group_key=d.group_key AND old.seq<d.seq AND old.status='pending')
              ORDER BY d.seq LIMIT 1""",
                (now, now),
            ).fetchone()
            if not task:
                return False
            task = dict(task)
            payload = json.loads(task["payload"])
            postponed = max(
                [
                    self._silenced_until(db, event["labels"], now)
                    for event in payload["events"]
                ]
                + [now]
            )
            if postponed > now:
                db.execute(
                    "UPDATE deliveries SET due=? WHERE id=?", (postponed, task["id"])
                )
                return False
            lease = uuid.uuid4().hex
            attempts = task["attempts"] + 1
            db.execute(
                "UPDATE deliveries SET lease=?,lease_until=?,attempts=? WHERE id=?",
                (lease, now + 30, attempts, task["id"]),
            )
        error = None
        try:
            provider.deliver(task["destination"], task["channel"], payload, task["id"])
        except Exception as exc:
            error = type(exc).__name__
        with self.store.transaction(True) as db:
            finished = max(now, self._now())
            if error is None:
                changed = db.execute(
                    "UPDATE deliveries SET status='sent',attempts=?,lease=NULL,lease_until=0,error='' WHERE id=? AND lease=?",
                    (attempts, task["id"], lease),
                ).rowcount
                if changed:
                    db.execute(
                        "UPDATE events SET status='sent' WHERE batch=?", (task["id"],)
                    )
            else:
                changed = db.execute(
                    "UPDATE deliveries SET attempts=?,due=?,lease=NULL,lease_until=0,status=?,error=? WHERE id=? AND lease=?",
                    (
                        attempts,
                        finished + min(300, 2**attempts),
                        "dead" if attempts >= 5 else "pending",
                        error,
                        task["id"],
                        lease,
                    ),
                ).rowcount
                if changed and attempts >= 5:
                    db.execute(
                        "UPDATE events SET status='dead' WHERE batch=?", (task["id"],)
                    )
        return True
