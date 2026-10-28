"""Authority invariants under writer waits, restart and malformed history."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from test_journal import stamp
from gaming_leaderboard.common import Error, digest, dumps, month_bounds, shard_for
from gaming_leaderboard.journal import Journal


class JournalReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = stamp("2030-01-10T12:00:00")
        self.store = Journal(self.root / "journal.sqlite", clock=lambda: self.now)
        self.store.register("alice", "Alice")
        self.store.register("bob", "Bob")

    def body(self, match="match", **changes):
        return {
            "match_id": match,
            "winner": "alice",
            "players": ["alice", "bob"],
            "ended_at": self.now,
            **changes,
        }

    def wait_for_writer(self, action, change):
        original = self.store.transaction
        waiting = threading.Event()
        owner = threading.get_ident()

        @contextmanager
        def transaction(write=False):
            if write and threading.get_ident() != owner:
                waiting.set()
            with original(write) as db:
                yield db

        self.store.transaction = transaction
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                with original(True):
                    result = pool.submit(action)
                    self.assertTrue(waiting.wait(2))
                    change()
                return result.result(timeout=3)
        finally:
            self.store.transaction = original

    def test_match_body_is_frozen_before_waiting_for_writer(self):
        original = self.body()
        body = {**original, "players": list(original["players"])}

        def mutate():
            body.update(winner="bob", ended_at=0)
            body["players"][:] = ["unknown", "bob"]

        receipt = self.wait_for_writer(lambda: self.store.ingest(body), mutate)
        self.assertEqual(receipt["winner"], "alice")
        self.assertTrue(self.store.ingest(original)["duplicate"])
        self.assertEqual(self.store.rows("2030-01")[0]["score"], 1)

    def test_ingest_clock_is_read_once_after_writer_acquisition(self):
        calls = []

        def clock():
            calls.append(self.now)
            return self.now

        self.store.clock = clock
        self.store.ingest(self.body())
        self.assertEqual(calls, [self.now])

    def test_waiting_ingest_observes_new_deadline_not_admission_time(self):
        body = self.body()
        deadline = month_bounds("2030-01")[1] + self.store.grace
        with self.assertRaises(Error) as caught:
            self.wait_for_writer(
                lambda: self.store.ingest(body), lambda: setattr(self, "now", deadline)
            )
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.store.status()["matches"], 0)

    def test_default_season_is_chosen_after_writer_wait(self):
        self.now = stamp("2030-01-31T23:59:59")
        snapshot = self.wait_for_writer(
            self.store.snapshot,
            lambda: setattr(self, "now", stamp("2030-02-01T00:00:00")),
        )
        self.assertEqual(snapshot["season"]["month"], "2030-02")
        self.assertEqual({row["score"] for row in self.store.rows("2030-02")}, {0})
        self.assertEqual(snapshot["heads"], self.store.status()["heads"])

    def test_register_uses_one_clock_and_keeps_original_registration(self):
        calls = []

        def clock():
            calls.append(self.now)
            return self.now

        self.store.clock = clock
        self.now += 50
        self.store.register("alice", "Renamed")
        self.assertEqual(len(calls), 1)
        with self.store.transaction() as db:
            registered = db.execute(
                "SELECT registered FROM players WHERE id='alice'"
            ).fetchone()[0]
        self.assertEqual(registered, self.now - 50)

    def test_profile_revision_changes_only_for_new_or_changed_name(self):
        first = self.store.snapshot()
        self.store.register("alice", "Alice")
        unchanged = self.store.snapshot()
        self.assertEqual(first, unchanged)
        self.store.register("alice", "Updated")
        current = self.store.read_snapshot()
        self.assertEqual(current["profile_revision"], first["profile_revision"] + 1)
        self.assertEqual(current["heads"], first["heads"])
        self.assertEqual(self.store.names(["alice", "alice"]), {"alice": "Updated"})

    def test_read_snapshot_never_initializes_or_acquires_write_transaction(self):
        self.now = stamp("2030-02-01T00:00:00")
        calls = []
        original = self.store.transaction

        @contextmanager
        def transaction(write=False):
            calls.append(write)
            with original(write) as db:
                yield db

        self.store.transaction = transaction
        self.assertIsNone(self.store.read_snapshot())
        self.assertEqual(calls, [False])
        self.store.transaction = original
        prepared = self.store.snapshot()
        self.assertEqual(self.store.read_snapshot(), prepared)
        self.assertIsNone(self.store.read_snapshot("2029-12"))

    def test_invalid_explicit_month_does_not_silently_default(self):
        for month in ("", False, 0, "2030-13", "2030-1"):
            for method in (
                self.store.month,
                self.store.snapshot,
                self.store.read_snapshot,
            ):
                with (
                    self.subTest(month=month, method=method.__name__),
                    self.assertRaises(ValueError),
                ):
                    method(month)

    def test_invalid_clock_rolls_back_registration_and_matches(self):
        heads = self.store.status()["heads"]
        for value in (float("nan"), float("inf"), True, -1, "2030"):
            self.store.clock = lambda: value
            with self.assertRaises(ValueError):
                self.store.register("new", "New")
            with self.assertRaises(ValueError):
                self.store.ingest(self.body())
        self.assertEqual(self.store.status()["heads"], heads)
        self.assertEqual(self.store.status()["matches"], 0)

    def test_duplicate_receipt_remains_available_without_fresh_clock(self):
        body = self.body()
        receipt = self.store.ingest(body)
        self.store.clock = lambda: float("nan")
        again = self.store.ingest({**body, "players": list(reversed(body["players"]))})
        self.assertEqual(again["heads"], receipt["heads"])
        self.assertTrue(again["duplicate"])

    def test_display_name_controls_and_invalid_unicode_are_rejected(self):
        for name in ("", "x" * 129, "new\x00name", "new\nname", "\ud800", 12):
            with self.subTest(name=repr(name)), self.assertRaises(ValueError):
                self.store.register("new", name)
        self.assertEqual(self.store.names(["alice"]), {"alice": "Alice"})

    def test_names_are_bounded_validated_and_missing_user_is_404(self):
        self.assertEqual(self.store.names([]), {})
        for users in ("alice", [True], ["bad id"], ["a"] * 10001):
            with self.assertRaises(ValueError):
                self.store.names(users)
        with self.assertRaises(Error) as caught:
            self.store.names(["missing"])
        self.assertEqual(caught.exception.status, 404)

    def test_invalid_timestamps_and_mutable_shapes_are_validation_errors(self):
        for body in (
            self.body(ended_at=2**53),
            self.body(ended_at=True),
            self.body(players=["alice", {}]),
            self.body(players="alice,bob"),
        ):
            with self.assertRaises(ValueError):
                self.store.ingest(body)
        self.assertEqual(self.store.status()["matches"], 0)

    def test_new_season_batches_zero_members_and_contiguous_feed_pages(self):
        users = [
            "u" + str(index)
            for index in range(1000)
            if shard_for("u" + str(index), self.store.shards) == 0
        ][:210]
        for user in users:
            self.store.register(user, "User")
        previous = self.store.status()["heads"]
        self.now = stamp("2030-02-01T00:00:00")
        prepared = self.store.snapshot()
        events, pages = [], []
        for shard in range(self.store.shards):
            after = previous[str(shard)]
            while after < prepared["heads"][str(shard)]:
                page = self.store.feed(shard, after)
                self.assertEqual(
                    [e["seq"] for e in page["events"]],
                    list(range(after + 1, after + 1 + len(page["events"]))),
                )
                events.extend(page["events"])
                pages.append(len(page["events"]))
                after = page["events"][-1]["seq"]
        self.assertEqual(len(events), 212)
        self.assertIn(200, pages)
        self.assertTrue(
            all(
                e["payload"]["score"] == 0 and e["payload"]["month"] == "2030-02"
                for e in events
            )
        )
        self.assertEqual(self.store.snapshot()["heads"], prepared["heads"])

    def test_gap_or_checksum_corruption_stops_feed(self):
        user_shard = shard_for("alice", self.store.shards)
        with self.store.transaction(True) as db:
            db.execute(
                "UPDATE events SET hash='bad' WHERE shard=? AND seq=1", (user_shard,)
            )
        with self.assertRaises(Error) as checksum:
            self.store.feed(user_shard, 0)
        self.assertEqual(checksum.exception.status, 409)
        with self.store.transaction(True) as db:
            db.execute("DELETE FROM events WHERE shard=? AND seq=1", (user_shard,))
        with self.assertRaises(Error) as gap:
            self.store.feed(user_shard, 0)
        self.assertEqual(gap.exception.status, 409)

    def test_rehashed_wrong_shard_or_invalid_payload_stops_feed(self):
        shard = shard_for("alice", self.store.shards)
        other = next(
            "user" + str(index)
            for index in range(100)
            if shard_for("user" + str(index), self.store.shards) != shard
        )
        for payload in (
            {"month": "2030-01", "user": other, "score": 0},
            {"month": "2030-01", "user": "alice", "score": True},
        ):
            with self.store.transaction(True) as db:
                db.execute(
                    "UPDATE events SET payload=?,hash=? WHERE shard=? AND seq=1",
                    (dumps(payload), digest(payload), shard),
                )
            with self.assertRaises(Error):
                self.store.feed(shard, 0)

    def test_settlement_uses_one_clock_and_is_immutable_after_profile_change(self):
        self.store.ingest(self.body())
        self.now = month_bounds("2030-01")[1] + self.store.grace
        calls = []

        def clock():
            calls.append(self.now)
            return self.now

        self.store.clock = clock
        settled = self.store.close("2030-01")
        self.assertEqual(len(calls), 1)
        self.store.register("alice", "Updated")
        self.assertEqual(self.store.close("2030-01"), settled)
        self.assertEqual(settled["top10"][0]["score"], 1)

    def test_closed_season_cannot_gain_members_after_clock_rollback(self):
        self.now = month_bounds("2030-01")[1] + self.store.grace
        closed = self.store.close("2030-01")
        self.now = stamp("2030-01-20T00:00:00")
        self.store.register("new", "New")
        self.assertEqual(len(self.store.rows("2030-01")), 2)
        self.assertEqual(self.store.close("2030-01"), closed)

    def test_legacy_metadata_table_migrates_revision_and_keeps_lineage(self):
        path = self.root / "legacy.sqlite"
        with closing(sqlite3.connect(path)) as db:
            db.execute(
                "CREATE TABLE metadata(id INTEGER PRIMARY KEY CHECK(id=1),lineage TEXT,shards INTEGER,grace INTEGER)"
            )
            db.execute("INSERT INTO metadata VALUES(1,'legacy',2,172800)")
            db.commit()
        journal = Journal(path, clock=lambda: self.now)
        self.assertEqual(journal.status()["lineage"], "legacy")
        self.assertEqual(journal.status()["profile_revision"], 0)
        journal.register("a", "A")
        self.assertEqual(
            Journal(path, clock=lambda: self.now).status()["profile_revision"], 1
        )


if __name__ == "__main__":
    unittest.main()
