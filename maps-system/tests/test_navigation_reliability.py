"""Route geometry, directions and bounded exact-search regressions."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from maps_service.navigation import encode_polyline, route
from maps_service.store import Error, Store


def decode_polyline(encoded):
    """Independent signed-varint decoder, returning longitude/latitude points."""
    values, current, shift = [], 0, 0
    for char in encoded:
        number = ord(char) - 63
        current |= (number & 31) << shift
        if number & 32:
            shift += 5
        else:
            values.append(-(current // 2) - 1 if current & 1 else current // 2)
            current, shift = 0, 0
    result, latitude, longitude = [], 0, 0
    for index in range(0, len(values), 2):
        latitude += values[index]
        longitude += values[index + 1]
        result.append([longitude / 100000, latitude / 100000])
    return result


class NavigationReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(
            Path(self.temp.name) / "maps.sqlite", clock=lambda: 2000000000
        )
        self.graph = json.loads((ROOT / "examples/roads.json").read_text())
        self.store.import_graph(self.graph)

    def test_encoded_polyline_matches_known_vector_and_roundtrips(self):
        points = [[-120.2, 38.5], [-120.95, 40.7], [-126.453, 43.252]]
        self.assertEqual(encode_polyline(points), "_p~iF~ps|U_ulLnnqC_mqNvxq`@")
        self.assertEqual(decode_polyline(encode_polyline(points)), points)
        coordinates = [[180, 90], [-180, -90], [-0.000005, 0.000005], [0, 0]]
        for original, decoded in zip(
            coordinates, decode_polyline(encode_polyline(coordinates))
        ):
            for first, second in zip(original, decoded):
                self.assertLessEqual(abs(first - second), 0.0000051)

    def test_directions_include_turns_geometry_and_exact_edge_costs(self):
        result = route(self.store, "A", "E", avoid_tolls=True)
        path = result["routes"][0]
        self.assertEqual(path["edges"], ["ab", "bd", "de"])
        self.assertEqual(
            [step["maneuver"] for step in path["steps"]],
            ["depart", "turn-left", "turn-right"],
        )
        self.assertEqual(path["steps"][0]["instruction"], "Head east along road ab.")
        self.assertAlmostEqual(
            sum(step["duration_seconds"] for step in path["steps"]), path["eta_seconds"]
        )
        self.assertAlmostEqual(
            sum(step["distance_m"] for step in path["steps"]), path["distance_m"]
        )
        self.assertEqual(decode_polyline(path["polyline"]["points"]), path["geometry"])
        self.assertEqual(
            path["steps"][-1]["end_location"], {"latitude": 0.001, "longitude": 0.002}
        )

    def test_directions_use_same_traffic_snapshot_as_route_eta(self):
        self.store.traffic([{"edge": "ab", "speed_mps": 20}], ttl=30)
        driving = route(self.store, "A", "B")["routes"][0]
        walking = route(self.store, "A", "B", mode="walking")["routes"][0]
        self.assertAlmostEqual(driving["steps"][0]["duration_seconds"], 222.4 / 20)
        self.assertAlmostEqual(walking["steps"][0]["duration_seconds"], 222.4 / 1.4)
        self.assertEqual(
            driving["eta_seconds"], driving["steps"][0]["duration_seconds"]
        )

    def test_same_origin_destination_has_zero_cost_and_valid_single_point_geometry(
        self,
    ):
        result = route(self.store, "B", "B", max_nodes=1)
        path = result["routes"][0]
        self.assertEqual(
            (path["edges"], path["steps"], path["eta_seconds"], path["distance_m"]),
            ([], [], 0, 0),
        )
        self.assertEqual(decode_polyline(path["polyline"]["points"]), [[0, 0]])
        self.assertEqual(result["nodes_visited"], 1)

    def test_alternatives_share_total_search_budget(self):
        baseline = route(self.store, "A", "C")
        with self.assertRaises(Error) as caught:
            route(
                self.store,
                "A",
                "C",
                alternatives=3,
                max_nodes=baseline["nodes_visited"],
            )
        self.assertEqual(caught.exception.status, 413)
        self.assertEqual(
            route(self.store, "A", "C")["routes"][0]["edges"], ["ab", "bc"]
        )

    def test_repeated_candidate_searches_reuse_request_node_hydration(self):
        statements = []
        original_connect = self.store.connect

        def connect():
            connection = original_connect()
            connection.set_trace_callback(statements.append)
            return connection

        self.store.connect = connect
        result = route(self.store, "A", "C", alternatives=3)
        node_reads = [
            query
            for query in statements
            if query.startswith("SELECT * FROM nodes WHERE id=")
        ]
        self.assertLessEqual(len(node_reads), len(self.graph["nodes"]))
        self.assertGreater(result["nodes_visited"], len(node_reads))

    def test_invalid_options_have_uniform_validation_errors(self):
        for options in (
            {"mode": []},
            {"mode": {}},
            {"max_nodes": True},
            {"max_nodes": 0},
            {"alternatives": False},
            {"avoid_tolls": 1},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                route(self.store, "A", "C", **options)

    def test_route_instruction_mutation_does_not_change_later_answers(self):
        first = route(self.store, "A", "C")
        first["routes"][0]["steps"][0]["end_location"]["longitude"] = 100
        first["routes"][0]["geometry"].clear()
        fresh = route(self.store, "A", "C")["routes"][0]
        self.assertEqual(fresh["steps"][0]["end_location"]["longitude"], 0)
        self.assertEqual(len(fresh["geometry"]), 3)


if __name__ == "__main__":
    unittest.main()
