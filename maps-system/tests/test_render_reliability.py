"""Geometry, classified routing tiles, viewport URLs and cold-render bounds."""

import json
import math
from pathlib import Path
import random
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from maps_service.geo import (
    EARTH,
    MAX_LAT,
    cell,
    clipped_segments,
    coordinates,
    distance,
    match_point,
    project,
    unproject,
)
from maps_service.navigation import route
from maps_service.render import render_tile, routing_tile, tile_urls
from maps_service.store import Error, Store


class GeographyReliabilityTests(unittest.TestCase):
    def test_invalid_numeric_inputs_and_cell_zoom_are_rejected(self):
        for value in (True, None, "0", float("nan"), float("inf"), 10**1000):
            with (
                self.subTest(value=type(value).__name__),
                self.assertRaises(ValueError),
            ):
                coordinates(value, 0)
        for z in (-1, True, 1.0, 22, "16"):
            with self.subTest(z=z), self.assertRaises(ValueError):
                cell(0, 0, z)
        for x, y in (
            (-0.001, 0),
            (1.001, 0),
            (0, -0.001),
            (0, 1.001),
            (float("nan"), 0),
        ):
            with self.subTest(x=x, y=y), self.assertRaises(ValueError):
                unproject(x, y)

    def test_projection_polar_roundoff_is_clamped_to_normalized_domain(self):
        for latitude in (-90, -MAX_LAT, 0, MAX_LAT, 90):
            for longitude in (-180, 0, 180):
                x, y = project(latitude, longitude)
                self.assertTrue(0 <= x <= 1 and 0 <= y <= 1)
                lat, lon = unproject(x, y)
                self.assertAlmostEqual(
                    lat, max(-MAX_LAT, min(MAX_LAT, latitude)), places=8
                )
                self.assertAlmostEqual(lon, longitude, places=8)

    def test_distances_match_independent_unit_vector_oracle(self):
        rng = random.Random(1901)

        def vector(point):
            latitude, longitude = map(math.radians, point)
            return (
                math.cos(latitude) * math.cos(longitude),
                math.cos(latitude) * math.sin(longitude),
                math.sin(latitude),
            )

        for _ in range(500):
            first = (rng.uniform(-90, 90), rng.uniform(-180, 180))
            second = (rng.uniform(-90, 90), rng.uniform(-180, 180))
            a, b = vector(first), vector(second)
            cross = (
                a[1] * b[2] - a[2] * b[1],
                a[2] * b[0] - a[0] * b[2],
                a[0] * b[1] - a[1] * b[0],
            )
            expected = EARTH * math.atan2(
                math.sqrt(sum(v * v for v in cross)), sum(x * y for x, y in zip(a, b))
            )
            self.assertAlmostEqual(distance(first, second), expected, delta=1e-7)
        expected = EARTH * math.radians(179.999999)
        self.assertAlmostEqual(distance((0, 0), (0, 179.999999)), expected, places=5)

    def test_dateline_match_does_not_cross_greenwich(self):
        start, end = (0, 179.999), (0, -179.999)
        gap, fraction, _ = match_point((0, 180), start, end)
        self.assertLess(gap, 1e-5)
        self.assertAlmostEqual(fraction, 0.5, places=6)
        far, _, _ = match_point((0, 0), start, end)
        self.assertGreater(far, 19000000)

    def test_horizontal_segment_clips_exact_tile_boundaries(self):
        for z in (2, 3, 12):
            n = 2**z
            x, y = n // 2, n // 2
            latitude, west = unproject(x / n, (y + 0.5) / n)
            _, east = unproject((x + 1) / n, (y + 0.5) / n)
            pieces = clipped_segments(
                (latitude, west - 0.001), (latitude, east + 0.001), z, x, y
            )
            self.assertEqual(len(pieces), 1)
            self.assertAlmostEqual(pieces[0][0][0], 0)
            self.assertAlmostEqual(pieces[0][1][0], 1)
            self.assertAlmostEqual(pieces[0][0][1], 0.5)


class RenderReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "maps.sqlite")
        self.document = json.loads((ROOT / "examples/roads.json").read_text())
        self.store.import_graph(self.document)
        self.graph = self.store.health()["graph"]

    def test_viewport_urls_wrap_longitude_clamp_poles_and_pin_graph(self):
        world = tile_urls(self.store, 90, 180, 0)
        self.assertEqual(len(world["tiles"]), 1)
        self.assertEqual(world["tiles"][0]["url"], f"/v1/tiles/{self.graph}/0/0/0.png")
        border = tile_urls(self.store, 0, 180, 3, "geojson")
        self.assertEqual({row["x"] for row in border["tiles"]}, {0, 6, 7})
        self.assertEqual(len(border["tiles"]), 9)
        polar = tile_urls(self.store, 90, 0, 3)
        self.assertEqual({row["y"] for row in polar["tiles"]}, {0, 1})
        self.assertEqual(len(polar["tiles"]), 6)
        self.assertTrue(all(self.graph in row["url"] for row in polar["tiles"]))

    def test_cold_render_budgets_fail_without_poisoning_later_requests(self):
        with self.assertRaises(Error) as caught:
            render_tile(self.store, self.graph, 0, 0, 0, max_edges=1)
        self.assertEqual(caught.exception.status, 413)
        with self.assertRaises(Error) as caught:
            render_tile(self.store, self.graph, 0, 0, 0, max_segments=1)
        self.assertEqual(caught.exception.status, 413)
        payload, _ = render_tile(self.store, self.graph, 0, 0, 0, "png")
        self.assertEqual(payload[:8], b"\x89PNG\r\n\x1a\n")

    def test_archive_hit_uses_exact_graph_hash_and_preserves_version_fence(self):
        calls = []

        class Archive:
            def get(self, *args):
                calls.append(args)
                return b"stored-png", "image/png"

        self.store.tile_archive = Archive()
        self.store.spatial_edges = lambda *args, **kwargs: self.fail(
            "archive hit rerendered"
        )
        self.assertEqual(
            render_tile(self.store, self.graph, 2, 0, 0, "png"),
            (b"stored-png", "image/png"),
        )
        self.assertEqual(
            calls[0], (self.graph, self.store.health()["graph_hash"], 2, 0, 0, "png")
        )
        self.store.import_graph(self.document)
        with self.assertRaises(Error) as caught:
            render_tile(self.store, self.graph, 2, 0, 0, "png")
        self.assertEqual(caught.exception.status, 410)
        self.assertEqual(len(calls), 1)

    def test_default_local_graph_does_not_fabricate_highway_layers(self):
        for z in (8, 12):
            x, y = cell(0, 0, z)
            result = routing_tile(self.store, self.graph, x, y, z=z)
            self.assertEqual(result["edges"], [])
            self.assertEqual(result["nodes"], [])

    def classified_graph(self):
        return {
            "nodes": [
                {"id": "A", "latitude": 10, "longitude": 10},
                {"id": "B", "latitude": 10, "longitude": 10.001},
                {"id": "C", "latitude": 10.001, "longitude": 10.001},
                {"id": "D", "latitude": 10.001, "longitude": 10.008},
            ],
            "edges": [
                {
                    "id": "local",
                    "source": "A",
                    "target": "B",
                    "speeds": {"driving": 10},
                },
                {
                    "id": "arterial",
                    "source": "B",
                    "target": "C",
                    "speeds": {"driving": 15},
                    "road_class": "arterial",
                },
                {
                    "id": "highway",
                    "source": "C",
                    "target": "D",
                    "speeds": {"driving": 25},
                    "road_class": "highway",
                },
            ],
        }

    def test_three_road_layers_have_class_filters_and_cross_level_entrances(self):
        self.store.import_graph(self.classified_graph())
        graph = self.store.health()["graph"]
        tiles = {}
        for z in (16, 12, 8):
            x, y = cell(10, 10, z)
            tiles[z] = routing_tile(self.store, graph, x, y, z=z)
        self.assertEqual(
            {row["id"] for row in tiles[16]["edges"]}, {"local", "arterial", "highway"}
        )
        self.assertEqual(
            {row["id"] for row in tiles[12]["edges"]}, {"arterial", "highway"}
        )
        self.assertEqual({row["id"] for row in tiles[8]["edges"]}, {"highway"})
        self.assertEqual(tiles[16]["parent"], tiles[12]["tile"])
        self.assertEqual(tiles[12]["parent"], tiles[8]["tile"])
        self.assertIn(tiles[16]["tile"], tiles[12]["children"])
        self.assertTrue(
            any(
                row["node"] == "C" and row["to"][0] == 8 for row in tiles[16]["portals"]
            )
        )
        highway = next(row for row in tiles[16]["edges"] if row["id"] == "highway")
        self.assertNotEqual(highway["source_tile"], highway["target_tile"])
        self.assertEqual(
            route(self.store, "A", "D")["routes"][0]["edges"],
            ["local", "arterial", "highway"],
        )

    def test_routing_tile_budget_and_return_value_isolation(self):
        self.store.import_graph(self.classified_graph())
        graph = self.store.health()["graph"]
        x, y = cell(10, 10)
        with self.assertRaises(Error) as caught:
            routing_tile(self.store, graph, x, y, max_nodes=1)
        self.assertEqual(caught.exception.status, 413)
        first = routing_tile(self.store, graph, x, y)
        first["edges"][0]["speeds"]["driving"] = 0
        first["nodes"].clear()
        fresh = routing_tile(self.store, graph, x, y)
        self.assertEqual(fresh["edges"][0]["speeds"]["driving"], 10)
        self.assertTrue(fresh["nodes"])


if __name__ == "__main__":
    unittest.main()
