"""Catalog immutability, transactional telemetry and cache overlap regressions."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import maps_service.store as store_module
from maps_service.catalog import prepare_graph
from maps_service.navigation import route
from maps_service.store import Cache, Error, Store, dumps


class StoreReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "maps.sqlite"
        self.now = [2000000000.0]
        self.store = Store(self.path, clock=lambda: self.now[0])
        self.graph = json.loads((ROOT / "examples/roads.json").read_text())
        self.store.import_graph(self.graph)
        for user in ("alice", "bob"):
            self.store.provision(user, user + "-private")

    def replica(self):
        return Store(
            Path(self.temp.name) / "replica.sqlite",
            readonly=True,
            clock=lambda: self.now[0],
        )

    def report(self, name="p1", sequence=1, **changes):
        return {
            "event_id": name,
            "sequence": sequence,
            "timestamp": self.now[0],
            "latitude": 0,
            "longitude": -0.0018,
            "heading": 90,
            **changes,
        }

    def test_same_revision_cannot_change_graph_traffic_or_metadata(self):
        self.store.traffic([{"edge": "ab", "speed_mps": 2}])
        snapshot = self.store.export()
        replica = self.replica()
        self.assertTrue(replica.install_snapshot(snapshot))
        for kind in ("graph", "traffic", "metadata"):
            changed = deepcopy(snapshot)
            if kind == "graph":
                changed["graph"]["nodes"][0]["longitude"] -= 0.001
                changed["meta"]["graph_hash"] = prepare_graph(changed["graph"])["hash"]
            elif kind == "traffic":
                changed["traffic"][0]["speed"] = 9
            else:
                changed["meta"]["traffic_version"] += 1
            with self.subTest(kind=kind), self.assertRaises(Error):
                replica.install_snapshot(changed)
            self.assertEqual(replica.export(), snapshot)

    def test_expiry_does_not_change_same_revision_snapshot_payload(self):
        self.store.traffic([{"edge": "ab", "speed_mps": 2}], ttl=1)
        snapshot = self.store.export()
        replica = self.replica()
        replica.install_snapshot(snapshot)
        self.now[0] += 2
        self.assertEqual(self.store.export(), snapshot)
        self.assertFalse(replica.install_snapshot(self.store.export()))

    def test_reused_graph_uuid_is_checked_across_restart_and_intermediate_graph(self):
        replica = self.replica()
        first = self.store.export()
        replica.install_snapshot(first)
        self.store.import_graph(self.graph)
        replica.install_snapshot(self.store.export())
        replica = self.replica()
        changed = deepcopy(first)
        changed["meta"]["revision"] += 10
        changed["graph"]["edges"][0]["length_m"] *= 2
        changed["meta"]["graph_hash"] = prepare_graph(changed["graph"])["hash"]
        with self.assertRaises(Error):
            replica.install_snapshot(changed)

    def test_snapshot_validation_is_atomic_and_does_not_coerce_closed_flags(self):
        self.store.traffic([{"edge": "ab", "speed_mps": 2}])
        snapshot = self.store.export()
        replica = self.replica()
        for change in (
            {"closed": "false"},
            {"closed": 2},
            {"expires": True},
            {"speed": float("inf")},
        ):
            bad = deepcopy(snapshot)
            bad["traffic"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                replica.install_snapshot(bad)
            self.assertFalse(replica.health()["ready"])
        bad = deepcopy(snapshot)
        bad["traffic"] *= 2
        with self.assertRaises(ValueError):
            replica.install_snapshot(bad)

    def test_snapshot_header_is_owned_during_slow_preparation(self):
        snapshot = self.store.export()
        revision = snapshot["meta"]["revision"]
        entered, release = threading.Event(), threading.Event()

        def slow(graph):
            entered.set()
            self.assertTrue(release.wait(3))
            return prepare_graph(graph)

        replica = self.replica()
        with (
            patch("maps_service.catalog.prepare_graph", side_effect=slow),
            ThreadPoolExecutor(1) as pool,
        ):
            future = pool.submit(replica.install_snapshot, snapshot)
            try:
                self.assertTrue(entered.wait(1))
                snapshot["meta"]["revision"] += 100
            finally:
                release.set()
            self.assertTrue(future.result(3))
        self.assertEqual(replica.health()["revision"], revision)

    def test_graph_preparation_does_not_hold_sqlite_writer(self):
        entered, release = threading.Event(), threading.Event()

        def slow(graph):
            entered.set()
            self.assertTrue(release.wait(3))
            return prepare_graph(graph)

        with (
            patch.object(store_module, "prepare_graph", side_effect=slow),
            ThreadPoolExecutor(2) as pool,
        ):
            future = pool.submit(self.store.import_graph, self.graph)
            try:
                self.assertTrue(entered.wait(1))
                other = pool.submit(self.store.provision, "carol", "carol-private")
                other.result(1)
                self.assertTrue(self.store.health()["ready"])
            finally:
                release.set()
            future.result(3)

    def test_legacy_class_migration_preserves_hash_and_defaults(self):
        before = self.store.health()
        with self.store.transaction(True) as db:
            db.execute("ALTER TABLE edges DROP COLUMN road_class")
        upgraded = Store(self.path)
        self.assertEqual(upgraded.health()["graph_hash"], before["graph_hash"])
        with upgraded.transaction() as db:
            document = upgraded.graph_document(db)
            self.assertEqual(
                {r[0] for r in db.execute("SELECT road_class FROM edges")}, {"local"}
            )
        self.assertEqual(
            hashlib.sha256(dumps(document).encode()).hexdigest(), before["graph_hash"]
        )

    def test_road_classes_roundtrip_and_affect_graph_hash(self):
        original = self.store.health()["graph_hash"]
        self.graph["edges"][0]["road_class"] = "highway"
        self.store.import_graph(self.graph)
        snapshot = self.store.export()
        self.assertNotEqual(snapshot["meta"]["graph_hash"], original)
        self.assertEqual(snapshot["graph"]["edges"][0]["road_class"], "highway")
        replica = self.replica()
        replica.install_snapshot(snapshot)
        self.assertEqual(replica.export(), snapshot)

    @contextmanager
    def queued_write(self, function, *args):
        entered = threading.Event()
        original = self.store.transaction

        @contextmanager
        def tracked(write=False):
            if write:
                entered.set()
            with original(write) as db:
                yield db

        with ThreadPoolExecutor(1) as pool:
            with (
                original(True),
                patch.object(self.store, "transaction", side_effect=tracked),
            ):
                future = pool.submit(function, *args)
                self.assertTrue(entered.wait(1))
                yield future

    def test_queued_ingest_rechecks_timestamp_after_writer_acquisition(self):
        body = self.report()
        with self.queued_write(self.store.ingest, "alice", [body]) as future:
            self.now[0] += 86400
        with self.assertRaises(ValueError):
            future.result()
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 0)

    def test_traffic_ttl_starts_after_writer_wait(self):
        with self.queued_write(
            self.store.traffic, [{"edge": "ab", "speed_mps": 2}], 30
        ):
            self.now[0] += 100
        with self.store.transaction() as db:
            self.assertEqual(
                db.execute("SELECT expires FROM traffic").fetchone()[0],
                self.now[0] + 30,
            )

    def test_atomic_batch_freezes_scalar_reports_and_samples_time_once(self):
        samples = []

        def clock():
            samples.append(1)
            return self.now[0]

        self.store.clock = clock
        self.store.ingest(
            "alice",
            [
                self.report(),
                self.report("p2", 2, timestamp=self.now[0] + 1, longitude=-0.001),
            ],
        )
        self.assertEqual(len(samples), 1)
        with self.store.transaction() as db:
            self.assertEqual(
                len({r[0] for r in db.execute("SELECT received FROM events")}), 1
            )

    def test_unknown_user_and_unknown_fields_cannot_write_history(self):
        with self.assertRaises(Error):
            self.store.ingest("unknown", [self.report()])
        with self.assertRaises(ValueError):
            self.store.ingest(
                "alice", [self.report(), self.report("p2", 2, secret="unexpected")]
            )
        self.assertEqual(self.store.history("alice")["reports"], [])

    def test_offline_old_reports_do_not_poison_live_traffic(self):
        for user in ("alice", "bob"):
            self.store.ingest(
                user,
                [
                    self.report(timestamp=self.now[0] - 4000),
                    self.report(
                        "p2", 2, timestamp=self.now[0] - 3980, longitude=-0.0006
                    ),
                ],
            )
        self.assertEqual(self.store.aggregate()["updated"], [])
        self.assertEqual(len(self.store.history("alice")["reports"]), 2)

    def test_aggregator_does_not_reopen_unexpired_admin_closure(self):
        for user in ("alice", "bob"):
            self.store.ingest(
                user,
                [
                    self.report(timestamp=self.now[0] - 40),
                    self.report("p2", 2, timestamp=self.now[0] - 20, longitude=-0.0006),
                ],
            )
        self.store.traffic([{"edge": "ab", "closed": True}], ttl=30)
        self.assertEqual(self.store.aggregate()["updated"], [])
        self.now[0] += 31
        self.assertEqual(self.store.aggregate()["updated"][0]["edge"], "ab")

    def test_latest_sample_in_one_batch_is_not_chosen_by_event_id_sort(self):
        self.store.ingest(
            "alice",
            [
                self.report(timestamp=self.now[0] - 40),
                self.report("z_old", 2, timestamp=self.now[0] - 30, longitude=-0.0014),
                self.report("a_new", 3, timestamp=self.now[0] - 20, longitude=-0.0006),
            ],
        )
        self.store.ingest(
            "bob",
            [
                self.report(timestamp=self.now[0] - 40),
                self.report("p2", 2, timestamp=self.now[0] - 20, longitude=-0.0006),
            ],
        )
        self.assertAlmostEqual(
            self.store.aggregate()["updated"][0]["speed_mps"], 7.784, places=3
        )

    def test_one_users_ingest_does_not_globally_prune_other_history(self):
        self.store.ingest("bob", [self.report()])
        self.now[0] += 86401
        self.store.ingest("alice", [self.report("new")])
        self.assertEqual(self.store.history("bob")["reports"], [])
        with self.store.transaction() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM events WHERE user='bob'").fetchone()[
                    0
                ],
                1,
            )
        self.store.prune()
        with self.store.transaction() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM events WHERE user='bob'").fetchone()[
                    0
                ],
                0,
            )

    def test_configuration_and_numeric_boundaries_are_strict(self):
        for kwargs in (
            {"readonly": 1},
            {"clock": None},
            {"clock": lambda: True},
            {"clock": lambda: 10**1000},
        ):
            with self.subTest(kwargs=tuple(kwargs)), self.assertRaises(ValueError):
                Store(self.path, **kwargs)
        with self.assertRaises(ValueError):
            Store(":memory:")
        for key in ("latitude", "longitude", "timestamp", "heading"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.store.ingest("alice", [self.report(**{key: 10**1000})])

    def test_outgoing_speed_mutation_cannot_change_future_routes(self):
        expected = route(self.store, "A", "C")
        with self.store.transaction() as db:
            graph = self.store.ready(db)["graph"]
            node = self.store.resolve(db, "A")
            outgoing = self.store.outgoing(db, node, graph, set())
            for edge in outgoing:
                edge["speeds"]["driving"] = 0.001
                edge["length"] = 1e6
            outgoing.clear()
            self.assertEqual(
                self.store.outgoing(db, self.store.resolve(db, "F"), graph, set()), []
            )
        actual = route(self.store, "A", "C")
        self.assertEqual(actual, expected)


class CacheReliabilityTests(unittest.TestCase):
    def test_cold_load_coalescing_and_warm_hit_overlap(self):
        cache = Cache()
        cache.get("warm", lambda: {"ready": True})
        entered, release = threading.Event(), threading.Event()
        calls = []

        def load():
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(3))
            return {"a": [{"speeds": {"driving": 1}}]}

        with ThreadPoolExecutor(8) as pool:
            futures = [pool.submit(cache.get, "cold", load) for _ in range(7)]
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(
                    pool.submit(cache.get, "warm", lambda: None).result(1),
                    {"ready": True},
                )
            finally:
                release.set()
            values = [f.result(2) for f in futures]
        self.assertEqual(len(calls), 1)
        values[0]["a"][0]["speeds"]["driving"] = 100
        self.assertEqual(values[1]["a"][0]["speeds"]["driving"], 1)

    def test_adjacency_projection_copies_only_selected_list(self):
        cache = Cache()
        source = {"a": [{"speed": 1}], "b": [{"speed": 2}]}
        selected = cache.get_item("tile", "a", lambda: source)
        selected[0]["speed"] = 99
        source["a"][0]["speed"] = 100
        self.assertEqual(cache.get_item("tile", "a", lambda: None), [{"speed": 1}])
        self.assertEqual(cache.get_item("tile", "missing", lambda: None), [])

    def test_failed_load_and_disabled_cache_remain_usable(self):
        cache = Cache(capacity=0)
        with self.assertRaises(OSError):
            cache.get("a", lambda: (_ for _ in ()).throw(OSError("read failed")))
        self.assertEqual(cache.get("a", lambda: 1), 1)
        self.assertEqual(cache.get("a", lambda: 2), 2)
        self.assertFalse(cache.items)


if __name__ == "__main__":
    unittest.main()
