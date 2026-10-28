from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import test_store  # noqa: F401 -- source path for standalone discovery
from payment_system.common import Error, digest
from payment_system.store import Store
from payment_system.provider import MockPSP


class PaymentSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1000
        self.store = Store(
            self.root / "store.sqlite", ("seller", "other"), lambda: self.now
        )

    def body(self, key="pay"):
        return {
            "request_key": key,
            "order_id": "order_" + key,
            "amount": 1000,
            "currency": "USD",
        }

    def receipt(self, op, status="SUCCEEDED", revision=2):
        return {
            **{
                key: op[key]
                for key in ("id", "merchant", "kind", "parent", "amount", "currency")
            },
            "provider_id": "mock_" + op["id"],
            "status": status,
            "revision": revision,
            "checkout": None,
        }

    def statement(self, rows=None):
        rows = rows or []
        return {"operations": rows, "snapshot": digest(rows), "simulated": True}

    def test_batch_checkout_is_atomic_idempotent_and_owned_per_order(self):
        body = {
            "checkout_id": "checkout",
            "orders": [
                {
                    "merchant": merchant,
                    "order_id": "order",
                    "amount": amount,
                    "currency": "USD",
                }
                for merchant, amount in (("seller", 1000), ("other", 2000))
            ],
        }
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(lambda _: self.store.create_checkout(body), range(8))
            )
        identities = [op["id"] for op in results[0]["orders"]]
        self.assertTrue(
            all(
                [op["id"] for op in result["orders"]] == identities
                for result in results
            )
        )
        self.assertEqual(self.store.status()["operations"], 2)
        for op in results[0]["orders"]:
            self.store.apply(self.receipt(op))
        self.assertEqual(self.store.wallet("seller")["available"], 1000)
        self.assertEqual(self.store.wallet("other")["available"], 2000)
        self.assertTrue(
            all(
                op["status"] == "SUCCEEDED"
                for op in self.store.checkout("checkout")["orders"]
            )
        )
        changed = json.loads(json.dumps(body))
        changed["orders"][0]["amount"] += 1
        with self.assertRaises(Error):
            self.store.create_checkout(changed)

    def test_later_invalid_checkout_order_rolls_back_earlier_jobs(self):
        body = {
            "checkout_id": "checkout",
            "orders": [
                {
                    "merchant": merchant,
                    "order_id": "order",
                    "amount": 1,
                    "currency": "USD",
                }
                for merchant in ("seller", "missing")
            ],
        }
        with self.assertRaises(Error):
            self.store.create_checkout(body)
        self.assertEqual(self.store.status()["operations"], 0)
        self.assertEqual(self.store.status()["pending_jobs"], 0)

    def test_checkout_size_and_duplicate_orders_are_bounded(self):
        order = {
            "merchant": "seller",
            "order_id": "order",
            "amount": 1,
            "currency": "USD",
        }
        for orders in ([], [order, order], [order] * 21):
            with self.assertRaises(ValueError):
                self.store.create_checkout(
                    {"checkout_id": "checkout", "orders": orders}
                )

    def test_create_freezes_input_before_writer_wait(self):
        from payment_system.contracts import freeze

        ready = threading.Event()

        def frozen(body):
            result = freeze(body)
            ready.set()
            return result

        body = self.body()
        connection = sqlite3.connect(self.store.path, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            with (
                patch("payment_system.operations.freeze", side_effect=frozen),
                ThreadPoolExecutor(max_workers=1) as pool,
            ):
                result = pool.submit(self.store.create, "seller", "payin", body)
                self.assertTrue(ready.wait(1))
                body["amount"] = 9000
                connection.commit()
                op = result.result(timeout=2)
            self.assertEqual(op["amount"], 1000)
            self.assertEqual(
                self.store.create("seller", "payin", self.body())["id"], op["id"]
            )
        finally:
            connection.close()

    def test_expired_lease_cannot_finish_even_without_reclaim(self):
        self.store.create("seller", "payin", self.body())
        job = self.store.claim()
        self.assertEqual(job["attempts"], 1)
        self.now += 3
        self.assertFalse(self.store.finish_job(job, error="late"))
        replacement = self.store.claim(lease_seconds=30)
        self.assertEqual(replacement["lease"], self.now + 30)
        self.assertEqual(replacement["attempts"], 2)
        self.assertNotEqual(job["token"], replacement["token"])

    def test_same_revision_conflict_goes_to_review(self):
        op = self.store.create("seller", "payin", self.body())
        pending = self.receipt(op, "PENDING", 1)
        self.store.apply(pending)
        result = self.store.apply({**pending, "status": "SUCCEEDED"})
        self.assertIn("review", result)
        self.assertEqual(self.store.wallet("seller")["available"], 0)

    def test_invalid_receipt_or_event_id_never_changes_funds(self):
        op = self.store.create("seller", "payin", self.body())
        for changes in (
            {"amount": True},
            {"currency": "EUR"},
            {"kind": "payout", "parent": "parent"},
            {"checkout": "http://outside.example/checkout/token"},
        ):
            with self.assertRaises(ValueError):
                self.store.apply({**self.receipt(op), **changes})
        with self.assertRaises(ValueError):
            self.store.apply(self.receipt(op), "")
        self.assertEqual(self.store.wallet("seller")["available"], 0)

    def test_known_provider_revision_cannot_change_after_unknown_state(self):
        pay = self.store.create("seller", "payin", self.body())
        self.store.apply(self.receipt(pay))
        out = self.store.create(
            "seller",
            "payout",
            {
                "request_key": "out",
                "amount": 100,
                "currency": "USD",
                "destination": "test-bank-ok",
            },
        )
        pending = self.receipt(out, "PENDING", 1)
        self.store.apply(pending)
        job = self.store.claim()
        self.assertEqual(job["payload"]["operation"], out["id"])
        self.store.finish_job(job, error="query response lost")
        self.assertEqual(self.store.operation(out["id"])["status"], "UNKNOWN")
        result = self.store.apply({**pending, "status": "FAILED"})
        self.assertIn("review", result)
        self.assertEqual(self.store.wallet("seller")["held"], 100)
        self.store.apply({**pending, "status": "FAILED", "revision": 2})
        self.assertEqual(self.store.wallet("seller")["held"], 0)
        self.assertEqual(self.store.wallet("seller")["available"], 1000)

    def test_ledger_post_identity_cannot_silently_change(self):
        op = self.store.create("seller", "payin", self.body())
        receipt = self.receipt(op)
        self.store.apply(receipt)
        with self.assertRaises(Error), self.store.transaction(True) as db:
            self.store.post(
                db,
                "settle:" + op["id"],
                op["id"],
                "settle",
                "psp:clearing",
                "merchant:seller:available",
                1000,
                "different",
            )
        self.assertEqual(self.store.wallet("seller")["available"], 1000)

    def test_provider_transaction_identity_cannot_credit_two_operations(self):
        first = self.store.create("seller", "payin", self.body("one"))
        second = self.store.create("seller", "payin", self.body("two"))
        receipt = self.receipt(first)
        self.store.apply(receipt)
        result = self.store.apply(
            {**self.receipt(second), "provider_id": receipt["provider_id"]}
        )
        self.assertIn("review", result)
        self.assertEqual(self.store.operation(second["id"])["status"], "CREATED")
        self.assertEqual(self.store.wallet("seller")["available"], 1000)

    def test_statement_checksum_duplicates_and_shape_fail_closed(self):
        op = self.store.create("seller", "payin", self.body())
        receipt = self.receipt(op)
        for statement in (
            {"operations": [], "snapshot": "wrong", "simulated": True},
            self.statement([receipt, receipt]),
            {"operations": [], "snapshot": digest([]), "simulated": False},
        ):
            run = self.store.schedule_reconcile()["run"]
            with self.assertRaises((ValueError, Error)):
                self.store.reconcile(run, statement)
            self.assertEqual(self.store.reconciliation(run)["status"], "pending")

    def test_reconciliation_lease_and_completed_result_are_fenced(self):
        run = self.store.schedule_reconcile()["run"]
        old = self.store.claim()
        self.now += 3
        with self.assertRaises(Error):
            self.store.reconcile(run, self.statement(), job=old)
        current = self.store.claim()
        first = self.store.reconcile(run, self.statement(), job=current)
        op = self.store.create("seller", "payin", self.body())
        self.assertEqual(
            self.store.reconcile(run, self.statement([self.receipt(op)])), first
        )

    def test_invalid_clock_does_not_commit_work(self):
        self.now = float("nan")
        with self.assertRaises(ValueError):
            self.store.create("seller", "payin", self.body())
        self.now = 1000
        self.assertEqual(self.store.status()["operations"], 0)

    def test_provider_callback_reclaim_rejects_late_successful_ack(self):
        provider = MockPSP(
            self.root / "provider.sqlite",
            {
                "provider_url": "http://127.0.0.1:9871",
                "api_url": "http://127.0.0.1:9870",
                "webhook_secret": "secret",
            },
            lambda: self.now,
        )
        provider.create(
            {
                "id": "op",
                "merchant": "seller",
                "kind": "payin",
                "parent": None,
                "amount": 1,
                "currency": "USD",
                "destination": "hosted",
            }
        )
        entered, release = threading.Event(), threading.Event()
        calls = []

        def callback(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                release.wait(2)
                return {"applied": True}
            raise OSError("new attempt failed")

        with (
            patch("payment_system.provider_delivery.http_json", side_effect=callback),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            old = pool.submit(provider.dispatch_once)
            self.assertTrue(entered.wait(1))
            self.assertFalse(provider.dispatch_once())
            self.now += 16
            provider.dispatch_once()
            release.set()
            old.result(timeout=2)
        with provider.transaction() as db:
            row = db.execute("SELECT * FROM webhooks").fetchone()
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["attempts"], 2)
