"""Real SQLite/file checks; clocks and blocked CPU stages are controlled faults."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import gzip
import json
from pathlib import Path
import random
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from metrics_monitor.cache import Cache
from metrics_monitor.common import Error, digest, dumps
from metrics_monitor.storage import DAY, Store, read_archive
from metrics_monitor import rollups


class StorageReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = [2000001600.0]
        self.path = Path(self.directory.name) / "metrics.sqlite"
        self.store = Store(self.path, clock=lambda: self.now[0])

    def point(self, value, timestamp=None, instance="a", kind="gauge", zone="west"):
        return {
            "metric": "cpu",
            "type": kind,
            "timestamp": self.now[0] - 600 if timestamp is None else timestamp,
            "value": value,
            "labels": {"instance": instance, "zone": zone},
        }

    def query(self, **options):
        return self.store.query("cpu", self.now[0] - 601, self.now[0] - 540, **options)

    def test_cache_results_and_loader_inputs_are_owned(self):
        cache = Cache()
        source = {"nested": [{"value": 1}]}
        result = cache.get("x", lambda: source)
        result["nested"][0]["value"] = 3
        source["nested"].append(2)
        self.assertEqual(
            cache.get("x", lambda: self.fail("unexpected load")),
            {"nested": [{"value": 1}]},
        )
        cache.clear()
        self.assertEqual(cache.get("x", lambda: [2]), [2])

    def test_cache_cold_loads_coalesce_without_global_io_lock(self):
        cache = Cache()
        entered, release, independent = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        calls = []

        def loader():
            calls.append(1)
            entered.set()
            release.wait(2)
            return {"safe": True}

        first_key = "one"
        second_key = next(
            str(i) for i in range(1000) if hash(str(i)) % 64 != hash(first_key) % 64
        )
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = [executor.submit(cache.get, first_key, loader) for _ in range(5)]
            try:
                self.assertTrue(entered.wait(1))
                other = executor.submit(
                    lambda: (cache.get(second_key, lambda: 2), independent.set())
                )
                self.assertTrue(independent.wait(1))
                other.result()
            finally:
                release.set()
            self.assertEqual([f.result() for f in results], [{"safe": True}] * 5)
        self.assertEqual(len(calls), 1)

    def test_cache_failure_recovers_and_capacity_is_bounded(self):
        cache = Cache(1)
        with self.assertRaises(OSError):
            cache.get("x", lambda: (_ for _ in ()).throw(OSError("controlled")))
        self.assertEqual(cache.get("x", lambda: None), None)
        cache.get("y", lambda: 2)
        self.assertEqual(list(cache.items), ["y"])
        empty = Cache(0)
        empty.get("x", lambda: 1)
        self.assertFalse(empty.items)

    def test_ingest_freezes_input_before_waiting_for_writer(self):
        original = [self.point(1)]
        entered, release = threading.Event(), threading.Event()
        transaction = self.store.transaction

        @contextmanager
        def gated(write=False):
            if write:
                entered.set()
                release.wait(2)
            with transaction(write) as db:
                yield db

        with (
            patch.object(self.store, "transaction", gated),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            pending = executor.submit(self.store.ingest, "one", original)
            try:
                self.assertTrue(entered.wait(1))
                original[0]["value"] = 9
                original[0]["labels"]["zone"] = "east"
            finally:
                release.set()
            pending.result()
        found = self.query()["series"][0]
        self.assertEqual(found["labels"]["zone"], "west")
        self.assertEqual(found["points"][0]["value"], 1)
        self.store.ingest("one", [self.point(1)])

    def test_ingest_samples_clock_after_writer_acquisition(self):
        points = [self.point(1)]
        transaction = self.store.transaction

        @contextmanager
        def delayed(write=False):
            if write:
                self.now[0] += 8 * DAY
            with transaction(write) as db:
                yield db

        with patch.object(self.store, "transaction", delayed):
            with self.assertRaises(ValueError):
                self.store.ingest("old", points)
        self.assertEqual(self.store.health()["raw_samples"], 0)

    def test_query_nested_mutation_does_not_poison_cache(self):
        self.store.ingest("one", [self.point(2)])
        result = self.query()
        result["series"][0]["points"][0]["value"] = 99
        result["series"][0]["labels"]["zone"] = "bad"
        again = self.query()
        self.assertEqual(again["series"][0]["points"][0]["value"], 2)
        self.assertEqual(again["series"][0]["labels"]["zone"], "west")

    def test_query_retries_late_sample_between_compaction_and_snapshot(self):
        base = self.now[0] - 600
        self.store.ingest("one", [self.point(10, base)])
        compact = self.store.compact
        calls = [0]

        def racing():
            calls[0] += 1
            compact()
            if calls[0] == 1:
                self.store.ingest("late", [self.point(30, base + 10)])

        with patch.object(self.store, "compact", racing):
            found = self.store.query("cpu", base, base + 59)
        self.assertEqual(calls[0], 2)
        self.assertEqual(found["series"][0]["points"][0]["count"], 2)

    def test_compaction_aggregation_does_not_hold_writer(self):
        self.store.ingest("one", [self.point(1)])
        entered, release, ingested = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        combine = rollups.combine
        calls = [0]

        def blocked(*args):
            calls[0] += 1
            if calls[0] == 1:
                entered.set()
                release.wait(2)
            return combine(*args)

        other = Store(self.path, clock=lambda: self.now[0])
        with (
            patch("metrics_monitor.rollups.combine", blocked),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            compacting = executor.submit(self.store.compact)
            try:
                self.assertTrue(entered.wait(1))
                future = executor.submit(
                    lambda: (
                        other.ingest("two", [self.point(3, self.now[0] - 590)]),
                        ingested.set(),
                    )
                )
                self.assertTrue(ingested.wait(1))
                future.result()
            finally:
                release.set()
            compacting.result()
        found = self.query()["series"][0]["points"][0]
        self.assertEqual((found["value"], found["count"]), (2, 2))

    def test_incremental_rollup_only_rebuilds_changed_bucket(self):
        base = self.now[0] - 4200
        self.store.ingest("one", [self.point(1, base), self.point(5, base + 120)])
        self.store.compact()
        self.store.ingest("late", [self.point(3, base + 10)])
        with patch(
            "metrics_monitor.rollups.combine", wraps=rollups.combine
        ) as combined:
            self.store.compact()
        # One minute and its containing hour, not every retained raw bucket.
        self.assertEqual(combined.call_count, 2)
        result = self.store.query("cpu", base, base + 179)
        self.assertEqual([p["value"] for p in result["series"][0]["points"]], [2, 5])

    def test_compaction_budget_failure_leaves_source_and_revision_unchanged(self):
        self.store.ingest("one", [self.point(1), self.point(2, self.now[0] - 599)])
        before = self.store.health()
        with patch("metrics_monitor.rollups.MAX_ROLLUP_ATOMS", 1):
            with self.assertRaises(Error) as caught:
                self.store.compact()
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.store.health(), before)
        self.store.compact()
        self.assertEqual(self.query()["series"][0]["points"][0]["count"], 2)

    def test_existing_database_migrates_rollup_work_without_changing_receipt(self):
        points = [self.point(7)]
        receipt = self.store.ingest("one", points)
        with self.store.transaction(True) as db:
            db.execute("DROP TABLE dirty_minutes")
            db.execute("DROP TABLE dirty_hours")
            db.execute("DELETE FROM meta WHERE key='incremental_rollups'")
        reopened = Store(self.path, clock=lambda: self.now[0])
        self.assertEqual(reopened.ingest("one", points), receipt)
        self.assertEqual(
            reopened.query("cpu", self.now[0] - 601, self.now[0] - 540)["series"][0][
                "points"
            ][0]["value"],
            7,
        )

    def test_raw_minute_hour_seams_do_not_lose_or_double_count(self):
        base = self.now[0]
        self.store.ingest(
            "one",
            [self.point(2, base - 30), self.point(4, base), self.point(6, base + 20)],
        )
        for advance, step in [(7 * DAY, 60), (23 * DAY, 3600)]:
            self.now[0] += advance
            found = self.store.query("cpu", base - 60, base + 30, step=step)
            self.assertEqual(sum(p["count"] for p in found["series"][0]["points"]), 3)

    def test_counter_random_resets_match_independent_oracle_across_tiers(self):
        generator = random.Random(21)
        values = [generator.randrange(0, 50) for _ in range(80)]
        base = self.now[0] - 3590
        self.store.ingest(
            "counter",
            [
                self.point(value, base + i * 20, kind="counter")
                for i, value in enumerate(values)
            ],
        )
        expected = sum(
            current - previous if current >= previous else current
            for previous, current in zip(values, values[1:])
        )
        for advance in (0, 8 * DAY, 23 * DAY):
            self.now[0] += advance
            found = self.store.query(
                "cpu", base, base + 1599, step=86400, aggregation="increase"
            )
            self.assertEqual(found["series"][0]["points"][0]["value"], expected)

    def test_grouped_gauge_average_is_weighted_by_actual_samples(self):
        base = self.now[0] - 600
        self.store.ingest(
            "one",
            [
                self.point(0, base),
                self.point(0, base + 10),
                self.point(90, base, instance="b"),
            ],
        )
        found = self.query(group_by=[])["series"][0]
        self.assertEqual(found["labels"], {})
        self.assertEqual(found["source_series"], 2)
        self.assertEqual(found["points"][0]["value"], 30)
        self.assertEqual(found["points"][0]["count"], 3)
        self.assertEqual(len(self.query()["series"]), 2)

    def test_group_labels_and_minmax_are_explicit(self):
        self.store.ingest(
            "one",
            [
                self.point(2),
                self.point(8, instance="b"),
                self.point(20, instance="c", zone="east"),
            ],
        )
        found = self.query(group_by=["zone"], aggregation="max")
        by_zone = {
            s["labels"]["zone"]: s["points"][0]["value"] for s in found["series"]
        }
        self.assertEqual(by_zone, {"west": 8, "east": 20})
        self.assertEqual(
            self.query(group_by=[], aggregation="min")["series"][0]["points"][0][
                "value"
            ],
            2,
        )

    def test_group_counter_last_and_invalid_labels_are_rejected(self):
        for group in (["zone", "zone"], "zone", [1], ["bad label"]):
            with self.assertRaises(ValueError):
                self.query(group_by=group)
        with self.assertRaises(ValueError):
            self.query(group_by=[], aggregation="last")
        self.store.ingest("counter", [self.point(1, kind="counter")])
        with self.assertRaises(ValueError):
            self.query(group_by=[])

    def test_global_query_atom_budget_is_enforced_across_series(self):
        base = self.now[0] - 600
        self.store.ingest(
            "one",
            [
                self.point(i, base + i, instance=instance)
                for instance in ("a", "b")
                for i in range(3)
            ],
        )
        with patch("metrics_monitor.queries.MAX_QUERY_ATOMS", 5):
            with self.assertRaises(Error) as caught:
                self.query()
        self.assertEqual(caught.exception.status, 413)

    def cold_archive(self):
        base = self.now[0] - 600
        self.store.ingest("cold", [self.point(3, base), self.point(9, base + 10)])
        self.now[0] += 31 * DAY
        path = Path(self.directory.name) / "cold.gz"
        self.store.archive(path, base // 3600 * 3600, base // 3600 * 3600 + 3600)
        return path

    def test_archive_checks_checksum_and_structure_even_with_new_checksum(self):
        path = self.cold_archive()
        raw = json.loads(gzip.decompress(path.read_bytes()))
        raw["payload"]["buckets"][0]["series"] = "unknown"
        raw["sha256"] = digest(raw["payload"])
        path.write_bytes(gzip.compress(dumps(raw).encode()))
        with self.assertRaises(ValueError):
            read_archive(path)

    def test_archive_rejects_duplicate_buckets_and_invalid_statistics(self):
        path = self.cold_archive()
        raw = json.loads(gzip.decompress(path.read_bytes()))
        original = json.loads(dumps(raw))
        raw["payload"]["buckets"].append(raw["payload"]["buckets"][0])
        raw["sha256"] = digest(raw["payload"])
        path.write_bytes(gzip.compress(dumps(raw).encode()))
        with self.assertRaises(ValueError):
            read_archive(path)
        original["payload"]["buckets"][0]["stats"]["count"] = -1
        original["sha256"] = digest(original["payload"])
        path.write_bytes(gzip.compress(dumps(original).encode()))
        with self.assertRaises(ValueError):
            read_archive(path)

    def test_archive_read_checks_decoded_budget(self):
        path = Path(self.directory.name) / "bomb.gz"
        path.write_bytes(gzip.compress(b"x" * 1000))
        with patch("metrics_monitor.archives.MAX_ARCHIVE_BYTES", 100):
            with self.assertRaises(ValueError):
                read_archive(path)

    def test_archive_write_failure_removes_only_new_file(self):
        path = self.cold_archive()
        raw = read_archive(path)
        failed = Path(self.directory.name) / "failed.gz"
        with patch(
            "metrics_monitor.archives.os.fsync",
            side_effect=OSError("controlled flush failure"),
        ):
            with self.assertRaises(OSError):
                self.store.archive(failed, raw["start"], raw["end"])
        self.assertFalse(failed.exists())
        before = path.read_bytes()
        with self.assertRaises(FileExistsError):
            self.store.archive(path, raw["start"], raw["end"])
        self.assertEqual(path.read_bytes(), before)

    def test_archive_range_and_bucket_budget_fail_before_file_creation(self):
        path = self.cold_archive()
        raw = read_archive(path)
        failed = Path(self.directory.name) / "budget.gz"
        with self.assertRaises(ValueError):
            self.store.archive(failed, raw["start"] + 1, raw["end"])
        with patch("metrics_monitor.archives.MAX_ARCHIVE_BUCKETS", 0):
            with self.assertRaises(Error):
                self.store.archive(failed, raw["start"], raw["end"])
        self.assertFalse(failed.exists())

    def test_invalid_clock_and_query_options_are_rejected(self):
        self.now[0] = -1
        with self.assertRaises(ValueError):
            self.store.ingest("one", [self.point(1)])
        self.now[0] = 2000001600
        for options in ({"match": []}, {"step": True}, {"aggregation": "unknown"}):
            with self.assertRaises(ValueError):
                self.query(**options)


if __name__ == "__main__":
    unittest.main()
