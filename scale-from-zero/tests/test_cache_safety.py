import math
import unittest
from scale_from_zero.cache import Cache
from scale_from_zero.common import Error


class CacheSafetyTests(unittest.TestCase):
    def test_fill_ticket_is_invalid_after_delete_even_when_value_missing(self):
        cache = Cache()
        ticket = cache.probe("user")["generation"]
        cache.delete("user", 2)
        self.assertFalse(cache.put("user", {"version": 1}, ticket))
        self.assertIsNone(cache.get("user"))
        self.assertEqual(cache.probe("user")["min_version"], 2)

    def test_lagging_replica_cannot_refill_below_observed_write_version(self):
        cache = Cache()
        cache.delete("user", 2)
        ticket = cache.probe("user")["generation"]
        self.assertFalse(cache.put("user", {"version": 1}, ticket))
        self.assertTrue(cache.put("user", {"version": 2}, ticket))
        cache.delete("user", 1)
        self.assertEqual(cache.probe("user")["min_version"], 2)

    def test_same_version_content_conflict_and_lower_version_do_not_overwrite(self):
        cache = Cache()
        cache.put("user", {"version": 2, "value": "a"})
        self.assertFalse(cache.put("user", {"version": 1, "value": "b"}))
        with self.assertRaises(Error):
            cache.put("user", {"version": 2, "value": "b"})
        self.assertEqual(cache.get("user")["value"], "a")

    def test_generation_is_not_reused_after_cache_restart(self):
        old = Cache().probe("user")["generation"]
        self.assertFalse(Cache().put("user", {"version": 1}, old))

    def test_byte_budget_rejects_oversize_without_evicting_valid_entries(self):
        cache = Cache(capacity=10, max_bytes=100)
        cache.put("a", {"version": 1})
        self.assertFalse(cache.put("b", {"version": 1, "value": "x" * 1000}))
        self.assertEqual(cache.get("a"), {"version": 1})
        self.assertLessEqual(cache.status()["bytes"], 100)

    def test_byte_eviction_and_expiry_keep_accounting_exact(self):
        now = [0]
        cache = Cache(capacity=10, max_bytes=60, clock=lambda: now[0])
        for key in ("a", "b", "c"):
            cache.put(key, {"value": "x" * 15})
        self.assertEqual(cache.status()["entries"], 2)
        now[0] = 3
        cache.put("d", {"version": 1})
        self.assertEqual(cache.status()["entries"], 1)
        self.assertEqual(cache.status()["bytes"], len('{"version":1}'))

    def test_invalid_ttl_or_clock_is_rejected(self):
        for value in (True, math.inf, math.nan, 0, -1):
            with self.assertRaises(ValueError):
                Cache(ttl=value)
        with self.assertRaises(ValueError):
            Cache(clock=lambda: math.nan).probe("key")

    def test_version_floor_metadata_is_bounded(self):
        cache = Cache()
        for i in range(1001):
            cache.delete(str(i), 1)
        self.assertEqual(cache.status()["version_floors"], 1000)
