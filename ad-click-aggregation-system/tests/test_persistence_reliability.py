"""Actual SQLite durability/concurrency with explicitly controlled clock faults."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ad_click.aggregate import Aggregator
from ad_click.common import Error, digest
from ad_click.log import EventLog
from ad_click.records import prepare_batch, prepare_events


def click(event_id="one", **changes):
    return {
        "event_id": event_id,
        "ad_id": "hot",
        "click_timestamp": 601,
        "user_id": "u",
        "ip": "127.0.0.1",
        "country": "US",
        **changes,
    }


class PersistenceReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = [1000]
        self.log = EventLog(self.root / "raw.sqlite", 1, clock=lambda: self.now[0])
        self.worker = Aggregator(
            self.root / "worker.sqlite", 0, clock=lambda: self.now[0]
        )

    def test_invalid_configuration_is_rejected_before_creation(self):
        for value in (True, 0, -1, 1.5):
            with self.assertRaises(ValueError):
                EventLog(self.root / "bad.sqlite", max_events=value)
        self.assertFalse((self.root / "bad.sqlite").exists())
        with self.assertRaises(ValueError):
            Aggregator(self.root / "bad-worker.sqlite", 0, clock=None)
        self.assertFalse((self.root / "bad-worker.sqlite").exists())

    def test_acceptance_time_is_sampled_after_writer_acquisition(self):
        prepared = threading.Event()

        def normalize(bodies):
            result = prepare_events(bodies)
            prepared.set()
            return result

        with (
            patch("ad_click.log.prepare_events", side_effect=normalize),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            with self.log.transaction(True):
                pending = executor.submit(
                    self.log.append, [click(click_timestamp=1100)]
                )
                self.assertTrue(prepared.wait(1))
                self.now[0] = 2000
            pending.result(3)
        self.assertEqual(self.log.read(0, 0)["entries"][0]["accepted"], 2000)

    def test_normalization_does_not_hold_writer_and_payload_is_owned(self):
        prepared, release, written = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        body = click()

        def normalize(bodies):
            result = prepare_events(bodies)
            prepared.set()
            release.wait(3)
            return result

        def write():
            with self.log.transaction(True):
                written.set()

        with (
            patch("ad_click.log.prepare_events", side_effect=normalize),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            pending = executor.submit(self.log.append, [body])
            try:
                self.assertTrue(prepared.wait(1))
                body["ad_id"] = "changed"
                other = executor.submit(write)
                self.assertTrue(written.wait(1))
                other.result()
            finally:
                release.set()
            pending.result(3)
        self.assertEqual(self.log.read(0, 0)["entries"][0]["event"]["ad_id"], "hot")

    def test_concurrent_duplicate_appends_allocate_one_position(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(
                executor.map(lambda _: self.log.append([click()]), range(24))
            )
        self.assertEqual(self.log.status()["raw_events"], 1)
        self.assertEqual({result["receipts"][0]["seq"] for result in results}, {1})
        self.assertEqual(
            sum(not result["receipts"][0]["duplicate"] for result in results), 1
        )

    def test_exact_replay_is_allowed_after_clock_rolls_back(self):
        first = self.log.append([click(click_timestamp=900)])
        self.now[0] = 100
        repeated = self.log.append([click(click_timestamp=900)])
        self.assertEqual(first["receipts"][0]["seq"], repeated["receipts"][0]["seq"])
        self.assertTrue(repeated["receipts"][0]["duplicate"])
        with self.assertRaises(ValueError):
            self.log.append([click("new", click_timestamp=900)])

    def test_persisted_payload_corruption_is_not_returned_as_valid_source(self):
        self.log.append([click()])
        with self.log.transaction(True) as db:
            db.execute(
                "UPDATE events SET payload=?", (json.dumps(click(ad_id="changed")),)
            )
        with self.assertRaises(Error) as caught:
            self.log.read(0, 0)
        self.assertEqual(caught.exception.status, 503)

    def test_persisted_offset_hole_fails_closed(self):
        self.log.append([click("one"), click("two")])
        with self.log.transaction(True) as db:
            db.execute("DELETE FROM events WHERE seq=1")
        with self.assertRaises(Error):
            self.log.read(0, 0)

    def test_read_bounds_are_consistent_and_explicit(self):
        self.log.append([click(str(index)) for index in range(4)])
        self.assertEqual(
            [entry["seq"] for entry in self.log.read(0, 0, until=2)["entries"]], [1, 2]
        )
        self.assertEqual(self.log.read(0, 3, until=1)["entries"], [])
        with self.assertRaises(Error):
            self.log.read(0, 5)
        with self.assertRaises(ValueError):
            self.log.read(False, 0)

    def test_already_applied_position_checks_payload_even_with_valid_new_hash(self):
        self.log.append([click()])
        page = self.log.read(0, 0)
        self.worker.apply(page)
        changed = json.loads(json.dumps(page))
        changed["entries"][0]["event"] = click("replacement")
        changed["entries"][0]["hash"] = digest(changed["entries"][0]["event"])
        with self.assertRaises(Error):
            self.worker.apply(changed)
        self.assertEqual(self.worker.audit()["count"], 1)

    def test_already_applied_acceptance_time_cannot_change(self):
        self.log.append([click()])
        page = self.log.read(0, 0)
        self.worker.apply(page)
        page["entries"][0]["accepted"] += 1
        with self.assertRaises(Error):
            self.worker.apply(page)

    def test_invalid_page_structure_head_and_sequence_have_no_partial_effect(self):
        self.log.append([click("one"), click("two")])
        page = self.log.read(0, 0)
        invalid = []
        invalid.append({**page, "head": 1})
        invalid.append({**page, "entries": list(reversed(page["entries"]))})
        invalid.append({**page, "partition": False})
        invalid.append({**page, "entries": page["entries"] * 101})
        bad = json.loads(json.dumps(page))
        bad["entries"][1]["hash"] = "0" * 64
        invalid.append(bad)
        for value in invalid:
            with self.assertRaises((ValueError, Error)):
                self.worker.apply(value)
            self.assertEqual(self.worker.status()["seq"], 0)
            self.assertEqual(self.worker.audit()["count"], 0)

    def test_same_event_at_a_later_compatible_position_is_not_counted_twice(self):
        self.log.append([click()])
        page = self.log.read(0, 0)
        self.worker.apply(page)
        repeated = {**page, "head": 2, "entries": [{**page["entries"][0], "seq": 2}]}
        self.worker.apply(repeated)
        self.assertEqual(self.worker.status()["seq"], 2)
        self.assertEqual(self.worker.audit()["count"], 1)

    def test_conflicting_event_rolls_back_the_whole_new_page(self):
        self.log.append([click()])
        original = self.log.read(0, 0)
        self.worker.apply(original)
        other = click("other")
        conflicting = click(ad_id="changed")
        page = {
            **original,
            "head": 3,
            "entries": [
                {"seq": 2, "event": other, "hash": digest(other), "accepted": 1000},
                {
                    "seq": 3,
                    "event": conflicting,
                    "hash": digest(conflicting),
                    "accepted": 1000,
                },
            ],
        }
        with self.assertRaises(Error):
            self.worker.apply(page)
        self.assertEqual(
            (self.worker.status()["seq"], self.worker.audit()["count"]), (1, 1)
        )

    def test_batch_mutation_after_capture_does_not_change_committed_result(self):
        self.log.append([click()])
        page = self.log.read(0, 0)
        captured = threading.Event()

        def prepare(body, partition):
            result = prepare_batch(body, partition)
            captured.set()
            return result

        with (
            patch("ad_click.aggregate.prepare_batch", side_effect=prepare),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            with self.worker.transaction(True):
                pending = executor.submit(self.worker.apply, page)
                self.assertTrue(captured.wait(1))
                page["entries"][0]["event"]["ad_id"] = "changed"
            pending.result(3)
        self.assertEqual(
            self.worker.totals({"from": 600, "to": 660})["totals"], {"hot": 1}
        )

    def test_concurrent_apply_of_same_page_counts_each_event_once(self):
        self.log.append([click(str(index)) for index in range(40)])
        page = self.log.read(0, 0)
        with ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(lambda _: self.worker.apply(page), range(12)))
        self.assertEqual(
            (self.worker.status()["seq"], self.worker.audit()["count"]), (40, 40)
        )

    def test_legacy_migration_replays_seen_identity_and_guards_new_positions(self):
        self.log.append([click("one")])
        self.worker.apply(self.log.read(0, 0))
        with self.worker.transaction(True) as db:
            db.execute("DROP TABLE input_positions")
            db.execute("DROP TABLE worker_meta")
        reopened = Aggregator(self.worker.path, 0, clock=lambda: self.now[0])
        reopened.apply(self.log.read(0, 0))
        self.log.append([click("two")])
        reopened.apply(self.log.read(0, 1))
        with reopened.transaction() as db:
            self.assertEqual(
                db.execute("SELECT seq FROM input_positions").fetchone()[0], 2
            )
        self.assertEqual(reopened.audit()["count"], 2)

    def test_streaming_audit_matches_independent_canonical_array_hash(self):
        self.log.append([click("one", ad_id="广告"), click("two", ad_id="other")])
        self.worker.apply(self.log.read(0, 0))
        with self.worker.transaction() as db:
            rows = [
                list(row)
                for row in db.execute(
                    "SELECT minute,ad_id,user_id,ip,country,count FROM buckets ORDER BY minute,ad_id,user_id,ip,country"
                )
            ]
        self.assertEqual(self.worker.audit()["digest"], digest(rows))
        with self.assertRaises(Error):
            self.worker.audit(max_rows=1)

    def test_idle_requires_lineage_and_never_moves_watermark_back(self):
        self.worker.idle()
        self.assertEqual(self.worker.status()["watermark"], 0)
        self.worker.apply(self.log.read(0, 0))
        self.worker.idle()
        watermark = self.worker.status()["watermark"]
        self.now[0] = 100
        self.worker.idle()
        self.assertEqual(self.worker.status()["watermark"], watermark)


if __name__ == "__main__":
    unittest.main()
