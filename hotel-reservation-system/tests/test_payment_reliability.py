"""Real simulated-ledger persistence; transports are explicitly substituted."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from hotel_reservation.common import Error
from hotel_reservation.payment import SimulatedHTTPPayment, SimulatedLedger
from hotel_reservation.payment_contracts import normalize_job, normalize_receipt


def job(action="charge", **changes):
    booking = "hotel." + "a" * 32
    return {
        "id": action + ":" + booking,
        "action": action,
        "booking_id": booking,
        "amount": 10000,
        "currency": "USD",
        "method": "test-ok",
        "token": "b" * 32,
        **changes,
    }


def operation(action="charge", **changes):
    return {
        key: value for key, value in job(action, **changes).items() if key != "token"
    }


def receipt(source, status="paid"):
    return {
        "booking_id": source["booking_id"],
        "amount": source["amount"],
        "currency": "USD",
        "status": status,
        "simulated": True,
    }


class PaymentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "payments.sqlite"
        self.ledger = SimulatedLedger(self.path)

    def test_receipt_replay_is_owned_and_does_not_charge_twice(self):
        original = self.ledger.execute(operation())
        original["amount"] = 999
        result = self.ledger.execute(operation())
        self.assertEqual(result["amount"], 10000)
        status = self.ledger.status()
        self.assertEqual(
            (status["operations"], status["attempts"], status["total_charges"]),
            (1, 2, 1),
        )

    def test_intent_frozen_before_waiting_for_writer(self):
        original = operation()
        transaction = self.ledger.transaction

        @contextmanager
        def mutate(write=False):
            original["amount"] = 999
            with transaction(write) as db:
                yield db

        with patch.object(self.ledger, "transaction", mutate):
            result = self.ledger.execute(original)
        self.assertEqual(result["amount"], 10000)
        self.assertEqual(self.ledger.execute(operation())["amount"], 10000)

    def test_concurrent_identical_operations_have_one_durable_effect(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(
                executor.map(lambda _: self.ledger.execute(operation()), range(24))
            )
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(self.ledger.status()["operations"], 1)
        self.assertEqual(self.ledger.status()["attempts"], 24)

    def test_lost_confirmation_survives_reopen_and_reuses_receipt(self):
        self.ledger.control({"fail_after_commit": True, "fail_refunds": False})
        with self.assertRaises(Error) as caught:
            self.ledger.execute(operation())
        self.assertEqual(caught.exception.status, 503)
        reopened = SimulatedLedger(self.path)
        reopened.control({"fail_after_commit": False, "fail_refunds": False})
        self.assertEqual(reopened.execute(operation())["status"], "paid")
        self.assertEqual(
            (reopened.status()["operations"], reopened.status()["attempts"]), (1, 2)
        )

    def test_conflicting_replay_cannot_change_the_original_intent(self):
        self.ledger.execute(operation())
        for body in (operation(amount=10001), operation(method="test-decline")):
            with self.assertRaises(Error) as caught:
                self.ledger.execute(body)
            self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.ledger.status()["charges"][0]["amount"], 10000)

    def test_refund_requires_full_matching_paid_charge(self):
        with self.assertRaises(Error):
            self.ledger.execute(operation("refund"))
        self.ledger.execute(operation())
        with self.assertRaises(Error):
            self.ledger.execute(operation("refund", amount=9999))
        result = self.ledger.execute(operation("refund"))
        self.assertEqual(result["status"], "refunded")
        self.assertEqual(self.ledger.execute(operation("refund")), result)
        self.assertEqual(self.ledger.status()["operations"], 2)

    def test_declined_charge_is_not_refundable(self):
        self.assertEqual(
            self.ledger.execute(operation(method="test-decline"))["status"], "declined"
        )
        with self.assertRaises(Error):
            self.ledger.execute(operation("refund", method="test-decline"))
        self.assertEqual(self.ledger.status()["operations"], 1)

    def test_refund_outage_blocks_new_effect_not_known_completed_receipt(self):
        self.ledger.execute(operation())
        self.ledger.control({"fail_after_commit": False, "fail_refunds": True})
        with self.assertRaises(Error) as caught:
            self.ledger.execute(operation("refund"))
        self.assertEqual(caught.exception.status, 503)
        self.ledger.control({"fail_after_commit": False, "fail_refunds": False})
        result = self.ledger.execute(operation("refund"))
        self.ledger.control({"fail_after_commit": False, "fail_refunds": True})
        self.assertEqual(self.ledger.execute(operation("refund")), result)
        self.assertEqual(self.ledger.status()["operations"], 2)

    def test_inconsistent_durable_charge_fails_closed(self):
        with self.ledger.transaction(True) as db:
            db.execute(
                "INSERT INTO charges VALUES(?,?,?,?)",
                (job()["booking_id"], 10000, "USD", "paid"),
            )
        with self.assertRaises(Error) as caught:
            self.ledger.execute(operation())
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.ledger.status()["operations"], 0)

    def test_invalid_operations_have_no_effect(self):
        for body in (
            operation(amount=True),
            operation(booking_id=None),
            operation(id="other"),
            operation(currency="EUR"),
            operation(method="card"),
            {**operation(), "unknown": 1},
        ):
            with self.assertRaises(ValueError):
                self.ledger.execute(body)
        self.assertEqual(self.ledger.status()["operations"], 0)

    def test_status_discloses_bounded_charge_listing(self):
        with self.ledger.transaction(True) as db:
            db.executemany(
                "INSERT INTO charges VALUES(?,?,?,?)",
                [(f"hotel.{index:032x}", 1, "USD", "paid") for index in range(1001)],
            )
        status = self.ledger.status()
        self.assertEqual(len(status["charges"]), 1000)
        self.assertEqual(status["total_charges"], 1001)
        self.assertTrue(status["charges_truncated"])

    def test_contract_strict_types_and_receipt_ownership(self):
        source = job()
        owned = normalize_job(source)
        source["amount"] = 2
        self.assertEqual(owned["amount"], 10000)
        normal = receipt(owned)
        copied = normalize_receipt(normal, owned)
        normal["amount"] = 1
        self.assertEqual(copied["amount"], 10000)
        for invalid in (
            receipt(owned) | {"amount": True},
            receipt(owned) | {"simulated": False},
            receipt(owned) | {"status": "pending"},
            receipt(owned) | {"unknown": 1},
        ):
            with self.assertRaises(Error):
                normalize_receipt(invalid, owned)
        for invalid in (job(token=""), job(amount=True), job(id="other")):
            with self.assertRaises(ValueError):
                normalize_job(invalid)

    def test_http_adapter_passes_budget_and_freezes_operation(self):
        config = {"payment_url": "http://payment", "payment_token": "private-token"}
        adapter = SimulatedHTTPPayment(config)
        config["payment_url"] = "http://changed"
        original = job()
        seen = []
        cancel = threading.Event()
        deadline = time.monotonic() + 2

        def call(url, token, body, **options):
            original["amount"] = 999
            seen.append((url, body, options))
            return receipt(body)

        with patch("hotel_reservation.payment.http_json", side_effect=call):
            self.assertEqual(
                adapter.execute_with_budget(original, deadline=deadline, cancel=cancel)[
                    "amount"
                ],
                10000,
            )
        self.assertEqual(seen[0][0], "http://payment/operations")
        self.assertEqual(seen[0][1]["amount"], 10000)
        self.assertIs(seen[0][2]["cancel"], cancel)
        self.assertEqual(seen[0][2]["deadline"], deadline)

    def test_adapter_rejects_cancelled_and_invalid_receipts(self):
        adapter = SimulatedHTTPPayment(
            {"payment_url": "http://payment", "payment_token": "private-token"}
        )
        stopped = threading.Event()
        stopped.set()
        with patch("hotel_reservation.payment.http_json") as call:
            with self.assertRaises(TimeoutError):
                adapter.execute_with_budget(job(), cancel=stopped)
            call.assert_not_called()
        with patch(
            "hotel_reservation.payment.http_json",
            return_value=receipt(job()) | {"amount": True},
        ):
            with self.assertRaises(Error):
                adapter.execute(job())


if __name__ == "__main__":
    unittest.main()
