"""Alert configuration facade, state evaluation and durable notification delivery."""

import json
import uuid

from .alert_delivery import Delivery
from .alert_evaluation import Evaluation
from .alert_providers import Provider as Provider
from .alert_providers import WebhookProvider as WebhookProvider
from .alert_rules import MAX_RULES, normalize_rule, read_rules
from .alert_schema import initialize
from .common import Error, dumps, identifier, labels, number


class Alerts(Evaluation, Delivery):
    def __init__(self, store):
        self.store = store
        initialize(store)

    def _now(self):
        try:
            result = number(self.store.clock())
        except (OverflowError, TypeError) as exc:
            raise ValueError("invalid alert clock") from exc
        if result < 0:
            raise ValueError("invalid alert clock")
        return result

    def put_rules(self, bodies):
        if not isinstance(bodies, list) or not 1 <= len(bodies) <= MAX_RULES:
            raise ValueError("rule import requires 1..1000 rules")
        rules = [normalize_rule(body) for body in list(bodies)]
        if len({rule["id"] for rule in rules}) != len(rules):
            raise ValueError("duplicate rule ID in import")
        with self.store.transaction(True) as db:
            additions = 0
            for rule in rules:
                old = db.execute(
                    "SELECT body FROM rules WHERE id=?", (rule["id"],)
                ).fetchone()
                if old and old[0] != dumps(rule):
                    raise Error(409, "rule IDs are immutable; choose a new ID")
                additions += int(old is None)
            if (
                db.execute("SELECT count(*) FROM rules").fetchone()[0] + additions
                > MAX_RULES
            ):
                raise Error(429, "alert rule capacity exceeded")
            db.executemany(
                "INSERT OR IGNORE INTO rules VALUES(?,?,'')",
                [(rule["id"], dumps(rule)) for rule in rules],
            )
        return rules

    def put_rule(self, body):
        return self.put_rules([body])[0]

    def load_rules(self, path):
        """Import a bounded JSON rule array/object atomically, preserving IDs."""
        return self.put_rules(read_rules(path))

    def silence(self, match, start, end, id=None):
        match = labels(match)
        start, end = number(start), number(end)
        if not 0 <= start < end or end - start > 7 * 86400:
            raise ValueError(
                "silence requires nonnegative increasing times within seven days"
            )
        id = identifier(id) if id is not None else uuid.uuid4().hex
        with self.store.transaction(True) as db:
            now = self._now()
            db.execute("DELETE FROM silences WHERE end<?", (now - 86400,))
            old = db.execute(
                "SELECT match,start,end FROM silences WHERE id=?", (id,)
            ).fetchone()
            if old:
                if tuple(old) != (dumps(match), start, end):
                    raise Error(409, "silence ID payload conflict")
                return {"id": id}
            if db.execute("SELECT count(*) FROM silences").fetchone()[0] >= 1000:
                raise Error(429, "silence capacity exceeded")
            db.execute(
                "INSERT INTO silences VALUES(?,?,?,?)", (id, dumps(match), start, end)
            )
        return {"id": id}

    def _silenced_until(self, db, labelset, now=None):
        now = self._now() if now is None else now
        until = 0.0
        for row in db.execute(
            "SELECT * FROM silences WHERE start<=? AND end>?", (now, now)
        ):
            if all(
                labelset.get(key) == value
                for key, value in json.loads(row["match"]).items()
            ):
                until = max(until, row["end"])
        return until

    def status(self, limit=1000):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("status limit must be 1..1000")
        with self.store.transaction() as db:
            counts = {
                table: db.execute("SELECT count(*) FROM " + table).fetchone()[0]
                for table in ("alerts", "deliveries", "rules")
            }
            return {
                "alerts": [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM alerts ORDER BY id LIMIT ?", (limit,)
                    )
                ],
                "deliveries": [
                    {
                        key: row[key]
                        for key in ("id", "status", "attempts", "due", "error")
                    }
                    for row in db.execute(
                        "SELECT * FROM deliveries ORDER BY seq LIMIT ?", (limit,)
                    )
                ],
                "rules": [
                    {"id": row["id"], "error": row["error"]}
                    for row in db.execute(
                        "SELECT * FROM rules ORDER BY id LIMIT ?", (limit,)
                    )
                ],
                "counts": counts,
                "truncated": {key: count > limit for key, count in counts.items()},
                "limit": limit,
            }
