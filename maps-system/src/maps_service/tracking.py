"""Private, expiring navigation sessions and coalesced adaptive route events."""

from contextlib import contextmanager
import json
import logging
import math
import threading
import time
import uuid

from .geo import boxes, cell, unproject
from .navigation import route
from .store import Error, dumps, identifier

_LOG = logging.getLogger(__name__)
_MAX_RESULT = 2 * 1024 * 1024


def _seconds(value, maximum):
    try:
        valid = (
            type(value) in (int, float)
            and math.isfinite(value)
            and 0 < value <= maximum
        )
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("invalid navigation duration")
    return float(value)


def _cover(result):
    """One conservative XYZ ancestor of all candidate routes, including detours."""
    points = [
        point
        for candidate in result.get("routes", [])
        for point in candidate["geometry"]
    ]
    tiles = [cell(lat, lon, 16) for lon, lat in points]
    if not tiles:
        return 0, 0, 0
    for z in range(16, -1, -1):
        parents = {(x >> (16 - z), y >> (16 - z)) for x, y in tiles}
        if len(parents) == 1:
            x, y = parents.pop()
            return z, x, y
    return 0, 0, 0


def _intersects(row, changes):
    z, x, y = row["z"], row["x"], row["y"]
    n = 2**z
    north, west = unproject(x / n, y / n)
    south, east = unproject((x + 1) / n, (y + 1) / n)
    north = 90 if y == 0 else north
    south = -90 if y == n - 1 else south
    return any(
        w <= east and e >= west and s <= north and h >= south for w, e, s, h in changes
    )


