import concurrent.futures
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import random
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tools")]
from proximity.geo import EARTH, bounds, distance, encode
from proximity.store import Cache, Error, Store, canonical
from proximity.http import Replicator
from lab import Cluster, request


def business(lat=0, lon=0, name="Cafe", category="cafe", hours=None):
    return {
        "name": name,
        "latitude": lat,
        "longitude": lon,
        "category": category,
        "hours": hours or [],
    }


def destination(lat, lon, meters, bearing):
    p, l, b, a = map(math.radians, (lat, lon, bearing, 0))
    a = meters / EARTH
    q = math.asin(math.sin(p) * math.cos(a) + math.cos(p) * math.sin(a) * math.cos(b))
    m = l + math.atan2(
        math.sin(b) * math.sin(a) * math.cos(p), math.cos(a) - math.sin(p) * math.sin(q)
    )
    return math.degrees(q), (math.degrees(m) + 180) % 360 - 180


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "data.sqlite"
        self.now = [datetime(2026, 10, 13, 12, tzinfo=timezone.utc).timestamp()]
        self.store = Store(self.path, clock=lambda: self.now[0])

    def tearDown(self):
        self.temp.cleanup()

    def add(self, body=None, key="add"):
        return self.store.mutate("alice", "POST", None, body or business(), key)["id"]

    def test_known_geohash_and_bounds(self):
        self.assertEqual(encode(42.6, -5.6, 5), "ezs42")
        south, north, west, east = bounds("ezs42")
        self.assertTrue(south <= 42.6 <= north and west <= -5.6 <= east)

    def test_validation(self):
        for lat, lon, radius in [
            (91, 0, 500),
            (0, 181, 500),
            (float("nan"), 0, 500),
            (0, 0, 501),
            (True, 0, 500),
        ]:
            with self.assertRaises((ValueError, TypeError)):
                self.store.search(lat, lon, radius)
        for body in [
            business(hours=[{"day": 0, "start": 100, "end": 99}]),
            business(name=""),
            dict(business(), unknown=1),
        ]:
            with self.assertRaises(ValueError):
                self.add(body)

    def test_next_day_restart_and_delayed_deletion(self):
        identifier = self.add()
        self.assertEqual(self.store.search(0, 0)["total"], 0)
        self.now[0] += 86400
        self.assertEqual(self.store.search(0, 0)["total"], 1)
        self.store.mutate(
            "alice", "PUT", identifier, business(10, 10, name="Moved"), "move"
        )
        self.assertEqual(self.store.detail(identifier)["business"]["name"], "Cafe")
        self.now[0] += 86400
        self.assertEqual(self.store.search(0, 0)["total"], 0)
        self.store = Store(self.path, clock=lambda: self.now[0])
        self.assertEqual(self.store.detail(identifier)["business"]["name"], "Moved")
        self.store.mutate("alice", "DELETE", identifier, None, "delete")
        self.assertEqual(self.store.search(10, 10)["total"], 1)
        self.now[0] += 86400
        with self.assertRaises(Error) as caught:
            self.store.detail(identifier)
        self.assertEqual(caught.exception.status, 404)

    def test_replay_conflict_ownership_and_expiry(self):
        identifier = self.add()
        self.store = Store(self.path, clock=lambda: self.now[0])
        self.assertEqual(self.add(), identifier)
        with self.assertRaises(Error) as caught:
            self.add(business(name="Different"))
        self.assertEqual(caught.exception.status, 409)
        with self.assertRaises(Error) as caught:
            self.store.mutate("bob", "DELETE", identifier, None, "x")
        self.assertEqual(caught.exception.status, 403)
        self.now[0] += 8 * 86400
        self.assertNotEqual(self.add(), identifier)

    def test_concurrent_idempotency(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            ids = list(executor.map(lambda _: self.add(), range(16)))
        self.assertEqual(len(set(ids)), 1)

    def test_first_write_after_midnight_freezes_previous_day(self):
        yesterday = self.add()
        self.now[0] += 86400
        today = self.add(business(name="Today"), "today")
        self.assertEqual(
            {r["id"] for r in self.store.search(0, 0)["businesses"]}, {yesterday}
        )
        self.now[0] += 86400
        self.assertEqual(
            {r["id"] for r in self.store.search(0, 0)["businesses"]}, {yesterday, today}
        )

    def test_shard_configuration_survives_restart(self):
        self.add()
        self.store.publish(force=True)
        restarted = Store(self.path, shards=17, clock=lambda: self.now[0])
        self.assertEqual(restarted.shards, 4)
        self.assertEqual(restarted.search(0, 0)["total"], 1)

    def test_readers_never_mix_snapshot_versions(self):
        ids = [self.add(business(name="A"), str(i)) for i in range(20)]
        self.store.publish(force=True)
        begin = threading.Event()

        def writer():
            begin.wait()
            for iteration in range(8):
                with self.store.transaction(True) as db:
                    db.execute(
                        "UPDATE drafts SET body=?",
                        (
                            canonical(
                                dict(
                                    business(name="A" if iteration % 2 else "B"),
                                    address="",
                                )
                            ),
                        ),
                    )
                self.store.publish(force=True)

        def reader():
            begin.wait()
            for _ in range(30):
                result = self.store.search(0, 0)
                self.assertEqual(result["total"], len(ids))
                self.assertEqual(len({r["name"] for r in result["businesses"]}), 1)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(writer),
                *[executor.submit(reader) for _ in range(3)],
            ]
            begin.set()
            for future in futures:
                future.result()

    def test_candidates_cache_uses_cells_and_does_not_persist_queries(self):
        self.add()
        self.store.publish(force=True)
        for i in range(100):
            self.store.search(0.000001 + i * 1e-8, 0.000001, 500)
        self.assertLess(len(self.store.candidates.items), 20)
        self.assertGreater(self.store.candidates.hits, 100)
        with self.store.transaction() as db:
            tables = {
                r[0]
                for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            self.assertEqual(tables, {"meta", "drafts", "public", "geo", "replay"})

    def test_publication_rollback(self):
        identifier = self.add()
        self.store.publish(force=True)
        old = self.store.health()["version"]
        self.store.mutate("alice", "PUT", identifier, business(1, 1), "move")
        with self.assertRaises(RuntimeError):
            self.store.publish(
                force=True,
                fail_hook=lambda: (_ for _ in ()).throw(RuntimeError("injected")),
            )
        self.assertEqual(self.store.health()["version"], old)
        self.assertEqual(self.store.search(0, 0)["total"], 1)

    def test_spatial_oracle_borders_poles_and_radius(self):
        rng = random.Random(17)
        centers = [
            (0, 0),
            (31.23, 121.47),
            (0, 179.999),
            (0, -179.999),
            (89.999, 45),
            (-89.999, -100),
            (90, 180),
            (-90, -180),
            (60, 0),
        ]
        cases = []
        points = []
        for lat, lon in centers:
            for radius in [500, 1000, 2000, 5000, 20000]:
                cases.append((lat, lon, radius))
                for meters in [0, radius - 0.01, radius, radius + 0.01, radius * 1.4]:
                    for bearing in [0, 45, 90, 180, 270, rng.uniform(0, 360)]:
                        points.append(destination(lat, lon, meters, bearing))
        with self.store.transaction(True) as db:
            rows = [
                dict(business(*point), id=str(i), address="")
                for i, point in enumerate(points)
            ]
            # Load drafts directly for a reproducible geometry fixture, then use real publication.
            db.executemany(
                "INSERT INTO drafts VALUES(?,?,?,0)",
                [
                    (
                        r["id"],
                        "alice",
                        canonical({k: v for k, v in r.items() if k != "id"}),
                    )
                    for r in rows
                ],
            )
        self.store.publish(force=True)
        for lat, lon, radius in cases:
            expected = {
                str(i)
                for i, (p, q) in enumerate(points)
                if distance(lat, lon, p, q) <= radius + 1e-6
            }
            result = self.store.search(lat, lon, radius, limit=1000)
            actual = {r["id"] for r in result["businesses"]}
            self.assertEqual(actual, expected, (lat, lon, radius))
            self.assertLessEqual(result["cells"], 4096)

    def test_random_global_oracle(self):
        rng = random.Random(42)
        rows = [
            dict(
                business(rng.uniform(-90, 90), rng.uniform(-180, 180)),
                id=str(i),
                address="",
            )
            for i in range(400)
        ]
        with self.store.transaction(True) as db:
            self.store._install(db, rows)
        for row in rows[:30]:
            result = self.store.search(row["latitude"], row["longitude"], 20000)
            expected = {
                r["id"]
                for r in rows
                if distance(
                    row["latitude"], row["longitude"], r["latitude"], r["longitude"]
                )
                <= 20000 + 1e-6
            }
            self.assertEqual({r["id"] for r in result["businesses"]}, expected)

    def test_cache_ttl_capacity_and_version_switch(self):
        tick = [0]
        cache = Cache(ttl=1, capacity=2, clock=lambda: tick[0])
        self.assertEqual(cache.get("a", lambda: 1), 1)
        self.assertEqual(cache.get("a", lambda: 2), 1)
        tick[0] = 2
        self.assertEqual(cache.get("a", lambda: 2), 2)
        cache.get("b", lambda: 3)
        cache.get("c", lambda: 4)
        self.assertEqual(len(cache.items), 2)
        identifier = self.add()
        self.store.publish(force=True)
        self.store.search(0, 0)
        self.store.detail(identifier)
        self.store.mutate("alice", "PUT", identifier, business(1, 1), "move")
        self.store.publish(force=True)
        self.assertEqual(self.store.search(0, 0)["total"], 0)
        self.assertEqual(self.store.detail(identifier)["business"]["latitude"], 1)

    def test_filters_pagination_and_cursor_conflict(self):
        hours = [{"day": 1, "start": 600, "end": 720}]
        for i in range(3):
            self.add(business(0, i * 0.0001, hours=hours), str(i))
        self.store.publish(force=True)
        first = self.store.search(
            0, 0, category="cafe", open_at="2026-10-13T10:00:00Z", limit=1
        )
        second = self.store.search(
            0,
            0,
            category="cafe",
            open_at="2026-10-13T10:00:00Z",
            limit=1,
            cursor=first["next_cursor"],
        )
        self.assertNotEqual(first["businesses"][0]["id"], second["businesses"][0]["id"])
        self.assertEqual(
            self.store.search(0, 0, open_at="2026-10-13T12:00:00Z")["total"], 0
        )
        self.assertEqual(self.store.search(0, 0, category="hotel")["total"], 0)
        self.store.publish(force=True)
        with self.assertRaises(Error) as caught:
            self.store.search(
                0,
                0,
                category="cafe",
                open_at="2026-10-13T10:00:00Z",
                limit=1,
                cursor=first["next_cursor"],
            )
        self.assertEqual(caught.exception.status, 409)

    def test_replica_monotonicity_readonly_and_atomic_import(self):
        replica = Store(Path(self.temp.name) / "replica.sqlite", readonly=True)
        with self.assertRaises(Error):
            replica.search(0, 0)
        self.add()
        self.store.publish(force=True)
        snapshot = self.store.export()
        replica.import_snapshot(snapshot)
        self.assertEqual(replica.search(0, 0)["total"], 1)
        with self.assertRaises(Error):
            replica.mutate("alice", "POST", None, business(), "x")
        for change in [
            {"version": 0},
            {"lineage": "00000000-0000-0000-0000-000000000000"},
        ]:
            with self.assertRaises(Error):
                replica.import_snapshot(dict(snapshot, **change))
        bad = dict(snapshot, version=2, businesses=snapshot["businesses"] * 2)
        with self.assertRaises(sqlite3.IntegrityError):
            replica.import_snapshot(bad)
        self.assertEqual(replica.health()["version"], 1)
        self.assertEqual(replica.search(0, 0)["total"], 1)

    def test_replication_poll_recovers_from_database_failure(self):
        self.add()
        self.store.publish(force=True)
        replica = Store(Path(self.temp.name) / "poll.sqlite", readonly=True)
        replicator = Replicator(replica, "http://unused.test", "test", interval=0)
        original = replica.import_snapshot
        calls = []

        def import_with_fault(snapshot):
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("injected transient failure")
            result = original(snapshot)
            replicator.stop.set()
            return result

        # Only this fault-injection unit test replaces transport; NetworkTests uses real HTTP.
        with (
            patch("proximity.http.fetch", return_value=self.store.export()),
            patch.object(replica, "import_snapshot", side_effect=import_with_fault),
        ):
            replicator.run()
        self.assertEqual(len(calls), 2)
        self.assertIsNone(replicator.last_error)
        self.assertEqual(replica.search(0, 0)["total"], 1)


class NetworkTests(unittest.TestCase):
    def test_real_process_replication_failover_restart_and_http_errors(self):
        with Cluster() as cluster:
            primary = cluster.start("primary", "primary")

            def status(path, code, **kwargs):
                with self.assertRaises(HTTPError) as caught:
                    request(primary, path, **kwargs)
                self.assertEqual(caught.exception.code, code)

            status("/v1/businesses", 401, method="POST", body=business())
            status("/v1/businesses", 403, method="POST", body=business(), token="wrong")
            status(
                "/v1/businesses",
                400,
                method="POST",
                body=business(),
                token="alice-demo",
            )
            status("/v1/search/nearby?latitude=nan&longitude=0", 400)
            status("/v1/search/nearby?latitude=0&latitude=1&longitude=0", 400)
            status("/internal/snapshot", 401)
            response = request(
                primary,
                "/v1/businesses",
                "POST",
                business(0, 179.999),
                "alice-demo",
                "create",
            )
            identifier = response["id"]
            status(
                "/v1/businesses/" + identifier,
                403,
                method="DELETE",
                token="bob-demo",
                key="bad",
            )
            self.assertEqual(
                request(
                    primary,
                    "/v1/search/nearby?latitude=0&longitude=-179.999&radius=500",
                )["total"],
                0,
            )
            request(primary, "/internal/publish", "POST", token="admin-demo")
            replicas = [
                cluster.start(
                    name, "replica", ["--upstream", primary, "--interval", "0.1"]
                )
                for name in ["r1", "r2"]
            ]
            for replica in replicas:
                cluster.wait(
                    lambda replica=replica: request(replica, "/health")["version"] == 1
                )
                self.assertEqual(
                    request(
                        replica,
                        "/v1/search/nearby?latitude=0&longitude=-179.999&radius=500",
                    )["total"],
                    1,
                )
            regions = cluster.root / "regions.json"
            regions.write_text(
                json.dumps({"local": replicas, "isolated": ["http://127.0.0.1:1"]}),
                encoding="utf-8",
            )
            gateway = cluster.start("gateway", "gateway", ["--regions", str(regions)])
            cluster.stop("r1")
            path = "/v1/search/nearby?latitude=0&longitude=-179.999&radius=500"
            self.assertEqual(request(gateway, path, region="local")["total"], 1)
            for region in ["isolated", "unknown"]:
                with self.assertRaises(HTTPError) as caught:
                    request(gateway, path, region=region)
                self.assertEqual(
                    caught.exception.code, 503 if region == "isolated" else 400
                )
            request(
                primary,
                "/v1/businesses/" + identifier,
                "PUT",
                business(1, 1),
                "alice-demo",
                "move",
            )
            request(primary, "/internal/publish", "POST", token="admin-demo")
            cluster.wait(lambda: request(replicas[1], "/health")["version"] == 2)
            self.assertEqual(request(gateway, path, region="local")["total"], 0)
            cluster.stop("primary")
            self.assertEqual(
                request(replicas[1], "/v1/search/nearby?latitude=1&longitude=1")[
                    "total"
                ],
                1,
            )
            cluster.stop("r2")
            restarted = cluster.start(
                "r2", "replica", ["--upstream", primary, "--interval", "0.1"]
            )
            self.assertEqual(
                request(restarted, "/v1/search/nearby?latitude=1&longitude=1")["total"],
                1,
            )
            restarted_primary = cluster.start("primary", "primary")
            replay = request(
                restarted_primary,
                "/v1/businesses",
                "POST",
                business(0, 179.999),
                "alice-demo",
                "create",
            )
            self.assertEqual(replay, response)
            with self.assertRaises(HTTPError) as caught:
                request(
                    restarted,
                    "/v1/businesses",
                    "POST",
                    business(),
                    "alice-demo",
                    "replica-write",
                )
            self.assertEqual(caught.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
