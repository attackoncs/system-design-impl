from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .geo import coordinates, distance, nearby_boxes
from .models import (
    Error as Error,
    MODES as MODES,
    dumps as dumps,
    identifier as identifier,
    positive as positive,
    finite,
)
from .cache import Cache as Cache
from .schema import SCHEMA
from .catalog import prepare_graph, prepare_snapshot, install_graph
from .telemetry import Telemetry
import os


class Store(Telemetry):
    def __init__(self, path, readonly=False, clock=time.time):
        if (
            not isinstance(path, (str, os.PathLike))
            or not str(path)
            or str(path) == ":memory:"
        ):
            raise ValueError("Store requires a persistent database file")
        if type(readonly) is not bool or not callable(clock):
            raise ValueError("invalid store configuration")
        self.path, self.readonly, self.clock = str(path), readonly, clock
        self.now()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.adjacency, self.tiles = Cache(64), Cache(128)
        with closing(self.connect()) as db:
            deadline = time.monotonic() + 15
            while True:
                try:
                    db.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as error:
                    if (
                        "locked" not in str(error).lower()
                        or time.monotonic() >= deadline
                    ):
                        raise
                    time.sleep(0.01)
            db.executescript(SCHEMA)
            columns = {row[1] for row in db.execute("PRAGMA table_info(edges)")}
            if "road_class" not in columns:
                try:
                    db.execute(
                        "ALTER TABLE edges ADD COLUMN road_class TEXT NOT NULL DEFAULT 'local'"
                    )
                except sqlite3.OperationalError:
                    if "road_class" not in {
                        row[1] for row in db.execute("PRAGMA table_info(edges)")
                    }:
                        raise
            for key, value in {
                "graph": str(uuid.uuid4()),
                "lineage": str(uuid.uuid4()),
                "revision": 0,
                "traffic_version": 0,
                "ready": False,
                "license": "unspecified",
                "graph_hash": "",
            }.items():
                db.execute(
                    "INSERT OR IGNORE INTO config VALUES(?,?)", (key, dumps(value))
                )
            meta = self.meta(db)
            if meta["ready"]:
                db.execute(
                    "INSERT OR IGNORE INTO graph_versions VALUES(?,?)",
                    (meta["graph"], meta["graph_hash"]),
                )
            db.commit()

    def now(self):
        value = self.clock()
        if not finite(value) or value < 0:
            raise ValueError("invalid server clock")
        return float(value)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=15000")
            return db
        except BaseException:
            db.close()
            raise

    @contextmanager
    def transaction(self, write=False):
        if type(write) is not bool:
            raise ValueError("write must be boolean")
        with closing(self.connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def meta(self, db):
        return {
            row[0]: json.loads(row[1])
            for row in db.execute("SELECT key,value FROM config")
        }

    def set_meta(self, db, **values):
        for key, value in values.items():
            db.execute("UPDATE config SET value=? WHERE key=?", (dumps(value), key))

    def writable(self):
        if self.readonly:
            raise Error(403, "replica is read only")

    def provision(self, user, token, admin=False):
        self.writable()
        identifier(user)
        if (
            not isinstance(token, str)
            or not 8 <= len(token) <= 4096
            or type(admin) is not bool
        ):
            raise ValueError("credential too short")
        with self.transaction(True) as db:
            db.execute(
                "INSERT INTO users VALUES(?,?,?)",
                (user, hashlib.sha256(token.encode()).hexdigest(), int(admin)),
            )

    def authenticate(self, token, admin=False):
        if (
            not isinstance(token, str)
            or not 8 <= len(token) <= 4096
            or type(admin) is not bool
        ):
            raise Error(403, "invalid credential or permission")
        with self.transaction() as db:
            row = db.execute(
                "SELECT id,admin FROM users WHERE token=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
            if not row or admin and not row["admin"]:
                raise Error(403, "invalid credential or permission")
            return row["id"]

    def _install_graph(self, db, graph=None, *, prepared=None):
        prepared = prepare_graph(graph) if prepared is None else prepared
        install_graph(db, prepared)
        self.set_meta(db, license=prepared["license"])

    def import_graph(self, graph, fail_hook=None):
        self.writable()
        if fail_hook is not None and not callable(fail_hook):
            raise ValueError("invalid failure hook")
        prepared = prepare_graph(graph)
        with self.transaction(True) as db:
            meta = self.meta(db)
            self._install_graph(db, prepared=prepared)
            if fail_hook is not None:
                fail_hook()
            self.set_meta(
                db,
                graph=str(uuid.uuid4()),
                revision=meta["revision"] + 1,
                traffic_version=0,
                ready=True,
                graph_hash=prepared["hash"],
            )
            db.execute(
                "INSERT INTO graph_versions VALUES(?,?)",
                (self.meta(db)["graph"], prepared["hash"]),
            )
            result = {
                **self.meta(db),
                "role": "primary",
                "nodes": len(prepared["nodes"]),
                "edges": len(prepared["edges"]),
            }
        return result

    def health(self):
        with self.transaction() as db:
            return {
                **self.meta(db),
                "role": "replica" if self.readonly else "primary",
                "nodes": db.execute("SELECT count(*) FROM nodes").fetchone()[0],
                "edges": db.execute("SELECT count(*) FROM edges").fetchone()[0],
            }

    def ready(self, db):
        meta = self.meta(db)
        if not meta["ready"]:
            raise Error(503, "no road catalog installed")
        return meta

    def graph_document(self, db):
        return {
            "nodes": [
                {"id": r["id"], "latitude": r["lat"], "longitude": r["lon"]}
                for r in db.execute("SELECT * FROM nodes ORDER BY id")
            ],
            "edges": [
                {
                    "id": r["id"],
                    "source": r["source"],
                    "target": r["target"],
                    "length_m": r["length"],
                    "speeds": json.loads(r["speeds"]),
                    "toll": bool(r["toll"]),
                    **(
                        {"road_class": r["road_class"]}
                        if r["road_class"] != "local"
                        else {}
                    ),
                }
                for r in db.execute("SELECT * FROM edges ORDER BY id")
            ],
            "addresses": dict(
                db.execute("SELECT name,node FROM addresses ORDER BY name")
            ),
            "license": self.meta(db)["license"],
        }

    def export(self):
        with self.transaction() as db:
            meta = self.ready(db)
            return {
                "meta": meta,
                "graph": self.graph_document(db),
                "traffic": [
                    dict(row)
                    for row in db.execute("SELECT * FROM traffic ORDER BY edge")
                ],
            }

    def install_snapshot(self, snapshot):
        if not self.readonly:
            raise Error(403, "snapshot import requires replica")
        incoming, prepared, traffic = prepare_snapshot(snapshot)
        with self.transaction(True) as db:
            old = self.meta(db)
            if old["ready"]:
                if (
                    old["lineage"] != incoming["lineage"]
                    or incoming["revision"] < old["revision"]
                ):
                    raise Error(409, "foreign lineage or catalog rollback")
                if (
                    old["graph"] == incoming["graph"]
                    and incoming["traffic_version"] < old["traffic_version"]
                ):
                    raise Error(409, "traffic version rollback")
                if incoming["revision"] == old["revision"]:
                    current_traffic = [
                        dict(row)
                        for row in db.execute("SELECT * FROM traffic ORDER BY edge")
                    ]
                    if incoming != old or traffic != current_traffic:
                        raise Error(409, "catalog revision contains different content")
                    return False
            known = db.execute(
                "SELECT hash FROM graph_versions WHERE graph=?", (incoming["graph"],)
            ).fetchone()
            if known and known[0] != prepared["hash"]:
                raise Error(409, "immutable graph UUID contains different content")
            self._install_graph(db, prepared=prepared)
            for record in traffic:
                self._traffic_record(
                    db,
                    record["edge"],
                    record["speed"],
                    bool(record["closed"]),
                    record["expires"],
                )
            self.set_meta(db, **incoming)
            db.execute(
                "INSERT OR IGNORE INTO graph_versions VALUES(?,?)",
                (incoming["graph"], prepared["hash"]),
            )
            return True

    def resolve(self, db, value, max_snap=1000):
        max_snap = positive(max_snap, 100000)
        if isinstance(value, str):
            row = db.execute(
                "SELECT id,lat,lon,tx,ty FROM nodes WHERE id=?", (value,)
            ).fetchone()
            if not row:
                row = db.execute(
                    "SELECT n.* FROM addresses a JOIN nodes n ON n.id=a.node WHERE a.name=?",
                    (value.strip().casefold(),),
                ).fetchone()
            if not row:
                raise Error(404, "node/address not found in fixture")
            return dict(row)
        if not isinstance(value, dict) or set(value) != {"latitude", "longitude"}:
            raise ValueError("coordinate object required")
        point = coordinates(value["latitude"], value["longitude"])
        choices = {}
        for west, east, south, north in nearby_boxes(*point, max_snap):
            for row in db.execute(
                "SELECT * FROM nodes WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? LIMIT 10001",
                (south, north, west, east),
            ):
                choices[row["id"]] = dict(row)
            if len(choices) > 10000:
                raise Error(413, "snap candidate budget exceeded")
        ranked = sorted(
            (distance(point, (row["lat"], row["lon"])), name, row)
            for name, row in choices.items()
        )
        if not ranked or ranked[0][0] > max_snap + 1e-6:
            raise Error(404, "no road node within snap limit")
        return ranked[0][2]

    def geocode(self, value, reverse=False):
        if type(reverse) is not bool:
            raise ValueError("reverse must be boolean")
        with self.transaction() as db:
            meta = self.ready(db)
            row = self.resolve(db, value)
            names = [
                r[0]
                for r in db.execute(
                    "SELECT name FROM addresses WHERE node=? ORDER BY name",
                    (row["id"],),
                )
            ]
            return {
                "graph": meta["graph"],
                "node": row["id"],
                "latitude": row["lat"],
                "longitude": row["lon"],
                "addresses": names,
                "method": "nearest fixture node" if reverse else "exact fixture lookup",
            }

    def outgoing(self, db, node, graph, loaded):
        tx, ty = node["tx"], node["ty"]
        loaded.add((tx, ty))

        def load():
            adjacency = {}
            for row in db.execute(
                "SELECT e.* FROM edges e JOIN nodes n ON n.id=e.source WHERE n.tx=? AND n.ty=? ORDER BY e.id",
                (tx, ty),
            ):
                record = dict(row)
                record["speeds"] = json.loads(record["speeds"])
                adjacency.setdefault(record["source"], []).append(record)
            # Cached immutable text avoids walking nested edge/speed dictionaries
            # on every node expansion. Each consumer parses its own edge values.
            return {source: dumps(edges) for source, edges in adjacency.items()}

        body = self.adjacency.get_item((graph, tx, ty), node["id"], load)
        return json.loads(body) if body else []

    def spatial_edges(self, db, west, east, south, north, *, limit=None):
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 500001):
            raise ValueError("invalid spatial result limit")
        return db.execute(
            """SELECT DISTINCT e.*,a.lat AS alat,a.lon AS alon,b.lat AS blat,b.lon AS blon
          FROM spatial s JOIN edge_boxes eb ON eb.id=s.id JOIN edges e ON e.id=eb.edge
          JOIN nodes a ON a.id=e.source JOIN nodes b ON b.id=e.target
          WHERE s.west<=? AND s.east>=? AND s.south<=? AND s.north>=?"""
            + (" LIMIT ?" if limit is not None else ""),
            (east, west, north, south, limit)
            if limit is not None
            else (east, west, north, south),
        ).fetchall()