class NavigationTracker:
    """Recompute affected routes, with periodic full refresh for newly faster paths.

    The spatial ancestor is an invalidation prefilter, never a shortest-path
    shortcut. Events retain the newest state only; reconnecting clients use the
    revision as Last-Event-ID and replace their displayed route.
    """

    def __init__(self, store, *, interval=1, refresh_interval=30, max_sessions=128):
        if store.readonly:
            raise ValueError("navigation tracking requires a primary")
        self.interval = _seconds(interval, 30)
        self.refresh_interval = _seconds(refresh_interval, 300)
        if type(max_sessions) is not int or not 1 <= max_sessions <= 1024:
            raise ValueError("max_sessions must be 1..1024")
        self.store, self.max_sessions = store, max_sessions
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name="maps-navigation")
        self.last_error = None
        self._condition = threading.Condition()
        self._local = threading.local()
        self._active, self._closing, self._closed = 0, False, False
        self._refresh_lock = threading.Lock()
        self._previous = None
        with store.transaction(True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS navigation_sessions(
                id TEXT PRIMARY KEY,user TEXT NOT NULL,options TEXT NOT NULL,
                result TEXT NOT NULL,revision INTEGER NOT NULL,expires REAL NOT NULL,
                updated REAL NOT NULL,next_refresh REAL NOT NULL,
                z INTEGER NOT NULL,x INTEGER NOT NULL,y INTEGER NOT NULL)""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS navigation_expiry ON navigation_sessions(expires)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS navigation_owner ON navigation_sessions(user,id)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS navigation_area ON navigation_sessions(z,x,y)"
            )

    def _now(self):
        now = self.store.clock()
        try:
            valid = type(now) in (int, float) and math.isfinite(now) and now >= 0
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError("invalid navigation clock")
        return now

    @contextmanager
    def _operation(self):
        with self._condition:
            if self._closing:
                raise Error(503, "navigation tracker is closing")
            self._active += 1
            self._local.depth = getattr(self._local, "depth", 0) + 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._local.depth -= 1
                self._condition.notify_all()

    @staticmethod
    def _encoded(result):
        encoded = dumps(result)
        if len(encoded.encode()) > _MAX_RESULT:
            raise Error(413, "navigation route too large; reduce search options")
        return encoded

    def _lookup(self, actor, session):
        identifier(actor)
        identifier(session)
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM navigation_sessions WHERE id=? AND user=? AND expires>?",
                (session, actor, self._now()),
            ).fetchone()
            if row is None:
                raise Error(404, "navigation session not found")
            return dict(row)

    def _response(self, row):
        return {
            "id": row["id"],
            "revision": row["revision"],
            "expires_at": row["expires"],
            "updated_at": row["updated"],
            "route": json.loads(row["result"]),
            "refresh_error": self.last_error,
        }

    def _notify(self):
        with self._condition:
            self._condition.notify_all()

    def create(self, actor, body):
        identifier(actor)
        with self._operation():
            if (
                not isinstance(body, dict)
                or set(body)
                - {
                    "origin",
                    "destination",
                    "mode",
                    "avoid_tolls",
                    "alternatives",
                    "max_nodes",
                    "ttl",
                }
                or not {"origin", "destination"} <= set(body)
            ):
                raise ValueError("invalid navigation fields")
            ttl = _seconds(body.get("ttl", 900), 3600)
            options = json.loads(
                dumps({key: value for key, value in body.items() if key != "ttl"})
            )
            result = route(self.store, **options)
            encoded, session = self._encoded(result), uuid.uuid4().hex
            z, x, y = _cover(result)
            with self.store.transaction(True) as db:
                now = self._now()
                if not db.execute(
                    "SELECT 1 FROM users WHERE id=?", (actor,)
                ).fetchone():
                    raise Error(401, "unknown user")
                db.execute("DELETE FROM navigation_sessions WHERE expires<=?", (now,))
                if (
                    db.execute("SELECT count(*) FROM navigation_sessions").fetchone()[0]
                    >= self.max_sessions
                ):
                    raise Error(503, "navigation session capacity reached")
                db.execute(
                    "INSERT INTO navigation_sessions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        session,
                        actor,
                        dumps(options),
                        encoded,
                        1,
                        now + ttl,
                        now,
                        now + self.refresh_interval,
                        z,
                        x,
                        y,
                    ),
                )
            self._notify()
            return self._response(self._lookup(actor, session))

    def read(self, actor, session):
        with self._operation():
            return self._response(self._lookup(actor, session))

    def move(self, actor, session, origin):
        with self._operation():
            row = self._lookup(actor, session)
            options = json.loads(row["options"])
            options["origin"] = json.loads(dumps(origin))
            result = route(self.store, **options)
            encoded, (z, x, y) = self._encoded(result), _cover(result)
            with self.store.transaction(True) as db:
                now = self._now()
                changed = db.execute(
                    """UPDATE navigation_sessions SET options=?,result=?,
                    revision=revision+1,updated=?,next_refresh=?,z=?,x=?,y=?
                    WHERE id=? AND user=? AND revision=? AND expires>?""",
                    (
                        dumps(options),
                        encoded,
                        now,
                        now + self.refresh_interval,
                        z,
                        x,
                        y,
                        session,
                        actor,
                        row["revision"],
                        now,
                    ),
                ).rowcount
                if not changed:
                    raise Error(409, "navigation session changed; retry")
            self._notify()
            return self._response(self._lookup(actor, session))

    def remove(self, actor, session):
        with self._operation():
            self._lookup(actor, session)
            with self.store.transaction(True) as db:
                db.execute(
                    "DELETE FROM navigation_sessions WHERE id=? AND user=?",
                    (session, actor),
                )
            self._notify()
            return {"id": session, "removed": True}

    def events(self, actor, session, after=0, wait=5, cancel=None):
        if type(after) is not int or not 0 <= after < 2**63:
            raise ValueError("invalid event cursor")
        try:
            valid = (
                type(wait) in (int, float) and math.isfinite(wait) and 0 <= wait <= 5
            )
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError("event wait must be 0..5 seconds")
        deadline = time.monotonic() + wait
        with self._operation():
            while True:
                row = self._lookup(actor, session)
                if after > row["revision"]:
                    raise Error(409, "event cursor is ahead of navigation session")
                if row["revision"] > after:
                    return [
                        {
                            "id": row["revision"],
                            "event": "route",
                            "data": self._response(row),
                        }
                    ]
                remaining = deadline - time.monotonic()
                if (
                    remaining <= 0
                    or self.stop.is_set()
                    or (cancel is not None and cancel.is_set())
                ):
                    return []
                with self._condition:
                    self._condition.wait(min(0.1, remaining))

    def _state(self):
        with self.store.transaction() as db:
            meta = self.store.ready(db)
            active = {
                row["edge"]: tuple(row)
                for row in db.execute(
                    "SELECT edge,speed,closed,expires FROM traffic WHERE expires>? ORDER BY edge",
                    (self._now(),),
                )
            }
            return meta, active

    def _changed_boxes(self, edges):
        output = []
        with self.store.transaction() as db:
            for offset in range(0, len(edges), 256):
                batch = edges[offset : offset + 256]
                marks = ",".join("?" for _ in batch)
                for row in db.execute(
                    """SELECT a.lat,a.lon,b.lat,b.lon FROM edges e
                    JOIN nodes a ON a.id=e.source JOIN nodes b ON b.id=e.target
                    WHERE e.id IN ("""
                    + marks
                    + ")",
                    batch,
                ):
                    output.extend(boxes((row[0], row[1]), (row[2], row[3])))
        return output

    def run_once(self):
        with self._operation(), self._refresh_lock:
            meta, traffic = self._state()
            previous = self._previous
            force = previous is None or previous[0] != meta["graph"]
            old = previous[1] if previous is not None else {}
            changed = sorted(
                edge
                for edge in old.keys() | traffic.keys()
                if old.get(edge) != traffic.get(edge)
            )
            force = force or len(changed) > 1024
            affected = [] if force or not changed else self._changed_boxes(changed)
            with self.store.transaction(True) as db:
                now = self._now()
                db.execute("DELETE FROM navigation_sessions WHERE expires<=?", (now,))
                rows = [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM navigation_sessions ORDER BY id"
                    )
                ]
            refreshed = 0
            for row in rows:
                if self.stop.is_set():
                    break
                if (
                    not force
                    and row["next_refresh"] > now
                    and not _intersects(row, affected)
                ):
                    continue
                try:
                    result = route(self.store, **json.loads(row["options"]))
                    encoded = self._encoded(result)
                    z, x, y = _cover(result)
                except Error as error:
                    encoded = self._encoded(
                        {
                            "routes": [],
                            "graph": meta["graph"],
                            "traffic_version": meta["traffic_version"],
                            "error": {"status": error.status, "message": error.message},
                        }
                    )
                    z, x, y = row["z"], row["x"], row["y"]
                with self.store.transaction(True) as db:
                    accepted = self._now()
                    changed_row = db.execute(
                        """UPDATE navigation_sessions SET result=?,
                        revision=revision+?,updated=?,next_refresh=?,z=?,x=?,y=?
                        WHERE id=? AND revision=? AND expires>?""",
                        (
                            encoded,
                            int(encoded != row["result"]),
                            accepted,
                            accepted + self.refresh_interval,
                            z,
                            x,
                            y,
                            row["id"],
                            row["revision"],
                            accepted,
                        ),
                    ).rowcount
                refreshed += changed_row
            self._previous = meta["graph"], traffic
            self.last_error = None
            self._notify()
            return refreshed

    def run(self):
        while not self.stop.is_set():
            try:
                self.run_once()
            except Exception:
                self.last_error = "navigation refresh unavailable"
                _LOG.warning(self.last_error)
            self.stop.wait(self.interval)

    def start(self):
        if self.stop.is_set():
            raise RuntimeError("navigation tracker is stopped")
        self.thread.start()
        return self

    def close(self):
        if (
            getattr(self._local, "depth", 0)
            or self.thread.ident == threading.get_ident()
        ):
            raise RuntimeError("navigation tracker cannot close from its own operation")
        with self._condition:
            if self._closing:
                self._condition.wait_for(lambda: self._closed)
                return
            self._closing = True
        self.stop.set()
        self._notify()
        if self.thread.ident is not None:
            self.thread.join()
        with self._condition:
            self._condition.wait_for(lambda: self._active == 0)
            self._closed = True
            self._condition.notify_all()
