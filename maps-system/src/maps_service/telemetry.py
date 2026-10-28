"""Private report ingestion and bounded-retention traffic derivation."""

import hashlib
import json

from .geo import coordinates, match_point, nearby_boxes
from .models import (
    Error,
    MODES,
    dumps,
    finite,
    identifier,
    positive,
    report as validate_report,
)


class Telemetry:
    def match(self, db, lat, lon, mode, heading=None):
        if heading is not None and (
            type(heading) not in (int, float)
            or not finite(heading)
            or not 0 <= heading < 360
        ):
            raise ValueError("invalid heading")
        coordinates(lat, lon)
        if not isinstance(mode, str) or mode not in MODES:
            raise ValueError("invalid matching mode")
        candidates, examined = {}, set()
        for box in nearby_boxes(lat, lon, 100):
            for row in self.spatial_edges(db, *box, limit=10001):
                examined.add(row["id"])
                if len(examined) > 10000:
                    raise Error(413, "map matching candidate budget exceeded")
                if mode not in json.loads(row["speeds"]):
                    continue
                gap, fraction, bearing = match_point(
                    (lat, lon), (row["alat"], row["alon"]), (row["blat"], row["blon"])
                )
                angle = (
                    abs((bearing - heading + 180) % 360 - 180)
                    if heading is not None
                    else 0
                )
                if gap <= 100 and angle <= 60:
                    candidates[row["id"]] = (
                        gap,
                        angle,
                        row["id"],
                        fraction,
                        row["length"],
                    )
        ranked = sorted(candidates.values())
        if not ranked:
            return None
        best = ranked[0]
        ambiguous = (
            heading is None
            or len(ranked) > 1
            and abs(ranked[1][0] - best[0]) < 1
            and abs(ranked[1][1] - best[1]) < 5
        )
        return {
            "edge": best[2],
            "fraction": best[3],
            "gap_m": best[0],
            "length_m": best[4],
            "traffic_eligible": not ambiguous,
        }

    def _prune(self, db, now=None, user=None):
        now = self.now() if now is None else now
        for table, retention in (
            ("events", 86400),
            ("positions", 86400),
            ("samples", 300),
        ):
            if user is None:
                db.execute(
                    "DELETE FROM " + table + " WHERE received<=?", (now - retention,)
                )
            else:
                db.execute(
                    "DELETE FROM " + table + " WHERE user=? AND received<=?",
                    (user, now - retention),
                )

    def prune(self):
        if not self.readonly:
            with self.transaction(True) as db:
                self._prune(db)

    def ingest(self, user, reports):
        self.writable()
        if not isinstance(reports, list) or not 1 <= len(reports) <= 100:
            raise ValueError("batch requires 1..100 reports")
        identifier(user)
        reports = [validate_report(body) for body in reports]
        output = []
        with self.transaction(True) as db:
            meta = self.ready(db)
            if not db.execute("SELECT 1 FROM users WHERE id=?", (user,)).fetchone():
                raise Error(404, "user not found")
            now = self.now()
            self._prune(db, now, user)
            for body in reports:
                event = identifier(body["event_id"])
                fingerprint = hashlib.sha256(dumps(body).encode()).hexdigest()
                replay = db.execute(
                    "SELECT hash,result FROM events WHERE user=? AND event=?",
                    (user, event),
                ).fetchone()
                if replay:
                    if replay["hash"] != fingerprint:
                        raise Error(409, "event ID reused with different report")
                    output.append(json.loads(replay["result"]))
                    continue
                seq = body["sequence"]
                stamp = body["timestamp"]
                if (
                    type(seq) is not int
                    or not 0 <= seq < 2**53
                    or type(stamp) not in (float, int)
                    or not finite(stamp)
                    or not now - 86400 < stamp <= now + 30
                ):
                    raise ValueError("invalid report sequence/timestamp")
                lat, lon = coordinates(body["latitude"], body["longitude"])
                mode = body.get("mode", "driving")
                if mode not in MODES:
                    raise ValueError("invalid report mode")
                matched = self.match(db, lat, lon, mode, body.get("heading"))
                previous = db.execute(
                    "SELECT * FROM positions WHERE user=?", (user,)
                ).fetchone()
                advances = (
                    previous is None
                    or seq > previous["seq"]
                    and stamp > previous["timestamp"]
                )
                result = {
                    "event_id": event,
                    "sequence": seq,
                    "current": advances,
                    "graph": meta["graph"],
                    "match": matched,
                }
                if advances:
                    position = {
                        "latitude": lat,
                        "longitude": lon,
                        "timestamp": stamp,
                        "graph": meta["graph"],
                        "mode": mode,
                        "match": matched,
                    }
                    if (
                        previous
                        and mode == "driving"
                        and matched
                        and matched["traffic_eligible"]
                    ):
                        old = json.loads(previous["body"])
                        old_match = old.get("match")
                        interval = stamp - previous["timestamp"]
                        if (
                            old["graph"] == meta["graph"]
                            and old.get("mode") == "driving"
                            and old_match
                            and old_match["traffic_eligible"]
                            and old_match["edge"] == matched["edge"]
                            and 1 <= interval <= 120
                            and now - 300 < previous["timestamp"] <= stamp <= now + 30
                        ):
                            speed = (
                                (matched["fraction"] - old_match["fraction"])
                                * matched["length_m"]
                                / interval
                            )
                            if 0.5 <= speed <= 50:
                                db.execute(
                                    "INSERT INTO samples VALUES(?,?,?,?,?)",
                                    (user, event, matched["edge"], speed, now),
                                )
                    db.execute(
                        "INSERT INTO positions VALUES(?,?,?,?,?) ON CONFLICT(user) DO UPDATE SET seq=excluded.seq,timestamp=excluded.timestamp,body=excluded.body,received=excluded.received",
                        (user, seq, stamp, dumps(position), now),
                    )
                stored = {**result, "report": body}
                db.execute(
                    "INSERT INTO events VALUES(?,?,?,?,?)",
                    (user, event, fingerprint, dumps(stored), now),
                )
                output.append(stored)
        return {"accepted": len(output), "reports": output}

    def history(self, user):
        identifier(user)
        with self.transaction() as db:
            if not db.execute("SELECT 1 FROM users WHERE id=?", (user,)).fetchone():
                raise Error(404, "user not found")
            now = self.now()
            return {
                "reports": [
                    json.loads(r[0])
                    for r in db.execute(
                        "SELECT result FROM events WHERE user=? AND received>? ORDER BY received DESC,event LIMIT 100",
                        (user, now - 86400),
                    )
                ]
            }

    def _traffic_record(self, db, edge, speed, closed, expires):
        if not db.execute("SELECT 1 FROM edges WHERE id=?", (edge,)).fetchone():
            raise Error(404, "traffic edge missing")
        if (
            type(closed) is not bool
            or type(expires) not in (float, int)
            or not finite(expires)
        ):
            raise ValueError("invalid traffic state")
        if speed is not None:
            speed = positive(speed, 100)
        if not closed and speed is None:
            raise ValueError("traffic speed required unless closed")
        db.execute(
            "INSERT INTO traffic VALUES(?,?,?,?) ON CONFLICT(edge) DO UPDATE SET speed=excluded.speed,closed=excluded.closed,expires=excluded.expires",
            (edge, speed, int(closed), expires),
        )

    def traffic(self, records, ttl=300):
        self.writable()
        ttl = positive(ttl, 3600)
        if not isinstance(records, list) or not 1 <= len(records) <= 100:
            raise ValueError("traffic batch requires 1..100 edges")
        normalized = []
        for record in records:
            if not isinstance(record, dict) or set(record) - {
                "edge",
                "speed_mps",
                "closed",
            }:
                raise ValueError("invalid traffic record fields")
            value = dict(record)
            identifier(value.get("edge"))
            normalized.append(value)
        records = normalized
        with self.transaction(True) as db:
            meta = self.ready(db)
            now = self.now()
            for record in records:
                self._traffic_record(
                    db,
                    record["edge"],
                    record.get("speed_mps"),
                    record.get("closed", False),
                    now + ttl,
                )
            self.set_meta(
                db,
                revision=meta["revision"] + 1,
                traffic_version=meta["traffic_version"] + 1,
            )
            return {
                "revision": meta["revision"] + 1,
                "traffic_version": meta["traffic_version"] + 1,
            }

    def aggregate(self, min_users=2):
        self.writable()
        if type(min_users) is not int or not 2 <= min_users <= 100:
            raise ValueError("at least two distinct users required")
        with self.transaction(True) as db:
            meta = self.ready(db)
            now = self.now()
            by_edge = {}
            for row in db.execute(
                "SELECT * FROM samples WHERE received>? ORDER BY received,rowid",
                (now - 300,),
            ):
                by_edge.setdefault(row["edge"], {})[row["user"]] = row["speed"]
            records = []
            closed = {
                row[0]
                for row in db.execute(
                    "SELECT edge FROM traffic WHERE closed=1 AND expires>?", (now,)
                )
            }
            for edge, users in by_edge.items():
                if edge in closed:
                    continue
                if len(users) >= min_users:
                    speed = sum(users.values()) / len(users)
                    self._traffic_record(db, edge, speed, False, now + 300)
                    records.append(
                        {"edge": edge, "speed_mps": speed, "distinct_users": len(users)}
                    )
            if records:
                self.set_meta(
                    db,
                    revision=meta["revision"] + 1,
                    traffic_version=meta["traffic_version"] + 1,
                )
            return {
                "updated": records,
                "traffic_version": meta["traffic_version"] + int(bool(records)),
            }
