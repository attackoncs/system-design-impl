from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import math
from pathlib import Path
import random
import struct
import sys
import tempfile
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from maps_service.geo import MAX_LAT, cell, clipped_segments, project, tile, unproject
from maps_service.navigation import route
from maps_service.render import render_tile, routing_tile
from maps_service.store import Error, Store


class MapsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "maps.sqlite"
        self.now = [2000000000.0]
        self.store = Store(self.path, clock=lambda: self.now[0])
        self.graph = json.loads((ROOT / "examples/roads.json").read_text())
        self.store.import_graph(self.graph)
        for user in ("alice", "bob"):
            self.store.provision(user, user + "-private", admin=user == "alice")

    def tearDown(self):
        self.temp.cleanup()

    def test_routes_modes_oneway_alternatives_and_tolls(self):
        result = route(self.store, "West Station", "East Station", alternatives=3)
        self.assertEqual(result["routes"][0]["edges"], ["ab", "bc"])
        self.assertAlmostEqual(result["routes"][0]["eta_seconds"], 44.48)
        self.assertGreaterEqual(len(result["routes"]), 2)
        self.assertEqual(route(self.store, "A", "D")["routes"][0]["edges"], ["ad"])
        self.assertEqual(
            route(self.store, "A", "D", avoid_tolls=True)["routes"][0]["edges"],
            ["ab", "bd"],
        )
        self.assertEqual(
            route(self.store, "A", "D", mode="walking")["routes"][0]["edges"],
            ["ab", "bd"],
        )
        for target in ("A", "F"):
            with self.assertRaises(Error) as caught:
                route(self.store, "C", target)
            self.assertEqual(caught.exception.status, 404)
        with self.assertRaises(ValueError):
            route(self.store, "A", "C", mode="transit")

    def test_traffic_expiry_closure_and_rerouting(self):
        initial = route(self.store, "A", "C")
        self.store.traffic([{"edge": "bc", "speed_mps": 1}], ttl=30)
        changed = route(self.store, "A", "C")
        self.assertEqual(changed["routes"][0]["edges"], ["ad", "de", "ec"])
        self.assertGreater(changed["traffic_version"], initial["traffic_version"])
        self.assertEqual(
            route(self.store, "A", "C", mode="walking")["routes"][0]["edges"],
            ["ab", "bc"],
        )
        self.now[0] += 30
        self.assertEqual(
            route(self.store, "A", "C")["routes"][0]["edges"], ["ab", "bc"]
        )
        self.store.traffic(
            [{"edge": "bc", "closed": True}, {"edge": "ec", "closed": True}]
        )
        with self.assertRaises(Error):
            route(self.store, "A", "C")

    def test_geocoding_snapping_and_bounded_search(self):
        self.assertEqual(self.store.geocode("  CENTRAL ")["node"], "B")
        self.assertEqual(
            self.store.geocode({"latitude": 0, "longitude": -0.00199}, True)["node"],
            "A",
        )
        with self.assertRaises(Error):
            self.store.geocode({"latitude": 30, "longitude": 80}, True)
        with self.assertRaises(Error) as caught:
            route(self.store, "A", "C", max_nodes=1)
        self.assertEqual(caught.exception.status, 413)

    def test_import_atomicity_restart_and_spatial_rebuild(self):
        old = self.store.health()
        for graph in [
            dict(self.graph, nodes=self.graph["nodes"] * 2),
            dict(self.graph, edges=[{**self.graph["edges"][0], "target": "absent"}]),
            dict(self.graph, edges=[{**self.graph["edges"][0], "length_m": -1}]),
        ]:
            with self.assertRaises(Exception):
                self.store.import_graph(graph)
            self.assertEqual(self.store.health()["graph"], old["graph"])
        with self.assertRaises(RuntimeError):
            self.store.import_graph(
                self.graph,
                fail_hook=lambda: (_ for _ in ()).throw(RuntimeError("injected")),
            )
        restarted = Store(self.path, clock=lambda: self.now[0])
        self.assertEqual(restarted.health()["graph"], old["graph"])
        self.assertEqual(route(restarted, "A", "C")["routes"][0]["edges"], ["ab", "bc"])

    def test_projection_xyz_wrap_and_poles(self):
        self.assertEqual(cell(0, 0, 1), (1, 1))
        self.assertEqual(tile(3, -1, 0), (3, 7, 0))
        for lat in [-90, -MAX_LAT, 0, MAX_LAT, 90]:
            x, y = project(lat, 180)
            p, q = unproject(x, y)
            self.assertAlmostEqual(p, max(-MAX_LAT, min(MAX_LAT, lat)), places=7)
            self.assertAlmostEqual(q, 180)
        for args in [(22, 0, 0), (1, 0, 2), (True, 0, 0)]:
            with self.assertRaises(ValueError):
                tile(*args)

    def test_vector_dateline_seams_and_actual_png(self):
        graph = self.store.health()["graph"]
        western = json.loads(render_tile(self.store, graph, 1, 0, 0)[0])
        eastern = json.loads(render_tile(self.store, graph, 1, 1, 0)[0])
        for doc in (western, eastern):
            pieces = [f for f in doc["features"] if f["properties"]["edge"] == "wz"]
            self.assertTrue(pieces)
            self.assertTrue(
                all(
                    abs(
                        f["geometry"]["coordinates"][1][0]
                        - f["geometry"]["coordinates"][0][0]
                    )
                    < 1
                    for f in pieces
                )
            )
        segments = clipped_segments((0, -0.002), (0, 0.002), 1, 0, 1)
        self.assertTrue(segments)
        self.assertAlmostEqual(segments[0][1][0], 1)
        data, content_type = render_tile(self.store, graph, 0, 0, 0, "png")
        self.assertEqual(content_type, "image/png")
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", data[16:24]), (256, 256))
        offset = 8
        compressed = b""
        while offset < len(data):
            size = struct.unpack(">I", data[offset : offset + 4])[0]
            kind = data[offset + 4 : offset + 8]
            payload = data[offset + 8 : offset + 8 + size]
            checksum = struct.unpack(
                ">I", data[offset + 8 + size : offset + 12 + size]
            )[0]
            self.assertEqual(zlib.crc32(kind + payload) & 0xFFFFFFFF, checksum)
            if kind == b"IDAT":
                compressed += payload
            offset += 12 + size
        self.assertEqual(len(zlib.decompress(compressed)), 256 * (256 * 3 + 1))

    def test_cache_graph_version_and_routing_tile_references(self):
        graph = self.store.health()["graph"]
        render_tile(self.store, graph, 1, 0, 0)
        render_tile(self.store, graph, 1, 0, 0)
        self.assertGreater(self.store.tiles.hits, 0)
        tx, ty = cell(0, -0.002)
        document = routing_tile(self.store, graph, tx, ty)
        self.assertIn("B", [r["target"] for r in document["edges"]])
        self.store.import_graph(self.graph)
        with self.assertRaises(Error) as caught:
            render_tile(self.store, graph, 1, 0, 0)
        self.assertEqual(caught.exception.status, 410)

    def report(self, event, seq, stamp, lon, **extra):
        return {
            "event_id": event,
            "sequence": seq,
            "timestamp": stamp,
            "latitude": 0,
            "longitude": lon,
            "heading": 90,
            **extra,
        }

    def test_batch_replay_late_reports_atomic_conflicts_and_retention(self):
        body = self.report("one", 2, self.now[0], -0.001)
        first = self.store.ingest("alice", [body])
        self.assertEqual(self.store.ingest("alice", [body]), first)
        late = self.report("late", 1, self.now[0] - 10, -0.0015)
        self.assertFalse(self.store.ingest("alice", [late])["reports"][0]["current"])
        with self.assertRaises(Error):
            self.store.ingest(
                "alice",
                [
                    self.report("new", 3, self.now[0] + 1, -0.0005),
                    {**body, "longitude": 0},
                ],
            )
        self.assertEqual(len(self.store.history("alice")["reports"]), 2)
        self.assertEqual(self.store.history("bob")["reports"], [])
        self.assertEqual(
            Store(self.path, clock=lambda: self.now[0]).ingest("alice", [body]), first
        )
        self.now[0] += 86401
        self.assertEqual(self.store.history("alice")["reports"], [])
        self.store.prune()
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 0)

    def test_direction_matching_and_distinct_user_traffic_aggregation(self):
        for user in ("alice", "bob"):
            self.store.ingest(
                user,
                [
                    self.report("p1", 1, self.now[0] - 40, -0.0018),
                    self.report("p2", 2, self.now[0] - 20, -0.0006),
                ],
            )
        result = self.store.aggregate()
        self.assertEqual(result["updated"][0]["edge"], "ab")
        self.assertEqual(result["updated"][0]["distinct_users"], 2)
        self.assertAlmostEqual(result["updated"][0]["speed_mps"], 6.672, places=3)
        with self.store.transaction() as db:
            west = self.store.match(db, 0, -0.001, "driving", 270)
            self.assertEqual(west["edge"], "ba")
            self.assertFalse(
                self.store.match(db, 0, -0.001, "driving")["traffic_eligible"]
            )
        with self.assertRaises(ValueError):
            self.store.aggregate(min_users=1)

    def test_replica_snapshot_atomicity_monotonicity_and_privacy(self):
        self.store.ingest("alice", [self.report("private", 1, self.now[0], -0.001)])
        snapshot = self.store.export()
        serialized = json.dumps(snapshot)
        self.assertNotIn("private", serialized)
        self.assertNotIn("alice", serialized)
        replica = Store(
            Path(self.temp.name) / "replica.sqlite",
            readonly=True,
            clock=lambda: self.now[0],
        )
        replica.install_snapshot(snapshot)
        self.assertEqual(route(replica, "A", "C")["routes"][0]["edges"], ["ab", "bc"])
        bad = deepcopy(snapshot)
        bad["meta"]["revision"] += 1
        bad["graph"]["nodes"][0]["longitude"] += 0.0001
        with self.assertRaises(Error):
            replica.install_snapshot(bad)
        self.assertEqual(replica.health()["graph_hash"], snapshot["meta"]["graph_hash"])
        bad = deepcopy(snapshot)
        bad["meta"]["revision"] -= 1
        with self.assertRaises(Error):
            replica.install_snapshot(bad)
        with self.assertRaises(Error):
            replica.import_graph(self.graph)

    def test_dijkstra_matches_floyd_warshall_oracle_and_lazy_cache(self):
        rng = random.Random(19)
        nodes = [
            {"id": str(i), "latitude": 0, "longitude": i * 0.01} for i in range(15)
        ]
        edges = []
        matrix = [[math.inf] * 15 for _ in range(15)]
        for i in range(15):
            matrix[i][i] = 0
            for j in range(15):
                if i != j and rng.random() < 0.15:
                    cost = rng.uniform(1, 100)
                    edges.append(
                        {
                            "id": f"e{i}_{j}",
                            "source": str(i),
                            "target": str(j),
                            "length_m": cost,
                            "speeds": {"driving": 1},
                        }
                    )
                    matrix[i][j] = cost
        self.store.import_graph({"nodes": nodes, "edges": edges})
        for k in range(15):
            for i in range(15):
                for j in range(15):
                    matrix[i][j] = min(matrix[i][j], matrix[i][k] + matrix[k][j])
        for i in range(15):
            for j in range(15):
                if math.isfinite(matrix[i][j]):
                    self.assertAlmostEqual(
                        route(self.store, str(i), str(j))["routes"][0]["eta_seconds"],
                        matrix[i][j],
                    )
                else:
                    with self.assertRaises(Error):
                        route(self.store, str(i), str(j))
        self.assertLessEqual(len(self.store.adjacency.items), 64)

    def test_concurrent_graph_swaps_keep_routes_in_one_snapshot(self):
        expected = {self.store.health()["graph"]: (["ab", "bc"], 44.48)}
        observed = []

        def writer():
            for i in range(8):
                graph = deepcopy(self.graph)
                if i % 2 == 0:
                    graph["edges"][2]["speeds"]["driving"] = 1
                meta = self.store.import_graph(graph)
                expected[meta["graph"]] = (
                    (["ad", "de", "ec"], 54.15) if i % 2 == 0 else (["ab", "bc"], 44.48)
                )

        def reader():
            for _ in range(30):
                observed.append(route(self.store, "A", "C"))

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(writer),
                *[executor.submit(reader) for _ in range(3)],
            ]
            for future in futures:
                future.result()
        for result in observed:
            edges, eta = expected[result["graph"]]
            self.assertEqual(result["routes"][0]["edges"], edges)
            self.assertAlmostEqual(result["routes"][0]["eta_seconds"], eta)


if __name__ == "__main__":
    unittest.main()
