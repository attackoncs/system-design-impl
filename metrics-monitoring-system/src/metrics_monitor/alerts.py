from abc import ABC, abstractmethod
from contextlib import closing
import json
import uuid

from .common import Error, digest, dumps, http_json, identifier, labels, number
from .storage import combine, reading, singleton


class Provider(ABC):
    @abstractmethod
    def deliver(self, destination, channel, payload, key):
        """Return only after provider confirmation; retries reuse key."""


class WebhookProvider(Provider):
    def __init__(self, destinations):
        self.destinations = destinations

    def deliver(self, destination, channel, payload, key):
        if channel != "webhook":
            raise RuntimeError("email/phone/PagerDuty adapter not configured")
        target = self.destinations.get(destination)
        if not target:
            raise RuntimeError("notification destination not configured")
        return http_json(
            target["url"],
            "POST",
            payload,
            token=target.get("token"),
            headers={"Idempotency-Key": key},
        )


class Alerts:
    def __init__(self, store):
        self.store = store
        with closing(store.connect()) as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS rules(id TEXT PRIMARY KEY,body TEXT,error TEXT);
            CREATE TABLE IF NOT EXISTS alerts(id TEXT PRIMARY KEY,rule_id TEXT,series TEXT,state TEXT,since REAL,transition INTEGER,updated REAL,no_data INTEGER,labels TEXT,value REAL);
            CREATE TABLE IF NOT EXISTS silences(id TEXT PRIMARY KEY,match TEXT,start REAL,end REAL);
            CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,alert_id TEXT,transition INTEGER,group_key TEXT,destination TEXT,channel TEXT,payload TEXT,due REAL,batch TEXT,status TEXT);
            CREATE TABLE IF NOT EXISTS deliveries(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,group_key TEXT,destination TEXT,channel TEXT,payload TEXT,due REAL,attempts INTEGER,lease TEXT,lease_until REAL,status TEXT,error TEXT);
            """)
            db.commit()

    def put_rule(self, body):
        if not isinstance(body, dict) or set(body) - {
            "id",
            "metric",
            "match",
            "aggregation",
            "op",
            "threshold",
            "for_seconds",
            "window",
            "max_age",
            "group_by",
            "destination",
            "channel",
        }:
            raise ValueError("invalid rule fields")
        rule = {
            "match": {},
            "aggregation": "last",
            "for_seconds": 0,
            "window": 300,
            "max_age": 300,
            "group_by": ["instance"],
            "channel": "webhook",
            **body,
        }
        for key in ("id", "metric", "destination"):
            identifier(rule[key])
        rule["match"] = labels(rule["match"])
        rule["threshold"] = number(rule["threshold"])
        if rule["op"] not in (">", ">=", "<", "<=", "==") or rule["channel"] not in (
            "webhook",
            "email",
            "phone",
            "pagerduty",
        ):
            raise ValueError("invalid alert operation/channel")
        if rule["aggregation"] not in ("last", "avg", "min", "max", "increase", "rate"):
            raise ValueError("invalid rule aggregation")
        for key, lower, upper in [
            ("for_seconds", 0, 86400),
            ("window", 1, 3600),
            ("max_age", 1, 3600),
        ]:
            value = number(rule[key])
            if not lower <= value <= upper:
                raise ValueError("invalid rule duration")
        if not isinstance(rule["group_by"], list) or len(set(rule["group_by"])) != len(
            rule["group_by"]
        ):
            raise ValueError("invalid group_by")
        labels({key: "" for key in rule["group_by"]})
        with self.store.transaction(True) as db:
            old = db.execute(
                "SELECT body FROM rules WHERE id=?", (rule["id"],)
            ).fetchone()
            if old and old[0] != dumps(rule):
                raise Error(409, "rule IDs are immutable; choose a new ID")
            db.execute(
                "INSERT OR IGNORE INTO rules VALUES(?,?,'')", (rule["id"], dumps(rule))
            )
        return rule

    def silence(self, match, start, end, id=None):
        match = labels(match)
        start, end = number(start), number(end)
        if not start < end or end - start > 7 * 86400:
            raise ValueError("silence requires increasing times within seven days")
        id = identifier(id) if id else uuid.uuid4().hex
        with self.store.transaction(True) as db:
            db.execute(
                "INSERT INTO silences VALUES(?,?,?,?)", (id, dumps(match), start, end)
            )
        return {"id": id}

    def _silenced_until(self, db, labelset):
        until = 0.0
        for row in db.execute(
            "SELECT * FROM silences WHERE start<=? AND end>?",
            (self.store.clock(), self.store.clock()),
        ):
            if all(
                labelset.get(key) == value
                for key, value in json.loads(row["match"]).items()
            ):
                until = max(until, row["end"])
        return until

    def evaluate(self):
        now = self.store.clock()
        with self.store.transaction(True) as db:
            for configured in db.execute("SELECT * FROM rules ORDER BY id").fetchall():
                rule = json.loads(configured["body"])
                try:
                    selected = {
                        row["id"]: row
                        for row in self.store.select(db, rule["metric"], rule["match"])
                    }
                except (Error, ValueError) as exc:
                    db.execute(
                        "UPDATE rules SET error=? WHERE id=?", (str(exc), rule["id"])
                    )
                    continue
                existing = {
                    row["series"]: dict(row)
                    for row in db.execute(
                        "SELECT * FROM alerts WHERE rule_id=?", (rule["id"],)
                    )
                }
                for sid in sorted(set(selected) | set(existing)):
                    series = selected.get(sid)
                    previous = existing.get(sid)
                    labelset = json.loads(
                        series["labels"] if series else previous["labels"]
                    )
                    points = db.execute(
                        "SELECT timestamp,value FROM raw WHERE series=? AND timestamp BETWEEN ? AND ? ORDER BY timestamp LIMIT 20001",
                        (sid, now - rule["window"], now),
                    ).fetchall()
                    if len(points) > 20000:
                        db.execute(
                            "UPDATE rules SET error='evaluation sample budget exceeded' WHERE id=?",
                            (rule["id"],),
                        )
                        continue
                    value = None
                    if points and points[-1][0] >= now - rule["max_age"]:
                        try:
                            value = reading(
                                combine(
                                    [singleton(row[0], row[1]) for row in points],
                                    series["kind"],
                                ),
                                rule["aggregation"],
                                series["kind"],
                            )
                            db.execute(
                                "UPDATE rules SET error='' WHERE id=?", (rule["id"],)
                            )
                        except ValueError as exc:
                            db.execute(
                                "UPDATE rules SET error=? WHERE id=?",
                                (str(exc), rule["id"]),
                            )
                    old = previous["state"] if previous else "inactive"
                    since = previous["since"] if previous else now
                    transition = previous["transition"] if previous else 0
                    state = old
                    emit = False
                    if value is None:
                        state = "firing" if old == "firing" else "inactive"
                        if state != "firing":
                            since = now
                    else:
                        threshold = rule["threshold"]
                        active = {
                            ">": value > threshold,
                            ">=": value >= threshold,
                            "<": value < threshold,
                            "<=": value <= threshold,
                            "==": value == threshold,
                        }[rule["op"]]
                        if active:
                            if old not in ("pending", "firing"):
                                since = now
                                state = "pending"
                            if old == "firing" or now - since >= rule["for_seconds"]:
                                state = "firing"
                                emit = old != "firing"
                        else:
                            state = "resolved" if old == "firing" else "inactive"
                            since = now
                            emit = old == "firing"
                    if emit:
                        transition += 1
                    alert_id = digest([rule["id"], sid])
                    db.execute(
                        "INSERT INTO alerts VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,since=excluded.since,transition=excluded.transition,updated=excluded.updated,no_data=excluded.no_data,value=excluded.value",
                        (
                            alert_id,
                            rule["id"],
                            sid,
                            state,
                            since,
                            transition,
                            now,
                            int(value is None),
                            dumps(labelset),
                            value,
                        ),
                    )
                    if emit:
                        event_id = digest([alert_id, transition])
                        group = digest(
                            [
                                rule["id"],
                                rule["destination"],
                                rule["channel"],
                                {
                                    key: labelset.get(key, "")
                                    for key in rule["group_by"]
                                },
                            ]
                        )
                        payload = {
                            "event_id": event_id,
                            "rule": rule["id"],
                            "metric": rule["metric"],
                            "labels": labelset,
                            "state": state,
                            "value": value,
                            "timestamp": now,
                            "transition": transition,
                        }
                        db.execute(
                            "INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?,?,NULL,'pending')",
                            (
                                event_id,
                                alert_id,
                                transition,
                                group,
                                rule["destination"],
                                rule["channel"],
                                dumps(payload),
                                self._silenced_until(db, labelset),
                            ),
                        )
            db.execute("DELETE FROM silences WHERE end<?", (now - 86400,))
        return self.status()

    def _materialize(self, db):
        groups = {}
        for event in db.execute(
            "SELECT * FROM events WHERE status='pending' AND batch IS NULL AND due<=? ORDER BY due,id LIMIT 1000",
            (self.store.clock(),),
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
            due = self._silenced_until(db, payload["labels"])
            if due > self.store.clock():
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
                        self.store.clock(),
                    ),
                )
                for event in batch:
                    db.execute(
                        "UPDATE events SET batch=? WHERE id=?", (id, event["id"])
                    )

    def notify_once(self, provider):
        now = self.store.clock()
        with self.store.transaction(True) as db:
            self._materialize(db)
            now = self.store.clock()
            task = db.execute(
                """SELECT d.* FROM deliveries d WHERE d.status='pending' AND d.due<=? AND d.lease_until<=?
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
                    self._silenced_until(db, event["labels"])
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
            db.execute(
                "UPDATE deliveries SET lease=?,lease_until=? WHERE id=?",
                (lease, now + 30, task["id"]),
            )
        error = None
        try:
            provider.deliver(task["destination"], task["channel"], payload, task["id"])
        except Exception as exc:
            error = type(exc).__name__
        finished = self.store.clock()
        with self.store.transaction(True) as db:
            attempts = task["attempts"] + 1
            if error is None:
                changed = db.execute(
                    "UPDATE deliveries SET status='sent',attempts=?,lease=NULL,lease_until=0 WHERE id=? AND lease=?",
                    (attempts, task["id"], lease),
                ).rowcount
                if changed:
                    db.execute(
                        "UPDATE events SET status='sent' WHERE batch=?", (task["id"],)
                    )
            else:
                db.execute(
                    "UPDATE deliveries SET attempts=?,due=?,lease=NULL,lease_until=0,status=?,error=? WHERE id=? AND lease=?",
                    (
                        attempts,
                        finished + min(300, 2**attempts),
                        "dead" if attempts >= 5 else "pending",
                        error,
                        task["id"],
                        lease,
                    ),
                )
        return True

    def status(self):
        with self.store.transaction() as db:
            return {
                "alerts": [
                    dict(r) for r in db.execute("SELECT * FROM alerts ORDER BY id")
                ],
                "deliveries": [
                    {
                        key: r[key]
                        for key in ("id", "status", "attempts", "due", "error")
                    }
                    for r in db.execute("SELECT * FROM deliveries ORDER BY seq")
                ],
                "rules": [
                    {"id": r["id"], "error": r["error"]}
                    for r in db.execute("SELECT * FROM rules ORDER BY id")
                ],
            }
