from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import test_store  # noqa: F401
from payment_system.worker import run, work_once


CONFIG = {
    "api_url": "http://api",
    "internal_token": "token",
    "receiver_url": "http://receiver",
    "event_token": "event",
    "provider_url": "http://provider",
    "provider_token": "provider",
}
OPERATION = {
    "id": "op",
    "merchant": "seller",
    "kind": "payin",
    "parent": None,
    "amount": 100,
    "currency": "USD",
    "status": "CREATED",
}


class Provider:
    def __init__(self, query_error=None):
        self.calls, self.query_error = [], query_error

    def query(self, operation, **options):
        self.calls.append(("query", operation, options))
        if self.query_error:
            raise self.query_error
        return {**OPERATION, "status": "SUCCEEDED"}

    def create(self, operation, **options):
        self.calls.append(("create", operation["id"], options))
        return {**OPERATION, "status": "SUCCEEDED"}


class WorkerTests(unittest.TestCase):
    def execute(self, provider):
        calls, stop = [], threading.Event()
        job = {
            "id": "execute:op",
            "token": "lease",
            "kind": "execute",
            "payload": {"operation": "op"},
        }

        def rpc(url, token, body=None, **options):
            calls.append((url, body, options))
            if url.endswith("/claim"):
                return {"job": job}
            if "/operation/" in url:
                return dict(OPERATION)
            return {"applied": True, "finished": True}

        with patch("payment_system.worker.http_json", side_effect=rpc):
            self.assertTrue(work_once(CONFIG, stop, provider=provider))
        return calls

    def test_only_explicit_404_creates_original_provider_identity(self):
        provider = Provider(HTTPError("url", 404, "missing", {}, BytesIO(b"{}")))
        calls = self.execute(provider)
        self.assertEqual(
            [call[:2] for call in provider.calls], [("query", "op"), ("create", "op")]
        )
        self.assertEqual(
            len(
                {call[2]["deadline"] for call in calls}
                | {call[2]["deadline"] for call in provider.calls}
            ),
            1,
        )

    def test_unknown_query_result_never_creates_new_payment(self):
        provider = Provider(TimeoutError("unknown"))
        calls = self.execute(provider)
        self.assertEqual([call[0] for call in provider.calls], ["query"])
        self.assertIn("error", calls[-1][1])

    def test_stopped_worker_never_claims(self):
        stop = threading.Event()
        stop.set()
        with patch("payment_system.worker.http_json") as rpc:
            self.assertFalse(work_once(CONFIG, stop))
            rpc.assert_not_called()

    def test_wrong_provider_operation_never_applies_or_finishes_successfully(self):
        provider = Provider()
        with patch.object(
            provider,
            "query",
            return_value={**OPERATION, "id": "other", "status": "SUCCEEDED"},
        ):
            calls = self.execute(provider)
        self.assertFalse(any(url.endswith("/apply") for url, _, _ in calls))
        self.assertIn("error", calls[-1][1])

    def test_parallel_executor_threads_are_owned_and_drained(self):
        stop, entered = threading.Event(), threading.Barrier(3)

        def work(*args, **kwargs):
            entered.wait(timeout=2)
            stop.wait(timeout=2)
            return False

        with (
            patch("payment_system.worker.work_once", side_effect=work),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            active = pool.submit(run, CONFIG, stop, 2)
            entered.wait(timeout=2)
            stop.set()
            active.result(timeout=2)
        self.assertFalse(
            any(
                thread.name.startswith("payment-executor-")
                for thread in threading.enumerate()
            )
        )
