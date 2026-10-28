"""Actual shard SQLite/cache state with controlled mutation and concurrency gates."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from hotel_reservation.cache import Cache
from hotel_reservation.common import Error
from hotel_reservation.store import Store


class StoreReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.now = [datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp()]
        self.store = Store(
            self.root / "shard.sqlite", hold_seconds=10, clock=lambda: self.now[0]
        )
        self.store.manage("h", {"name": "Original", "address": "Address"})
        self.store.manage("h", {"name": "King", "description": "Room"}, "king")
        self.days = [
            {"day": f"2030-01-0{i}", "capacity": 10, "overbook": 10, "price": 10000 + i}
            for i in (2, 3)
        ]
        self.store.set_days("h", "king", self.days)

    def request(self, key="request"):
        return {
            "hotel": "h",
            "room": "king",
            "start": "2030-01-02",
            "end": "2030-01-04",
            "quantity": 1,
            "request_key": key,
            "expected_total": 20005,
            "payment_method": "test-ok",
        }

    @staticmethod
    def receipt(job, status="paid"):
        return {
            "booking_id": job["booking_id"],
            "amount": job["amount"],
            "currency": job["currency"],
            "status": status,
        }

    def available(self):
        return self.store.availability("h", "king", "2030-01-02", "2030-01-04")

    def mutate_before_writer(self, mutation):
        original = self.store.transaction

        @contextmanager
        def transaction(write=False):
            if write:
                mutation()
            with original(write) as db:
                yield db

        return patch.object(self.store, "transaction", transaction)

    def test_reservation_owns_fingerprint_and_dates_before_writer_wait(self):
        request = self.request()
        original = json.loads(json.dumps(request))
        with self.mutate_before_writer(lambda: request.update(start="2030-01-03")):
            booking = self.store.reserve("alice", request)
        self.assertEqual(booking["start"], "2030-01-02")
        self.assertEqual(len(booking["nights"]), 2)
        self.assertEqual(self.store.reserve("alice", original)["id"], booking["id"])
        self.assertEqual(self.available()["available"], 10)

    def test_catalog_input_owned_before_writer_wait(self):
        body = {"name": "Kept", "address": "Address"}
        with self.mutate_before_writer(lambda: body.update(name="Changed")):
            self.store.manage("h", body)
        self.assertEqual(self.store.catalog("h")["name"], "Kept")

    def test_fresh_reservation_uses_one_lock_after_time_for_hold_job_and_event(self):
        calls = []

        def ticking():
            calls.append(1)
            return self.now[0] + len(calls)

        self.store.clock = ticking
        booking = self.store.reserve("alice", self.request())
        with self.store.transaction() as db:
            job = db.execute(
                "SELECT due FROM jobs WHERE booking=?", (booking["id"],)
            ).fetchone()
            event = db.execute(
                "SELECT created FROM events WHERE booking=?", (booking["id"],)
            ).fetchone()
        self.assertEqual(len(calls), 1)
        self.assertEqual(booking["created"], job[0])
        self.assertEqual(booking["created"], event[0])
        self.assertEqual(booking["expires"] - booking["created"], 10)

    def test_writer_delayed_until_next_day_rechecks_stay_atomically(self):
        with self.mutate_before_writer(
            lambda: self.now.__setitem__(
                0, datetime(2030, 1, 3, tzinfo=timezone.utc).timestamp()
            )
        ):
            with self.assertRaises(ValueError):
                self.store.reserve("alice", self.request())
        with self.store.transaction() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM bookings").fetchone()[0], 0
            )
            self.assertEqual(
                db.execute("SELECT sum(reserved) FROM inventory").fetchone()[0], 0
            )

    def test_idempotent_replay_survives_past_dates_and_price_updates(self):
        request = self.request()
        booking = self.store.reserve("alice", request)
        self.store.cancel(booking["id"])
        self.now[0] += 90 * 86400
        replay = self.store.reserve("alice", request)
        self.assertEqual(replay["id"], booking["id"])
        self.assertEqual(replay["status"], "CANCELLED")
        with self.store.transaction() as db:
            self.assertEqual(
                db.execute("SELECT sum(reserved) FROM inventory").fetchone()[0], 0
            )

    def test_catalog_revision_and_body_share_a_snapshot(self):
        other = Store(self.store.path, clock=lambda: self.now[0])
        original = self.store.cache.load
        changed = []

        def load(key, loader):
            if not changed:
                changed.append(1)
                other.manage("h", {"name": "New", "address": "New address"})
            return original(key, loader)

        with patch.object(self.store.cache, "load", load):
            self.assertEqual(self.store.catalog("h")["name"], "Original")
        self.assertEqual(self.store.catalog("h")["name"], "New")

    def test_availability_active_flag_and_inventory_share_snapshot(self):
        other = Store(self.store.path, clock=lambda: self.now[0])
        original = self.store.cache.load

        def load(key, loader):
            other.manage("h", room="king", delete=True)
            return original(key, loader)

        with patch.object(self.store.cache, "load", load):
            self.assertEqual(self.available()["available"], 11)
        with self.assertRaises(Error) as caught:
            self.available()
        self.assertEqual(caught.exception.status, 404)

    def test_cached_catalog_and_availability_are_independent_objects(self):
        catalog = self.store.catalog("h")
        catalog["rooms"][0]["name"] = "Mutated"
        quote = self.available()
        quote["days"][0]["price"] = 0
        self.assertEqual(self.store.catalog("h")["rooms"][0]["name"], "King")
        self.assertEqual(self.available()["unit_total"], 20005)

    def test_claim_expired_unattempted_charge_releases_before_payment(self):
        booking = self.store.reserve("alice", self.request())
        self.now[0] += 11
        self.assertIsNone(self.store.claim())
        current = self.store.booking(booking["id"])
        self.assertEqual((current["status"], current["payment"]), ("EXPIRED", "NONE"))
        self.assertEqual(self.available()["available"], 11)
        self.assertEqual(self.store.status()["unfinished_jobs"], 0)

    def test_lease_validation_does_not_claim_or_change_job(self):
        self.store.reserve("alice", self.request())
        for lease in (True, 0, -1, float("inf"), float("nan"), 10**1000):
            with self.assertRaises(ValueError):
                self.store.claim(lease)
        with self.store.transaction() as db:
            row = db.execute("SELECT attempts,status FROM jobs").fetchone()
        self.assertEqual(tuple(row), (0, "pending"))

    def test_receipt_bool_amount_or_changed_job_method_cannot_settle(self):
        booking = self.store.reserve("alice", self.request())
        job = self.store.claim()
        with self.assertRaises(Error):
            self.store.finish(job, {**self.receipt(job), "amount": True})
        altered = {**job, "method": "test-decline"}
        with self.assertRaises(Error):
            self.store.finish(altered, self.receipt(altered))
        with self.assertRaises(Error):
            self.store.retry(altered, "wrong intent")
        self.assertEqual(self.store.booking(booking["id"])["payment"], "CHARGING")
        self.assertTrue(self.store.finish(job, self.receipt(job)))

    def test_finish_owns_job_and_receipt_before_writer_wait(self):
        booking = self.store.reserve("alice", self.request())
        job = self.store.claim()
        result = self.receipt(job)
        with self.mutate_before_writer(
            lambda: (job.update(amount=1), result.update(amount=1, status="declined"))
        ):
            self.assertTrue(self.store.finish(job, result))
        self.assertEqual(self.store.booking(booking["id"])["status"], "CONFIRMED")

    def test_stale_retry_cannot_revert_new_owner(self):
        self.store.reserve("alice", self.request())
        old = self.store.claim()
        self.now[0] += 3
        current = self.store.claim()
        self.store.retry(old, "late error")
        with self.store.transaction() as db:
            row = db.execute("SELECT token,status,error FROM jobs").fetchone()
        self.assertEqual(tuple(row), (current["token"], "inflight", ""))

    def test_cancel_and_paid_callback_race_always_refunds_once(self):
        booking = self.store.reserve("alice", self.request())
        job = self.store.claim()
        start = threading.Barrier(2)

        def cancel():
            start.wait(2)
            self.store.cancel(booking["id"], "alice")

        def finish():
            start.wait(2)
            self.store.finish(job, self.receipt(job))

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(cancel), executor.submit(finish)]
            for future in futures:
                future.result(3)
        current = self.store.booking(booking["id"])
        self.assertEqual(
            (current["status"], current["payment"]), ("CANCELLED", "REFUND_PENDING")
        )
        refund = self.store.claim()
        self.assertEqual(refund["action"], "refund")
        self.store.finish(refund, self.receipt(refund, "refunded"))
        self.assertFalse(self.store.finish(job, self.receipt(job)))
        self.assertEqual(self.store.booking(booking["id"])["payment"], "REFUNDED")
        self.assertEqual(self.available()["available"], 11)
        with self.store.transaction() as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM jobs WHERE action='refund'"
                ).fetchone()[0],
                1,
            )

    def test_missing_inventory_release_fails_closed_and_rolls_back(self):
        booking = self.store.reserve("alice", self.request())
        with self.store.transaction(True) as db:
            db.execute("DELETE FROM inventory WHERE day='2030-01-03'")
        with self.assertRaises(Error) as caught:
            self.store.cancel(booking["id"])
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.store.booking(booking["id"])["held"], 1)
        with self.store.transaction() as db:
            self.assertEqual(
                db.execute("SELECT reserved FROM inventory").fetchone()[0], 1
            )

    def test_sweep_batch_remains_bounded_and_recovers_remaining_holds(self):
        self.store.set_days(
            "h", "king", [{**row, "capacity": 200} for row in self.days]
        )
        for index in range(105):
            self.store.reserve("alice", self.request("hold" + str(index)))
        self.now[0] += 11
        self.assertEqual(self.store.sweep(), 100)
        self.assertEqual(self.store.sweep(), 5)
        self.assertEqual(self.store.sweep(), 0)
        with self.store.transaction() as db:
            self.assertEqual(
                db.execute("SELECT sum(reserved) FROM inventory").fetchone()[0], 0
            )

    def test_history_owner_and_cursor_validation(self):
        booking = self.store.reserve("alice", self.request())
        self.assertEqual(self.store.history("h", "bob")["reservations"], [])
        with self.assertRaises(ValueError):
            self.store.history("h", "alice", after="other." + "a" * 32)
        self.assertEqual(
            self.store.history("h", "alice")["reservations"][0]["id"], booking["id"]
        )


class CacheReliabilityTests(unittest.TestCase):
    def test_same_key_cold_load_is_coalesced_and_every_return_is_isolated(self):
        cache, entered, release = Cache(), threading.Event(), threading.Event()
        calls = []

        def load():
            calls.append(1)
            entered.set()
            release.wait(3)
            return {"nested": [1]}

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(cache.load, "same", load) for _ in range(8)]
            try:
                self.assertTrue(entered.wait(1))
            finally:
                release.set()
            values = [future.result(3) for future in futures]
        self.assertEqual(len(calls), 1)
        values[0]["nested"].append(2)
        self.assertTrue(all(value == {"nested": [1]} for value in values[1:]))
        self.assertEqual(cache.get("same"), {"nested": [1]})

    def test_slow_loader_does_not_hold_global_lru_lock(self):
        cache, entered, release = Cache(), threading.Event(), threading.Event()

        def load():
            entered.set()
            release.wait(3)
            return {"ok": True}

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(cache.load, "slow", load)
            try:
                self.assertTrue(entered.wait(1))
                cache.put("fast", {"value": 1})
                self.assertEqual(cache.get("fast"), {"value": 1})
            finally:
                release.set()
            future.result(3)

    def test_failed_loader_is_retryable_and_ttl_capacity_are_enforced(self):
        clock = [100.0]
        cache = Cache(ttl=1, limit=1, clock=lambda: clock[0])
        with self.assertRaises(ValueError):
            cache.load("a", lambda: (_ for _ in ()).throw(ValueError("failure")))
        self.assertEqual(cache.load("a", lambda: {"ok": True}), {"ok": True})
        clock[0] += 1
        self.assertIsNone(cache.get("a"))
        cache.put("a", {})
        cache.put("b", {})
        self.assertIsNone(cache.get("a"))
        for options in (
            {"ttl": float("nan")},
            {"ttl": -1},
            {"limit": True},
            {"limit": -1},
        ):
            with self.assertRaises(ValueError):
                Cache(**options)


if __name__ == "__main__":
    unittest.main()
