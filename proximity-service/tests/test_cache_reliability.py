"""Cache isolation/overlap and strict public business/query/cursor contracts."""

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proximity.cache import Cache
from proximity.models import Error, validate_business
from proximity.store import Store


def business(name="Cafe", longitude=0):
    return {
        "name": name,
        "latitude": 0,
        "longitude": longitude,
        "category": "cafe",
        "hours": [{"day": 1, "start": 600, "end": 720}],
    }


def cursor_document(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


class CacheReliabilityTests(unittest.TestCase):
    def test_loader_input_and_returned_values_cannot_mutate_cache(self):
        source = business()
        cache = Cache()
        first = cache.get("a", lambda: source)
        source["hours"][0]["end"] = 1000
        first["hours"][0]["start"] = 0
        first["name"] = "Changed"
        second = cache.get("a", lambda: self.fail("warm cache invoked loader"))
        self.assertEqual(second, business())
        second["hours"].clear()
        self.assertEqual(cache.get("a", lambda: None), business())

    def test_warm_hit_does_not_wait_for_cold_loader(self):
        cache = Cache()
        cache.get("warm", lambda: {"value": "ready"})
        entered, release = threading.Event(), threading.Event()

        def load():
            entered.set()
            self.assertTrue(release.wait(3))
            return {"value": "cold"}

        with ThreadPoolExecutor(2) as pool:
            pending = pool.submit(cache.get, "cold", load)
            try:
                self.assertTrue(entered.wait(1))
                warm = pool.submit(cache.get, "warm", lambda: self.fail("cache miss"))
                self.assertEqual(warm.result(1), {"value": "ready"})
            finally:
                release.set()
            self.assertEqual(pending.result(2), {"value": "cold"})

    def test_same_key_loads_are_coalesced_and_results_are_independent(self):
        cache = Cache()
        entered, release = threading.Event(), threading.Event()
        calls = []

        def load():
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(3))
            return {"hours": [{"day": 1}]}

        with ThreadPoolExecutor(16) as pool:
            futures = [pool.submit(cache.get, "shared", load) for _ in range(16)]
            try:
                self.assertTrue(entered.wait(1))
            finally:
                release.set()
            values = [future.result(2) for future in futures]
        self.assertEqual(len(calls), 1)
        self.assertEqual(cache.misses, 1)
        self.assertEqual(cache.hits, 15)
        values[0]["hours"][0]["day"] = 6
        self.assertEqual(values[1]["hours"][0]["day"], 1)

    def test_ttl_starts_when_slow_loader_completes(self):
        clock = [0]
        cache = Cache(ttl=5, clock=lambda: clock[0])

        def slow():
            clock[0] = 100
            return {"generation": 1}

        cache.get("a", slow)
        clock[0] = 104
        self.assertEqual(cache.get("a", lambda: {"generation": 2}), {"generation": 1})
        clock[0] = 105
        self.assertEqual(cache.get("a", lambda: {"generation": 2}), {"generation": 2})

    def test_loader_failure_does_not_poison_cache_or_keep_load_lock(self):
        cache = Cache()

        def fail():
            raise OSError("database unavailable")

        with self.assertRaises(OSError):
            cache.get("a", fail)
        self.assertNotIn("a", cache.items)
        self.assertEqual(cache.get("a", lambda: None), None)
        self.assertEqual(cache.get("a", lambda: self.fail("cached None missed")), None)
        self.assertEqual(cache.misses, 2)

    def test_capacity_zero_and_lru_eviction(self):
        uncached = Cache(capacity=0)
        self.assertEqual(uncached.get("a", lambda: 1), 1)
        self.assertEqual(uncached.get("a", lambda: 2), 2)
        self.assertEqual(
            (len(uncached.items), uncached.hits, uncached.misses), (0, 0, 2)
        )
        cache = Cache(capacity=2)
        cache.get("a", lambda: 1)
        cache.get("b", lambda: 2)
        cache.get("a", lambda: 3)
        cache.get("c", lambda: 4)
        self.assertEqual(set(cache.items), {"a", "c"})

    def test_cache_configuration_is_strict(self):
        for ttl in (True, False, 0, -1, float("nan"), float("inf"), 10**1000, "60"):
            with self.subTest(ttl=type(ttl).__name__), self.assertRaises(ValueError):
                Cache(ttl=ttl)
        for capacity in (True, -1, 1.0, "1", None):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                Cache(capacity=capacity)
        with self.assertRaises(ValueError):
            Cache(clock=None)


class BusinessQueryReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        clock = datetime(2026, 10, 13, 12, tzinfo=timezone.utc).timestamp()
        self.store = Store(
            Path(self.temp.name) / "metadata.sqlite", clock=lambda: clock
        )
        self.identifier = self.store.mutate("alice", "POST", None, business(), "first")[
            "id"
        ]
        self.store.mutate("alice", "POST", None, business("Second", 0.001), "second")
        self.store.publish(force=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_detail_and_nested_search_hours_are_isolated(self):
        first = self.store.detail(self.identifier)
        first["business"]["name"] = "Mutated"
        first["business"]["hours"][0]["start"] = 0
        second = self.store.detail(self.identifier)
        self.assertEqual(second["business"]["name"], "Cafe")
        self.assertEqual(second["business"]["hours"][0]["start"], 600)
        result = self.store.search(0, 0, 5000)
        result["businesses"][0]["hours"][0]["end"] = 1440
        result["businesses"][0]["name"] = "Modified search"
        again = self.store.search(0, 0, 5000)
        self.assertEqual(again["businesses"][0]["hours"][0]["end"], 720)
        self.assertEqual(self.store.detail(self.identifier)["business"]["name"], "Cafe")

    def test_business_validation_copies_hours_and_rejects_boolean_windows(self):
        source = business()
        normalized = validate_business(source)
        source["hours"][0]["end"] = 1000
        source["hours"].append({"day": 2, "start": 0, "end": 1440})
        self.assertEqual(normalized["hours"], [{"day": 1, "start": 600, "end": 720}])
        for name in ("day", "start", "end"):
            invalid = business()
            invalid["hours"][0][name] = True
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_business(invalid)
        for key in ("latitude", "longitude"):
            invalid = business()
            invalid[key] = "0"
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_business(invalid)

    def test_empty_and_wrong_type_query_options_are_rejected(self):
        for options in (
            {"category": ""},
            {"category": " "},
            {"category": True},
            {"limit": True},
            {"limit": 1.0},
            {"limit": 0},
            {"limit": 1001},
            {"open_at": ""},
            {"open_at": " "},
            {"open_at": True},
            {"open_at": "2026-10-13T10:00:00"},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.store.search(0, 0, **options)
        self.assertEqual(self.store.search(0, 0, category="cafe")["total"], 2)

    def test_utc_conversion_boundaries_and_equivalent_instant_filtering(self):
        for instant in ("0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"):
            with self.subTest(instant=instant), self.assertRaises(ValueError):
                self.store.search(0, 0, open_at=instant)
        self.assertEqual(
            self.store.search(0, 0, open_at="2026-10-13T11:00:00Z")["total"], 2
        )
        self.assertEqual(
            self.store.search(0, 0, open_at="2026-10-13T12:00:00+01:00")["total"], 2
        )
        self.assertEqual(
            self.store.search(0, 0, open_at="2026-10-13T12:00:00Z")["total"], 0
        )

    def test_malformed_and_boolean_cursor_fields_are_rejected(self):
        initial = self.store.search(0, 0, limit=1)
        cursor = initial["next_cursor"]
        token = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        for changes in (
            {"version": True},
            {"version": -1},
            {"version": 1.0},
            {"offset": True},
            {"offset": -1},
            {"offset": 1.0},
            {"query": True},
            {"lineage": False},
            {"extra": 1},
        ):
            malformed = dict(token, **changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.search(0, 0, limit=1, cursor=cursor_document(malformed))
        for value in ("", True, "A", "@@@", cursor_document([])):
            with self.subTest(cursor=value), self.assertRaises(ValueError):
                self.store.search(0, 0, limit=1, cursor=value)
        raw = '{"version":true,' + json.dumps(token)[1:]
        duplicate = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
        with self.assertRaises(ValueError):
            self.store.search(0, 0, limit=1, cursor=duplicate)

    def test_changed_cursor_query_or_generation_returns_conflict(self):
        initial = self.store.search(0, 0, limit=1)
        cursor = initial["next_cursor"]
        token = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        for changes in (
            {"version": token["version"] + 1},
            {"query": "different"},
            {"lineage": "foreign"},
        ):
            with self.subTest(changes=changes), self.assertRaises(Error) as failure:
                self.store.search(
                    0, 0, limit=1, cursor=cursor_document(dict(token, **changes))
                )
            self.assertEqual(failure.exception.status, 409)
        following = self.store.search(0, 0, limit=1, cursor=cursor)
        self.assertNotEqual(
            initial["businesses"][0]["id"], following["businesses"][0]["id"]
        )


if __name__ == "__main__":
    unittest.main()
