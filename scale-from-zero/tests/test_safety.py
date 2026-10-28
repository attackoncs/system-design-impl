from concurrent.futures import ThreadPoolExecutor
import copy
import sqlite3
import threading
from unittest.mock import patch

from helpers_review import StoreCase
from scale_from_zero.common import Error, checksum
from scale_from_zero.contracts import update
from scale_from_zero.store import Store, partition


class SafetyTests(StoreCase):
    def test_frozen_input_waiting_for_writer_keeps_fingerprint_and_result_consistent(
        self,
    ):
        body = self.body()
        frozen = threading.Event()

        def validate(value):
            result = update(value)
            frozen.set()
            return result

        with (
            ThreadPoolExecutor(max_workers=1) as pool,
            patch("scale_from_zero.profiles.validate_update", side_effect=validate),
        ):
            with self.store.transaction():
                future = pool.submit(self.store.update, "alice", body)
                self.assertTrue(frozen.wait(1))
                body["city"] = "changed"
            result = future.result(2)
        self.assertEqual(result["profile"]["city"], "Test")
        self.assertEqual(self.store.update("alice", self.body()), result)

    def test_expired_claim_cannot_ack_even_without_reclaim(self):
        self.store.update("alice", self.body())
        job = self.store.claim()
        self.now = job["lease"]
        self.assertFalse(self.store.ack(job)["acknowledged"])
        self.assertEqual(self.store.status()["pending_jobs"], 1)

    def test_changed_claim_payload_or_foreign_source_cannot_finish_original_job(self):
        self.store.update("alice", self.body())
        job = self.store.claim()
        changed = copy.deepcopy(job)
        changed["payload"]["profile"]["city"] = "another"
        self.assertFalse(self.store.ack(changed)["acknowledged"])
        changed = copy.deepcopy(job)
        changed["source_id"] = "f" * 32
        self.assertFalse(self.store.ack(changed)["acknowledged"])
        self.assertTrue(self.store.ack(job)["acknowledged"])

    def test_task_ids_support_full_length_users(self):
        user = "a" * 80
        self.store.update(user, self.body())
        job = self.store.claim()
        self.assertGreater(len(job["id"]), 80)
        self.assertTrue(self.consumer.consume(job))
        self.assertTrue(self.store.ack(job)["acknowledged"])

    def test_writer_samples_one_clock_for_lease_and_attempt_metadata(self):
        self.store.update("alice", self.body())
        samples = []

        def clock():
            samples.append(1000 + len(samples))
            return samples[-1]

        self.store.clock = clock
        job = self.store.claim(lease_seconds=5)
        self.assertEqual(samples, [1000])
        self.assertEqual((job["lease"], job["attempts"]), (1005, 1))

    def test_consumer_scopes_identical_ids_to_source_databases(self):
        self.store.update("alice", self.body())
        first = self.store.claim()
        other = Store(self.root / "other.sqlite", lambda: self.now)
        other.update("alice", self.body())
        second = other.claim()
        self.assertEqual(first["id"], second["id"])
        self.assertNotEqual(first["source_id"], second["source_id"])
        self.consumer.consume(first)
        self.consumer.consume(second)
        self.assertEqual(self.consumer.status()["unique_events"], 2)

    def test_replication_binds_source_and_survives_source_process_restart(self):
        self.store.update("alice", self.body())
        self.replica.apply_feed(self.store.changes(0))
        restarted = Store(self.store.path, lambda: self.now)
        self.replica.apply_feed(restarted.changes(1))
        other = Store(self.root / "other.sqlite", lambda: self.now)
        other.update("alice", self.body())
        with self.assertRaises(Error):
            self.replica.apply_feed(other.changes(1))
        self.assertEqual(self.replica.get("alice")["version"], 1)

    def test_replication_rejects_omitted_changed_prefix_and_regressing_user_versions(
        self,
    ):
        self.store.update("alice", self.body())
        self.store.update("alice", self.body("next", 1))
        feed = self.store.changes(0)
        changed = copy.deepcopy(feed)
        changed["changes"] = changed["changes"][:1]
        with self.assertRaises(Error):
            self.replica.apply_feed(changed)
        changed = copy.deepcopy(feed)
        changed["previous_hash"] = "f" * 64
        with self.assertRaises(Error):
            self.replica.apply_feed(changed)
        changed = copy.deepcopy(feed)
        last = changed["changes"][1]
        last["version"] = 1
        last["hash"] = checksum(
            {key: last[key] for key in ("seq", "user", "version", "profile")}
        )
        changed["head"] = last["hash"]
        with self.assertRaises(Error):
            self.replica.apply_feed(changed)
        self.assertEqual(self.replica.status()["seq"], 0)

    def test_feed_late_failure_rolls_back_every_record_and_source_binding(self):
        self.store.update("alice", self.body())
        self.store.update("alice", self.body("next", 1))
        feed = self.store.changes(0)
        feed["head"] = "f" * 64
        with self.assertRaises(Error):
            self.replica.apply_feed(feed)
        self.assertEqual(self.replica.status()["users"], 0)
        with self.replica.connection() as db:
            self.assertIsNone(db.execute("SELECT * FROM replication_source").fetchone())

    def test_bound_store_rejects_wrong_shard_even_when_used_directly(self):
        self.replica.bind_partition(2, 0)
        wrong = next(
            "user" + str(i) for i in range(20) if partition("user" + str(i), 2) == 1
        )
        self.store.update(wrong, self.body())
        with self.assertRaises(Error):
            self.replica.apply_feed(self.store.changes(0))
        with self.assertRaises(Error):
            self.replica.update(wrong, self.body())
        self.assertEqual(self.replica.status()["seq"], 0)

    def test_profile_history_is_immutable(self):
        self.store.update("alice", self.body())
        for statement in ("DELETE FROM changes", "UPDATE changes SET version=999"):
            with (
                self.assertRaises(sqlite3.IntegrityError),
                self.store.transaction() as db,
            ):
                db.execute(statement)

    def test_nonfinite_clock_cannot_create_infinite_session(self):
        self.store.clock = lambda: float("inf")
        with self.assertRaises(ValueError):
            self.store.session_create("alice")
        with self.store.connection() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0
            )
