from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from metrics_monitor.common import Error
from metrics_monitor.storage import DAY, Store, read_archive


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = [2000001600.0]
        self.path = Path(self.temp.name) / "metrics.sqlite"
        self.store = Store(self.path, clock=lambda: self.now[0])

    def tearDown(self):
        self.temp.cleanup()

    def point(self, value, timestamp=None, metric="cpu", kind="gauge", labels=None):
        return {
            "metric": metric,
            "type": kind,
            "timestamp": self.now[0] if timestamp is None else timestamp,
            "value": value,
            "labels": labels or {"instance": "a"},
        }

    def test_durable_batch_replay_conflicts_and_atomic_sample_validation(self):
        samples = [self.point(1)]
        result = self.store.ingest("one", samples)
        self.assertEqual(
            Store(self.path, clock=lambda: self.now[0]).ingest("one", samples), result
        )
        with self.assertRaises(Error):
            self.store.ingest("one", [self.point(2)])
        with self.assertRaises(Error):
            self.store.ingest("two", [self.point(3, self.now[0] - 1), self.point(2)])
        self.assertEqual(self.store.health()["raw_samples"], 1)
        for bad in [
            self.point(float("nan")),
            self.point(-1, kind="counter"),
            self.point(1, self.now[0] + 31),
            self.point(1, self.now[0] - 8 * DAY),
        ]:
            with self.assertRaises((ValueError, Error)):
                self.store.ingest("bad", [bad])

    def test_cardinality_and_metric_type_budgets_rollback(self):
        store = Store(
            Path(self.temp.name) / "limited.sqlite",
            clock=lambda: self.now[0],
            max_series=2,
            per_metric=1,
        )
        with self.assertRaises(Error) as caught:
            store.ingest(
                "batch",
                [
                    self.point(1, labels={"instance": "a"}),
                    self.point(1, labels={"instance": "b"}),
                ],
            )
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(store.health()["series"], 0)
        store.ingest("ok", [self.point(1)])
        with self.assertRaises(Error):
            store.ingest("kind", [self.point(1, self.now[0] - 1, kind="counter")])

    def test_late_data_label_index_cache_and_order(self):
        timestamp = self.now[0] - 600
        self.store.ingest(
            "one", [self.point(10, timestamp), self.point(30, timestamp + 20)]
        )
        result = self.store.query(
            "cpu", timestamp, timestamp + 59, match={"instance": "a"}
        )
        self.assertEqual(result["series"][0]["points"][0]["value"], 20)
        self.store.query("cpu", timestamp, timestamp + 59, match={"instance": "a"})
        self.assertGreater(self.store.cache.hits, 0)
        self.store.ingest("late", [self.point(20, timestamp + 10)])
        updated = self.store.query("cpu", timestamp, timestamp + 59)
        self.assertGreater(updated["revision"], result["revision"])
        self.assertEqual(updated["series"][0]["points"][0]["count"], 3)
        self.assertEqual(
            self.store.query(
                "cpu", timestamp, timestamp + 59, match={"instance": "absent"}
            )["series"],
            [],
        )

    def test_gauge_rollup_weighted_mean_and_retention_tiers(self):
        base = self.now[0] - 600
        self.store.ingest(
            "one",
            [
                self.point(10, base),
                self.point(20, base + 10),
                self.point(90, base + 60),
            ],
        )
        self.now[0] += 8 * DAY
        result = self.store.query("cpu", base, base + 120, step=120)
        self.assertEqual(result["series"][0]["points"][0]["value"], 40)
        self.assertEqual(self.store.health()["raw_samples"], 0)
        self.now[0] += 23 * DAY
        hourly = self.store.query("cpu", base, base + 120, step=3600)
        self.assertEqual(hourly["minimum_grain"], 3600)
        self.assertTrue(hourly["series"][0]["partial_coarse_buckets"])
        self.assertEqual(hourly["series"][0]["points"][0]["value"], 40)
        self.now[0] += 335 * DAY
        self.store.compact()
        self.assertEqual(self.store.health()["series"], 0)

    def test_counter_resets_compose_across_minutes_hours(self):
        base = self.now[0] - 600
        self.store.ingest(
            "counter",
            [
                self.point(value, base + i * 30, metric="requests", kind="counter")
                for i, value in enumerate([90, 100, 5, 15])
            ],
        )
        for advance, step in [(0, 120), (8 * DAY, 120), (23 * DAY, 3600)]:
            self.now[0] += advance
            result = self.store.query(
                "requests", base, base + 119, step=step, aggregation="increase"
            )
            self.assertEqual(result["series"][0]["points"][0]["value"], 25)
            rate = self.store.query(
                "requests", base, base + 119, step=step, aggregation="rate"
            )["series"][0]["points"][0]["value"]
            self.assertAlmostEqual(rate, 25 / 90)

    def test_mixed_old_and_live_data_uses_nonoverlapping_tiers(self):
        base = self.now[0] - 600
        self.store.ingest("old", [self.point(2, base)])
        self.now[0] += 8 * DAY
        self.store.ingest("new", [self.point(8)])
        result = self.store.query("cpu", base, self.now[0], step=3600)
        self.assertEqual([p["value"] for p in result["series"][0]["points"]], [2, 8])
        self.assertEqual(sum(p["count"] for p in result["series"][0]["points"]), 2)

    def test_local_cold_archive_verifies_after_live_retention(self):
        base = self.now[0] - 600
        self.store.ingest("cold", [self.point(4, base), self.point(8, base + 20)])
        self.now[0] += 31 * DAY
        path = Path(self.temp.name) / "cold.json.gz"
        result = self.store.archive(
            path, base // 3600 * 3600, base // 3600 * 3600 + 3600
        )
        self.assertEqual(result["buckets"], 1)
        self.now[0] += 335 * DAY
        self.store.compact()
        self.assertEqual(self.store.health()["series"], 0)
        payload = read_archive(path)
        self.assertEqual(payload["buckets"][0]["stats"]["sum"], 12)
        with self.assertRaises(FileExistsError):
            self.store.archive(path, base // 3600 * 3600, base // 3600 * 3600 + 3600)


if __name__ == "__main__":
    unittest.main()
