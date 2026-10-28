"""Publication fences, immutable replication, and background lifecycle tests."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proximity.publisher import DailyPublisher
from proximity.store import Error, Store, canonical


def business(name="Cafe"):
    return {
        "name": name,
        "latitude": 0.0,
        "longitude": 0.0,
        "category": "cafe",
        "hours": [],
        "address": "",
    }


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "primary.sqlite"
        self.now = [datetime(2026, 10, 13, 12, tzinfo=timezone.utc).timestamp()]
        self.store = Store(self.path, clock=lambda: self.now[0])
        self.identifier = self.store.mutate(
            "alice", "POST", None, business(), "create"
        )["id"]
        self.store.publish(force=True)

    def update_draft(self, name):
        # A short independent SQLite writer proves encoding does not own the
        # writer lock. Triggers must also fence maintenance tools' direct edits.
        with closing(sqlite3.connect(self.path, timeout=0.2)) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE drafts SET body=? WHERE id=?",
                (canonical(business(name)), self.identifier),
            )
            db.commit()

    def test_offline_build_retries_changed_draft_revision(self):
        prepare = self.store._prepare
        calls = []

        def changed(businesses):
            calls.append(1)
            if len(calls) == 1:
                self.update_draft("Edited during construction")
            return prepare(businesses)

        with patch.object(self.store, "_prepare", side_effect=changed):
            result = self.store.publish(force=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["version"], 2)
        self.assertEqual(
            self.store.detail(self.identifier)["business"]["name"],
            "Edited during construction",
        )

    def test_continuous_source_changes_are_bounded_and_leave_old_generation(self):
        prepare = self.store._prepare
        calls = []

        def changed(businesses):
            calls.append(1)
            self.update_draft("Change " + str(len(calls)))
            return prepare(businesses)

        with patch.object(self.store, "_prepare", side_effect=changed):
            with self.assertRaises(Error) as caught:
                self.store.publish(force=True)
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.store.health()["version"], 1)
        self.assertEqual(self.store.detail(self.identifier)["business"]["name"], "Cafe")
        self.store.publish(force=True)
        self.assertEqual(
            self.store.detail(self.identifier)["business"]["name"], "Change 3"
        )

    def test_another_store_publishing_the_same_day_wins_without_duplicate_version(self):
        other = Store(self.path, clock=lambda: self.now[0])
        self.now[0] += 86400
        prepare = self.store._prepare
        winner = []

        def competitor(businesses):
            self.update_draft("Competing publisher")
            winner.append(other.publish())
            return prepare(businesses)

        with patch.object(self.store, "_prepare", side_effect=competitor):
            result = self.store.publish()
        self.assertEqual(result, winner[0])
        self.assertEqual(result["version"], 2)
        self.assertEqual(
            self.store.detail(self.identifier)["business"]["name"],
            "Competing publisher",
        )

    def test_midnight_during_build_recaptures_target_day(self):
        prepare = self.store._prepare
        calls = []

        def advance(businesses):
            calls.append(1)
            if len(calls) == 1:
                self.now[0] += 86400
            return prepare(businesses)

        with patch.object(self.store, "_prepare", side_effect=advance):
            result = self.store.publish(force=True)
        self.assertEqual(result["day"], "2026-10-14")
        self.assertEqual(len(calls), 2)

    def test_readers_serve_old_generation_while_due_build_runs(self):
        self.now[0] += 86400
        self.update_draft("Tomorrow")
        entered, release = threading.Event(), threading.Event()
        prepare = self.store._prepare

        def slow(businesses):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test did not release publication")
            return prepare(businesses)

        with ThreadPoolExecutor(max_workers=2) as executor:
            with patch.object(self.store, "_prepare", side_effect=slow):
                publication = executor.submit(self.store.publish)
                try:
                    self.assertTrue(entered.wait(2))
                    detail = executor.submit(self.store.detail, self.identifier)
                    self.assertEqual(
                        detail.result(timeout=1)["business"]["name"], "Cafe"
                    )
                    result = executor.submit(self.store.search, 0, 0).result(timeout=1)
                    self.assertEqual(result["version"], 1)
                    self.assertEqual(result["businesses"][0]["name"], "Cafe")
                finally:
                    release.set()
                self.assertEqual(publication.result(timeout=2)["version"], 2)
        self.assertEqual(
            self.store.detail(self.identifier)["business"]["name"], "Tomorrow"
        )

    def test_preparation_and_install_failure_preserve_generation(self):
        before = self.store.export()
        self.update_draft("Pending")
        with patch.object(self.store, "_prepare", side_effect=RuntimeError("encode")):
            with self.assertRaises(RuntimeError):
                self.store.publish(force=True)
        self.assertEqual(self.store.export(), before)

        def fail():
            raise RuntimeError("install")

        with self.assertRaises(RuntimeError):
            self.store.publish(force=True, fail_hook=fail)
        self.assertEqual(self.store.export(), before)

    def test_replica_rejects_mutation_of_an_existing_generation(self):
        replica = Store(Path(self.temp.name) / "replica.sqlite", readonly=True)
        snapshot = self.store.export()
        self.assertTrue(replica.import_snapshot(snapshot))
        replica.detail(self.identifier)  # Prime the generation-keyed cache.
        changed = json.loads(json.dumps(snapshot))
        changed["businesses"][0]["name"] = "Changed without a new version"
        for attempt in (changed, dict(snapshot, day="2026-10-14")):
            with self.assertRaises(Error) as caught:
                replica.import_snapshot(attempt)
            self.assertEqual(caught.exception.status, 409)
        self.assertEqual(replica.detail(self.identifier)["business"]["name"], "Cafe")
        self.assertFalse(replica.import_snapshot(snapshot))

    def test_invalid_replica_imports_are_atomic_and_cannot_initialize(self):
        replica = Store(Path(self.temp.name) / "replica.sqlite", readonly=True)
        snapshot = self.store.export()
        invalid = [
            dict(snapshot, version=True),
            dict(snapshot, version=-1),
            dict(snapshot, version=2**63),
            dict(snapshot, day="2026-1-1"),
            dict(snapshot, day="2026-02-30"),
            dict(snapshot, lineage=snapshot["lineage"].upper()),
            dict(snapshot, unknown=True),
            dict(snapshot, businesses=[dict(snapshot["businesses"][0], id="bad/id")]),
            dict(snapshot, businesses=[dict(snapshot["businesses"][0], latitude=True)]),
        ]
        for attempt in invalid:
            with self.subTest(attempt=attempt):
                with self.assertRaises(ValueError):
                    replica.import_snapshot(attempt)
                self.assertFalse(replica.health()["ready"])
        duplicate = dict(snapshot, businesses=snapshot["businesses"] * 2)
        with self.assertRaises(sqlite3.IntegrityError):
            replica.import_snapshot(duplicate)
        self.assertFalse(replica.health()["ready"])
        self.assertTrue(replica.import_snapshot(snapshot))
        self.assertEqual(replica.search(0, 0)["total"], 1)

    def test_replica_rollbacks_and_foreign_lineage_leave_content_unchanged(self):
        replica = Store(Path(self.temp.name) / "replica.sqlite", readonly=True)
        snapshot = self.store.export()
        replica.import_snapshot(snapshot)
        for attempt in (
            dict(snapshot, version=0),
            dict(snapshot, version=2, day="2026-10-12"),
            dict(snapshot, version=2, lineage=str(uuid.uuid4())),
        ):
            with self.assertRaises(Error) as caught:
                replica.import_snapshot(attempt)
            self.assertEqual(caught.exception.status, 409)
        self.assertEqual(replica.export(), snapshot)

    def test_replica_import_owns_validated_header_during_preparation(self):
        replica = Store(Path(self.temp.name) / "replica.sqlite", readonly=True)
        snapshot = self.store.export()
        expected = json.loads(json.dumps(snapshot))
        prepare = replica._prepare

        def caller_reuses_dictionary(businesses):
            snapshot.update(version="invalid", day="invalid", lineage="invalid")
            return prepare(businesses)

        with patch.object(replica, "_prepare", side_effect=caller_reuses_dictionary):
            self.assertTrue(replica.import_snapshot(snapshot))
        self.assertEqual(replica.export(), expected)

    def test_legacy_five_table_database_migrates_without_losing_generation(self):
        # Remove only new metadata/triggers to emulate a pre-review database.
        before = self.store.export()
        with self.store.transaction(True) as db:
            db.execute(
                "DELETE FROM meta WHERE key IN ('draft_revision','content_hash')"
            )
            for action in ("insert", "update", "delete"):
                db.execute("DROP TRIGGER drafts_" + action + "_revision")
        restarted = Store(self.path, shards=19, clock=lambda: self.now[0])
        self.assertEqual(restarted.shards, 4)
        self.assertEqual(restarted.export(), before)
        with restarted.transaction() as db:
            metadata = restarted.metadata(db)
            self.assertEqual(int(metadata["draft_revision"]), 0)
            self.assertEqual(len(metadata["content_hash"]), 64)
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertEqual(tables, {"meta", "drafts", "public", "geo", "replay"})
        with restarted.transaction(True) as db:
            db.execute(
                "INSERT INTO drafts VALUES('temporary','alice',?,0)",
                (canonical(business()),),
            )
            db.execute("UPDATE drafts SET deleted=1 WHERE id='temporary'")
            db.execute("DELETE FROM drafts WHERE id='temporary'")
            self.assertEqual(int(restarted.metadata(db)["draft_revision"]), 3)

    def test_publication_configuration_is_strict(self):
        for options in (
            {"force": 1},
            {"wait": 0},
            {"force": True, "wait": False},
            {"fail_hook": 1},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.store.publish(**options)
        for value in (-1, float("nan"), True, "0"):
            with patch.object(self.store, "clock", return_value=value):
                with self.assertRaises(ValueError):
                    self.store.day()


class DailyPublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = [datetime(2026, 10, 13, 12, tzinfo=timezone.utc).timestamp()]
        self.store = Store(
            Path(self.temp.name) / "primary.sqlite", clock=lambda: self.now[0]
        )

    def test_background_publishes_without_read_traffic(self):
        self.store.mutate("alice", "POST", None, business(), "create")
        self.now[0] += 86400
        published = threading.Event()
        real = self.store.publish

        def observe():
            result = real()
            published.set()
            return result

        publisher = DailyPublisher(self.store)
        with patch.object(self.store, "publish", side_effect=observe):
            publisher.start()
            try:
                self.assertTrue(published.wait(2))
                self.assertEqual(self.store.health()["businesses"], 1)
                self.assertEqual(self.store.health()["day"], "2026-10-14")
            finally:
                publisher.close()
        self.assertFalse(publisher.thread.is_alive())
        self.assertFalse(publisher.thread.daemon)

    def test_close_drains_work_in_progress(self):
        entered, release, closed = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        def slow():
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test did not release publisher")
            return {"version": 0}

        publisher = DailyPublisher(self.store)
        with patch.object(self.store, "publish", side_effect=slow):
            publisher.start()
            self.assertTrue(entered.wait(2))

            def close():
                publisher.close()
                closed.set()

            closer = threading.Thread(target=close)
            closer.start()
            try:
                self.assertFalse(closed.wait(0.05))
            finally:
                release.set()
                closer.join(2)
            self.assertTrue(closed.is_set())
            self.assertFalse(publisher.thread.is_alive())

    def test_periodic_failure_keeps_retrying_then_clears_error(self):
        attempts, completed = [], threading.Event()
        publisher = DailyPublisher(self.store)

        def publish():
            attempts.append(1)
            if len(attempts) == 1:
                raise sqlite3.OperationalError("transient")
            completed.set()
            publisher.stop.set()
            return {"version": 0}

        with patch.object(self.store, "publish", side_effect=publish):
            with self.assertLogs("proximity.publisher", level="ERROR"):
                publisher.start()
                try:
                    self.assertTrue(completed.wait(3))
                finally:
                    publisher.close()
        self.assertEqual(len(attempts), 2)
        self.assertIsNone(publisher.last_error)
        self.assertEqual(publisher.last_snapshot, {"version": 0})

    def test_interval_role_and_owned_thread_configuration(self):
        for value in (True, 0, 0.5, 86401, float("inf"), float("nan"), "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                DailyPublisher(self.store, interval=value)
        replica = Store(Path(self.temp.name) / "replica.sqlite", readonly=True)
        with self.assertRaises(ValueError):
            DailyPublisher(replica)
        publisher = DailyPublisher(self.store, interval=86400)
        self.assertEqual(publisher._delay(), 43200)
        publisher.close()  # Safe before start and on repeated cleanup.
        publisher.close()


if __name__ == "__main__":
    unittest.main()
