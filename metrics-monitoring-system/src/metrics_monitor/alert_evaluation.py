"""Consistent per-rule snapshots, lock-free aggregation and CAS publication."""

import json

from .common import Error, digest, dumps
from .storage import combine, reading, singleton

MAX_SERIES = 1000
MAX_POINTS = 20000


def next_state(previous, value, now, rule):
    old = previous["state"] if previous else "inactive"
    since = previous["since"] if previous else now
    transition = previous["transition"] if previous else 0
    # A pending interval cannot survive an unobserved gap longer than max_age.
    if old == "pending" and now - previous["updated"] > rule["max_age"]:
        old, since = "inactive", now
    emit = False
    state = old
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
                since, state = now, "pending"
            if old == "firing" or now - since >= rule["for_seconds"]:
                state, emit = "firing", old != "firing"
        else:
            state, since, emit = (
                ("resolved" if old == "firing" else "inactive"),
                now,
                old == "firing",
            )
    return state, since, transition + int(emit), emit


class Evaluation:
    def _evaluation_snapshot(self, rule):
        with self.store.transaction() as db:
            # The first read pins series, samples, prior states and version together.
            version = db.execute(
                "SELECT version,updated FROM evaluations WHERE rule_id=?", (rule["id"],)
            ).fetchone()
            now = self._now()
            previous_time = version["updated"] if version else -1
            selected = {
                row["id"]: dict(row)
                for row in self.store.select(db, rule["metric"], rule["match"])
            }
            old = db.execute(
                "SELECT * FROM alerts WHERE rule_id=? ORDER BY series LIMIT ?",
                (rule["id"], MAX_SERIES + 1),
            ).fetchall()
            existing = {row["series"]: dict(row) for row in old}
            series_ids = sorted(set(selected) | set(existing))
            if len(series_ids) > MAX_SERIES:
                raise Error(413, "evaluation series budget exceeded")
            snapshots = []
            remaining = MAX_POINTS
            for sid in series_ids:
                series = selected.get(sid)
                prior = existing.get(sid)
                points = db.execute(
                    "SELECT timestamp,value FROM raw WHERE series=? AND timestamp BETWEEN ? AND ? ORDER BY timestamp LIMIT ?",
                    (sid, now - rule["window"], now, remaining + 1),
                ).fetchall()
                if len(points) > remaining:
                    raise Error(413, "evaluation sample budget exceeded")
                remaining -= len(points)
                snapshots.append(
                    (sid, series, prior, [(row[0], row[1]) for row in points])
                )
        return now, version["version"] if version else 0, previous_time, snapshots

    def _evaluate_snapshot(self, rule, now, snapshots):
        updates = []
        error = ""
        for sid, series, previous, points in snapshots:
            labelset = json.loads(series["labels"] if series else previous["labels"])
            labelset.update(rule.get("labels", {}))
            value = None
            if series and points and points[-1][0] >= now - rule["max_age"]:
                try:
                    value = reading(
                        combine(
                            [singleton(stamp, value) for stamp, value in points],
                            series["kind"],
                        ),
                        rule["aggregation"],
                        series["kind"],
                    )
                except ValueError:
                    error = "aggregation incompatible with metric type"
            state, since, transition, emit = next_state(previous, value, now, rule)
            updates.append((sid, labelset, value, state, since, transition, emit))
        return updates, error

    def _accept_evaluation(self, rule, now, version, updates, error):
        with self.store.transaction(True) as db:
            db.execute(
                "INSERT OR IGNORE INTO evaluations VALUES(?,0,-1)", (rule["id"],)
            )
            changed = db.execute(
                "UPDATE evaluations SET version=version+1,updated=? WHERE rule_id=? AND version=? AND updated<=?",
                (now, rule["id"], version, now),
            ).rowcount
            if not changed:
                return False
            for sid, labelset, value, state, since, transition, emit in updates:
                alert_id = digest([rule["id"], sid])
                db.execute(
                    "INSERT INTO alerts VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,since=excluded.since,transition=excluded.transition,updated=excluded.updated,no_data=excluded.no_data,labels=excluded.labels,value=excluded.value",
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
                            {key: labelset.get(key, "") for key in rule["group_by"]},
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
                            self._silenced_until(db, labelset, now),
                        ),
                    )
            db.execute("UPDATE rules SET error=? WHERE id=?", (error, rule["id"]))
            db.execute("DELETE FROM silences WHERE end<?", (now - 86400,))
        return True

    def evaluate(self):
        with self.store.transaction() as db:
            configured = db.execute(
                "SELECT * FROM rules ORDER BY id LIMIT 1001"
            ).fetchall()
        if len(configured) > 1000:
            raise Error(413, "evaluation rule budget exceeded")
        for configured_rule in configured:
            rule = json.loads(configured_rule["body"])
            for _attempt in range(3):
                try:
                    now, version, previous_time, snapshots = self._evaluation_snapshot(
                        rule
                    )
                    if now < previous_time:
                        break  # A wall-clock rollback must not regress accepted state.
                    updates, error = self._evaluate_snapshot(rule, now, snapshots)
                    if self._accept_evaluation(rule, now, version, updates, error):
                        break
                except (Error, ValueError):
                    with self.store.transaction(True) as db:
                        db.execute(
                            "UPDATE rules SET error='evaluation failed or budget exceeded' WHERE id=?",
                            (rule["id"],),
                        )
                    break
            else:
                # Another evaluator has already accepted fresher state; do not
                # overwrite it or claim a new transition after bounded retries.
                continue
        return self.status()
