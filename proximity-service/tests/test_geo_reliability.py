"""Independent spherical oracle and conservative cell-coverage boundaries."""

import math
from pathlib import Path
import random
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proximity.geo import EARTH, RADII, bounds, coordinates, cover, distance, encode


def oracle_distance(lat, lon, other_lat, other_lon):
    """Unit-vector cross/dot angle, independent of the serving implementation."""

    def vector(latitude, longitude):
        p, q = math.radians(latitude), math.radians(longitude)
        return math.cos(p) * math.cos(q), math.cos(p) * math.sin(q), math.sin(p)

    a, b = vector(lat, lon), vector(other_lat, other_lon)
    cross = (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )
    return EARTH * math.atan2(
        math.sqrt(sum(value * value for value in cross)),
        sum(left * right for left, right in zip(a, b)),
    )


def destination(lat, lon, meters, bearing):
    p, l, b = map(math.radians, (lat, lon, bearing))
    angular = meters / EARTH
    q = math.asin(
        max(
            -1,
            min(
                1,
                math.sin(p) * math.cos(angular)
                + math.cos(p) * math.sin(angular) * math.cos(b),
            ),
        )
    )
    m = l + math.atan2(
        math.sin(b) * math.sin(angular) * math.cos(p),
        math.cos(angular) - math.sin(p) * math.sin(q),
    )
    return math.degrees(q), (math.degrees(m) + 180) % 360 - 180


class GeoReliabilityTests(unittest.TestCase):
    def test_coordinate_failures_are_uniform_and_query_strings_remain_supported(self):
        self.assertEqual(coordinates("42.6", "-5.6"), (42.6, -5.6))
        for value in (
            None,
            {},
            [],
            object(),
            True,
            False,
            float("nan"),
            float("inf"),
            10**1000,
            "invalid",
        ):
            with self.subTest(value=type(value).__name__):
                for arguments in ((value, 0), (0, value)):
                    with self.assertRaises(ValueError):
                        coordinates(*arguments)
                with self.assertRaises(ValueError):
                    distance(0, 0, value, 0)

    def test_precision_radius_and_budget_reject_boolean_and_noninteger_values(self):
        for precision in (True, False, 1.0, "5", None, 0, 13, float("nan")):
            with self.subTest(precision=precision), self.assertRaises(ValueError):
                encode(0, 0, precision)
        for radius in (True, 500.0, "500", None, 0, 501, float("nan"), float("inf")):
            with self.subTest(radius=radius), self.assertRaises(ValueError):
                cover(0, 0, radius)
        for budget in (True, False, 1.0, None, "100", 0, -1, float("inf")):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                cover(0, 0, 500, max_cells=budget)

    def test_geohash_parser_is_bounded_and_rejects_invalid_codes(self):
        for code in (None, [], 1, "", "a", "EZs42", "z" * 13):
            with self.subTest(code=code), self.assertRaises(ValueError):
                bounds(code)
        for latitude, longitude in (
            (-90, -180),
            (-90, 180),
            (90, -180),
            (90, 180),
            (0, 0),
            (42.6, -5.6),
            (89.999999, -179.999999),
        ):
            for precision in range(1, 13):
                south, north, west, east = bounds(
                    encode(latitude, longitude, precision)
                )
                self.assertLessEqual(south, latitude)
                self.assertLessEqual(latitude, north)
                self.assertLessEqual(west, longitude)
                self.assertLessEqual(longitude, east)

    def test_dateline_coincidence_and_near_antipodes_keep_distance_precision(self):
        self.assertEqual(distance(0, -180, 0, 180), 0)
        self.assertEqual(distance(12.5, 30.5, 12.5, 30.5), 0)
        self.assertAlmostEqual(distance(0, 0, 0, 180), math.pi * EARTH, delta=1e-7)
        # A rounded haversine of one loses this remaining centimetre.
        self.assertAlmostEqual(
            distance(0, 0, 0, 180 - 1e-7), EARTH * math.radians(180 - 1e-7), delta=1e-6
        )
        self.assertAlmostEqual(distance(90, 0, 90, 180), 0, delta=1e-6)
        self.assertAlmostEqual(distance(-90, 0, 90, 180), math.pi * EARTH, delta=1e-7)

    def test_random_distances_match_independent_vector_oracle(self):
        generator = random.Random(1701)
        for _ in range(1000):
            values = (
                generator.uniform(-90, 90),
                generator.uniform(-180, 180),
                generator.uniform(-90, 90),
                generator.uniform(-180, 180),
            )
            self.assertAlmostEqual(
                distance(*values), oracle_distance(*values), delta=1e-5
            )
            self.assertAlmostEqual(
                distance(*values),
                distance(values[2], values[3], values[0], values[1]),
                delta=1e-7,
            )

    def test_randomized_bruteforce_filter_has_no_coverage_false_negatives(self):
        generator = random.Random(1717)
        centers = [
            (0, 0),
            (0, -180),
            (0, 180),
            (90, 180),
            (-90, -180),
            (89.9955, 179.999999),
            (-89.9955, -179.999999),
            (60, 0),
        ]
        centers.extend(
            (generator.uniform(-90, 90), generator.uniform(-180, 180))
            for _ in range(64)
        )
        tested = 0
        for lat, lon in centers:
            for radius in RADII:
                precision, cells = cover(lat, lon, radius)
                self.assertEqual(cells, sorted(set(cells)))
                self.assertLessEqual(len(cells), 4096)
                candidates = set(cells)
                points = [
                    destination(lat, lon, radius * factor, bearing)
                    for factor in (
                        0,
                        0.5,
                        (radius - 0.001) / radius,
                        1,
                        (radius + 0.001) / radius,
                        1.4,
                    )
                    for bearing in (0, 45, 90, 135, 180, 225, 270, 315)
                ]
                points.extend(
                    (generator.uniform(-90, 90), generator.uniform(-180, 180))
                    for _ in range(24)
                )
                expected = {
                    index
                    for index, point in enumerate(points)
                    if oracle_distance(lat, lon, *point) <= radius + 1e-6
                }
                actual = {
                    index
                    for index, point in enumerate(points)
                    if encode(*point, precision) in candidates
                    and distance(lat, lon, *point) <= radius + 1e-6
                }
                self.assertEqual(actual, expected, (lat, lon, radius, precision))
                tested += len(points)
        self.assertEqual(tested, 25920)

    def test_polar_cell_budget_coarsens_without_dropping_longitudes(self):
        precision, cells = cover(89.999, 179.999, 20000, max_cells=32)
        self.assertLessEqual(precision, 2)
        self.assertLessEqual(len(cells), 32)
        for bearing in range(0, 360, 5):
            point = destination(89.999, 179.999, 19999, bearing)
            self.assertIn(encode(*point, precision), cells)
        with self.assertRaises(ValueError):
            cover(90, 0, 500, max_cells=1)


if __name__ == "__main__":
    unittest.main()
