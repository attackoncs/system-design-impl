from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"src"))
from hotel_reservation.common import Error, shard_for
from hotel_reservation.payment import SimulatedLedger
from hotel_reservation.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp()
        self.store = Store(self.root/"hotel.sqlite", clock=lambda: self.now, hold_seconds=10)
        self.store.manage("h", {"name": "Hotel", "address": "Address"})
        self.store.manage("h", {"name": "King", "description": "Room type"}, "king")
        self.days = [{"day": f"2030-01-0{i}", "capacity": 10, "overbook": 10, "price": 10000+i} for i in (2, 3)]
        self.store.set_days("h", "king", self.days)

    def tearDown(self):
        self.temp.cleanup()

    def request(self, key="req", quantity=1, **kwargs):
        return {"hotel": "h", "room": "king", "start": "2030-01-02", "end": "2030-01-04", "quantity": quantity,
                "request_key": key, "expected_total": quantity*20005, "payment_method": "test-ok", **kwargs}

    @staticmethod
    def receipt(job, status="paid"):
        return {"booking_id": job["booking_id"], "amount": job["amount"], "currency": "USD", "status": status}

    def available(self):
        return self.store.availability("h", "king", "2030-01-02", "2030-01-04")

    def test_multinight_atomicity_price_snapshot_idempotency(self):
        first = self.store.reserve("alice", self.request())
        self.assertEqual(self.store.reserve("alice", self.request())["id"], first["id"])
        self.assertEqual(self.available()["available"], 10)
        with self.assertRaises(Error):
            self.store.reserve("alice", self.request(quantity=2))
        self.store.set_days("h", "king", [{**r, "price": 20000} for r in self.days])
        self.assertEqual(self.store.reserve("alice", self.request())["amount"], 20005)
        with self.assertRaises(Error):
            self.store.reserve("alice", self.request("price-changed"))
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM bookings").fetchone()[0], 1)

    def test_second_night_failure_and_precommit_exception_rollback(self):
        self.store.set_days("h", "king", [{**self.days[1], "capacity": 0}])
        with self.assertRaises(Error):
            self.store.reserve("alice", self.request())
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT sum(reserved) FROM inventory").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        self.store.set_days("h", "king", self.days)
        def crash():
            raise RuntimeError("before commit")
        with self.assertRaises(RuntimeError):
            self.store.reserve("alice", self.request(), crash)
        self.assertEqual(self.available()["available"], 11)

    def test_concurrency_overbooking_rounding_and_database_constraint(self):
        self.store.set_days("h", "king", [{**r, "capacity": 9} for r in self.days])
        def reserve(i):
            try:
                self.store.reserve("alice", self.request(str(i)))
                return 1
            except Error as exc:
                self.assertEqual(exc.status, 409)
                return 0
        with ThreadPoolExecutor(max_workers=12) as pool:
            self.assertEqual(sum(pool.map(reserve, range(30))), 9)
        with self.store.transaction(True) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE inventory SET reserved=reserved+1")
        with self.assertRaises(Error):
            self.store.set_days("h", "king", [{**self.days[0], "capacity": 1}])
        self.store.set_days("h", "king", self.days)  # 10 physical permits floor(11).
        self.assertEqual(self.available()["available"], 2)

    def test_decline_cancel_repeated_cancel_and_inventory(self):
        booking = self.store.reserve("alice", self.request())
        job = self.store.claim()
        self.store.finish(job, self.receipt(job, "declined"))
        self.assertEqual(self.store.booking(booking["id"])["status"], "REJECTED")
        self.assertEqual(self.available()["available"], 11)
        other = self.store.reserve("alice", self.request("other"))
        self.store.cancel(other["id"], "alice")
        self.store.cancel(other["id"], "alice")
        self.store.claim()  # Never-attempted charge is skipped after cancellation.
        self.assertEqual(self.available()["available"], 11)
        with self.assertRaises(Error):
            self.store.booking(other["id"], "bob")

    def test_cancel_during_charge_and_lost_ack_refund(self):
        booking = self.store.reserve("alice", self.request())
        charge = self.store.claim()
        ledger = SimulatedLedger(self.root/"payments.sqlite")
        operation = {k: v for k, v in charge.items() if k != "token"}
        ledger.control({"fail_after_commit": True, "fail_refunds": False})
        with self.assertRaises(Error):
            ledger.execute(operation)
        self.store.cancel(booking["id"])
        ledger.control({"fail_after_commit": False, "fail_refunds": False})
        receipt = ledger.execute(operation)
        self.store.finish(charge, receipt)
        self.assertEqual(self.store.booking(booking["id"])["payment"], "REFUND_PENDING")
        refund = self.store.claim()
        self.store.finish(refund, ledger.execute({k: v for k, v in refund.items() if k != "token"}))
        self.assertEqual(self.store.booking(booking["id"])["payment"], "REFUNDED")
        self.assertEqual(self.available()["available"], 11)
        self.assertEqual(ledger.status()["operations"], 2)
        self.assertEqual(ledger.status()["attempts"], 3)

    def test_expiry_before_and_during_charge_never_confirms(self):
        first = self.store.reserve("alice", self.request())
        self.now += 11
        self.assertEqual(self.store.sweep(), 1)
        self.store.claim()
        self.assertEqual(self.store.booking(first["id"])["payment"], "NONE")
        second = self.store.reserve("alice", self.request("second"))
        job = self.store.claim()
        self.now += 11
        self.store.finish(job, self.receipt(job))
        self.assertEqual(self.store.booking(second["id"])["status"], "EXPIRED")
        refund = self.store.claim()
        self.store.finish(refund, self.receipt(refund, "refunded"))
        self.assertEqual(self.available()["available"], 11)

    def test_leases_fence_stale_workers_and_restart_preserves_jobs(self):
        self.store.reserve("alice", self.request())
        old = self.store.claim()
        self.now += 3
        self.store = Store(self.store.path, clock=lambda: self.now, hold_seconds=10)
        current = self.store.claim()
        self.assertNotEqual(old["token"], current["token"])
        self.assertFalse(self.store.finish(old, self.receipt(old)))
        self.assertTrue(self.store.finish(current, self.receipt(current)))
        self.assertEqual(self.store.status()["unfinished_jobs"], 0)
        self.assertIn("CONFIRMED", [r["kind"] for r in self.store.events()["events"]])

    def test_soft_delete_cache_and_shard_ownership(self):
        self.available()  # Warm cached read; a different Store instance writes.
        other = Store(self.store.path, clock=lambda: self.now)
        other.reserve("alice", self.request())
        self.assertEqual(self.available()["available"], 10)
        self.store.manage("h", room="king", delete=True)
        with self.assertRaises(Error):
            self.store.reserve("alice", self.request("after-delete"))
        history = self.store.history("h", "alice")["reservations"]
        self.store.cancel(history[0]["id"])
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT sum(reserved) FROM inventory").fetchone()[0], 0)
        sharded = Store(self.root/"shard.sqlite", 0, 2)
        wrong = next(str(i) for i in range(20) if shard_for(str(i), 2)==1)
        with self.assertRaises(Error):
            sharded.owner(wrong)
        with self.assertRaises(Error):
            Store(sharded.path, 0, 1)

    def test_invalid_requests_and_provider_payload_conflict(self):
        for body in [self.request(quantity=True), self.request(start="2029-01-01"),
                     self.request(end="2030-01-02"), self.request(payment_method="real-card"),
                     {**self.request(), "unknown": 1}]:
            with self.assertRaises((ValueError, Error)):
                self.store.reserve("alice", body)
        booking = self.store.reserve("alice", self.request())
        job = self.store.claim()
        ledger = SimulatedLedger(self.root/"payments.sqlite")
        body = {k: v for k, v in job.items() if k!="token"}
        ledger.execute(body)
        with self.assertRaises(Error):
            ledger.execute({**body, "amount": body["amount"]+1})
        with self.assertRaises(Error):
            self.store.finish(job, {**self.receipt(job), "amount": 1})


if __name__ == "__main__":
    unittest.main()
