"""Saga worker behavior with controlled provider/HTTP and real durable stores."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from hotel_reservation.payment import PaymentAdapter, SimulatedLedger
from hotel_reservation.store import Store
from hotel_reservation.worker import Worker
from test_payment_reliability import job, receipt


CONFIG = {
    "shard_urls": ["http://shard"],
    "internal_token": "private-token",
    "payment_url": "http://payment",
    "payment_token": "payment-token",
}


class Provider(PaymentAdapter):
    def __init__(self, action=None):
        self.action = action or (lambda source: receipt(source))
        self.calls = []

    def execute(self, source):
        self.calls.append(dict(source))
        return self.action(source)


class WorkerTests(unittest.TestCase):
    def fake_rpc(self, calls, source=None):
        def call(url, token, body, **options):
            path = url.split("http://shard")[1]
            calls.append((path, body, options))
            if path == "/internal/sweep":
                return {"expired": 0}
            if path == "/internal/claim":
                return {"job": source if source is not None else job()}
            if path == "/internal/finish":
                return {"settled": True}
            if path == "/internal/retry":
                return {"retained": True}
            raise AssertionError(path)

        return call

    def test_success_has_one_budget_and_separate_provider_call(self):
        provider, calls = Provider(), []
        worker = Worker(CONFIG, 0, provider=provider)
        self.addCleanup(worker.close)
        with patch(
            "hotel_reservation.worker.http_json", side_effect=self.fake_rpc(calls)
        ):
            self.assertTrue(worker.run_once())
        self.assertEqual(
            [path for path, _, _ in calls],
            ["/internal/sweep", "/internal/claim", "/internal/finish"],
        )
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len({options["deadline"] for _, _, options in calls}), 1)
        self.assertTrue(
            all(options["cancel"] is worker.stop for _, _, options in calls)
        )
        self.assertEqual(worker.processed, 1)

    def test_provider_mutation_does_not_change_durable_job_callback(self):
        def change(source):
            result = receipt(source)
            source["amount"] = 999
            source["token"] = "changed"
            return result

        calls = []
        worker = Worker(CONFIG, 0, provider=Provider(change))
        self.addCleanup(worker.close)
        with patch(
            "hotel_reservation.worker.http_json", side_effect=self.fake_rpc(calls)
        ):
            self.assertTrue(worker.run_once())
        self.assertEqual(calls[-1][1]["job"], job())

    def test_unknown_provider_failure_never_becomes_decline_and_is_sanitized(self):
        def fail(_source):
            raise OSError("secret-token and private card material")

        calls = []
        worker = Worker(CONFIG, 0, provider=Provider(fail))
        self.addCleanup(worker.close)
        with (
            patch(
                "hotel_reservation.worker.http_json", side_effect=self.fake_rpc(calls)
            ),
            self.assertLogs("hotel_reservation.worker", level="WARNING") as logs,
        ):
            self.assertFalse(worker.run_once())
        self.assertEqual(calls[-1][0], "/internal/retry")
        self.assertNotIn(
            "secret-token", str(calls) + str(logs.output) + str(worker.last_error)
        )
        self.assertNotIn("/internal/finish", [path for path, _, _ in calls])

    def test_bad_receipts_retain_intent_instead_of_finishing(self):
        for invalid in (
            receipt(job()) | {"amount": True},
            receipt(job()) | {"status": "pending"},
            receipt(job()) | {"booking_id": "another"},
        ):
            calls = []
            worker = Worker(CONFIG, 0, provider=Provider(lambda _source: invalid))
            try:
                with (
                    patch(
                        "hotel_reservation.worker.http_json",
                        side_effect=self.fake_rpc(calls),
                    ),
                    self.assertLogs("hotel_reservation.worker", level="WARNING"),
                ):
                    self.assertFalse(worker.run_once())
                self.assertEqual(calls[-1][0], "/internal/retry")
                self.assertEqual(worker.processed, 0)
            finally:
                worker.close()

    def test_malformed_claim_never_reaches_provider(self):
        provider, calls = Provider(), []
        worker = Worker(CONFIG, 0, provider=provider)
        self.addCleanup(worker.close)
        with (
            patch(
                "hotel_reservation.worker.http_json",
                side_effect=self.fake_rpc(calls, job(amount=True)),
            ),
            self.assertLogs("hotel_reservation.worker", level="WARNING"),
        ):
            self.assertFalse(worker.run_once())
        self.assertEqual(provider.calls, [])
        self.assertEqual(
            [path for path, _, _ in calls], ["/internal/sweep", "/internal/claim"]
        )

    def test_explicit_decline_is_forwarded_as_a_provider_receipt(self):
        calls = []
        worker = Worker(
            CONFIG, 0, provider=Provider(lambda source: receipt(source, "declined"))
        )
        self.addCleanup(worker.close)
        with patch(
            "hotel_reservation.worker.http_json", side_effect=self.fake_rpc(calls)
        ):
            self.assertTrue(worker.run_once())
        self.assertEqual(calls[-1][1]["result"]["status"], "declined")

    def test_retry_callback_failure_leaves_lease_recoverable(self):
        calls = []
        basic = self.fake_rpc(calls)

        def rpc(url, token, body, **options):
            if url.endswith("/internal/retry"):
                raise OSError("retry callback down")
            return basic(url, token, body, **options)

        worker = Worker(
            CONFIG,
            0,
            provider=Provider(
                lambda _source: (_ for _ in ()).throw(OSError("unknown"))
            ),
        )
        self.addCleanup(worker.close)
        with (
            patch("hotel_reservation.worker.http_json", side_effect=rpc),
            self.assertLogs("hotel_reservation.worker", level="WARNING"),
        ):
            self.assertFalse(worker.run_once())
        self.assertIn("lease recovery", worker.last_error)
        self.assertEqual(worker.processed, 0)

    def test_close_waits_for_custom_provider_and_does_not_ack_after_stop(self):
        entered, release, closed = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        def slow(source):
            entered.set()
            release.wait(3)
            return receipt(source)

        calls = []
        worker = Worker(CONFIG, 0, provider=Provider(slow))
        with (
            patch(
                "hotel_reservation.worker.http_json", side_effect=self.fake_rpc(calls)
            ),
            self.assertLogs("hotel_reservation.worker", level="WARNING"),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            running = executor.submit(worker.run_once)
            try:
                self.assertTrue(entered.wait(1))
                closing = executor.submit(lambda: (worker.close(), closed.set()))
                self.assertFalse(closed.wait(0.05))
            finally:
                release.set()
            self.assertFalse(running.result(2))
            closing.result(2)
        self.assertNotIn("/internal/finish", [path for path, _, _ in calls])
        self.assertNotIn("/internal/retry", [path for path, _, _ in calls])

    def test_close_signals_default_network_cancellation(self):
        entered = threading.Event()
        worker = Worker(CONFIG, 0)

        def rpc(url, token, body, **options):
            entered.set()
            options["cancel"].wait(2)
            raise TimeoutError("cancelled")

        with (
            patch("hotel_reservation.worker.http_json", side_effect=rpc),
            self.assertLogs("hotel_reservation.worker", level="WARNING"),
        ):
            worker.start()
            self.assertTrue(entered.wait(1))
            worker.close()
        self.assertFalse(worker._thread.is_alive())

    def test_lifecycle_rejects_restart_and_self_join_without_changing_state(self):
        worker = Worker(CONFIG, 0, provider=Provider())

        def callback(source):
            with self.assertRaises(RuntimeError):
                worker.close()
            self.assertFalse(worker._closed)
            return receipt(source)

        worker.provider = Provider(callback)
        with patch("hotel_reservation.worker.http_json", side_effect=self.fake_rpc([])):
            self.assertTrue(worker.run_once())
        worker.close()
        worker.close()
        with self.assertRaises(RuntimeError):
            worker.start()
        with self.assertRaises(RuntimeError):
            worker.run_once()

    def test_configuration_is_owned_and_durations_strict(self):
        config = {**CONFIG, "shard_urls": ["http://shard"]}
        worker = Worker(config, 0, provider=Provider())
        config["shard_urls"][0] = "http://changed"
        self.assertEqual(worker.url, "http://shard")
        worker.close()
        for options in ({"timeout": True}, {"interval": 0}, {"timeout": float("inf")}):
            with self.assertRaises(ValueError):
                Worker(CONFIG, 0, **options)


class WorkerSagaTests(unittest.TestCase):
    def test_cancel_during_payment_requires_compensation_without_confirming(self):
        with tempfile.TemporaryDirectory() as directory:
            now = datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp()
            store = Store(Path(directory) / "hotel.sqlite", clock=lambda: now)
            store.manage("hotel", {"name": "Hotel", "address": "Address"})
            store.manage("hotel", {"name": "King", "description": "Room"}, "king")
            store.set_days(
                "hotel",
                "king",
                [{"day": "2030-01-02", "capacity": 1, "overbook": 0, "price": 10000}],
            )
            booking = store.reserve(
                "alice",
                {
                    "hotel": "hotel",
                    "room": "king",
                    "start": "2030-01-02",
                    "end": "2030-01-03",
                    "quantity": 1,
                    "request_key": "one",
                    "expected_total": 10000,
                    "payment_method": "test-ok",
                },
            )
            ledger = SimulatedLedger(Path(directory) / "payments.sqlite")

            class CancelProvider(PaymentAdapter):
                def execute(_self, source):
                    result = ledger.execute(
                        {key: value for key, value in source.items() if key != "token"}
                    )
                    if source["action"] == "charge":
                        store.cancel(booking["id"], "alice")
                    return result

            def rpc(url, token, body, **options):
                if url.endswith("/sweep"):
                    return {"expired": store.sweep()}
                if url.endswith("/claim"):
                    return {"job": store.claim()}
                if url.endswith("/finish"):
                    return {"settled": store.finish(body["job"], body["result"])}
                if url.endswith("/retry"):
                    store.retry(body["job"], body["error"])
                    return {"retained": True}
                raise AssertionError(url)

            worker = Worker(CONFIG, 0, provider=CancelProvider())
            try:
                with patch("hotel_reservation.worker.http_json", side_effect=rpc):
                    self.assertTrue(worker.run_once())
                    self.assertEqual(
                        store.booking(booking["id"])["payment"], "REFUND_PENDING"
                    )
                    self.assertTrue(worker.run_once())
                result = store.booking(booking["id"])
                self.assertEqual(
                    (result["status"], result["payment"]), ("CANCELLED", "REFUNDED")
                )
                self.assertEqual(
                    store.availability("hotel", "king", "2030-01-02", "2030-01-03")[
                        "available"
                    ],
                    1,
                )
                self.assertEqual(ledger.status()["operations"], 2)
            finally:
                worker.close()


if __name__ == "__main__":
    unittest.main()
