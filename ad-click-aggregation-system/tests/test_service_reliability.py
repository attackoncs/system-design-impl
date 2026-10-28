"""Actual executors/SQLite with controlled RPC responses and failure gates."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ad_click.aggregate import Aggregator
from ad_click.common import Error
from ad_click.log import EventLog
from ad_click.query_service import QueryService
from ad_click.recovery import reconcile, replay


def config(partitions=4):
    return {
        "ingress_url": "http://source",
        "worker_urls": ["http://worker" + str(p) for p in range(partitions)],
        "internal_token": "private-token",
    }


def part(partition, totals, **changes):
    return {
        "totals": totals,
        "checkpoint": {
            "partition": partition,
            "lineage": "test-lineage",
            "seq": 10,
            "watermark": 1000,
        },
        "closed": False,
        **changes,
    }


class QueryTests(unittest.TestCase):
    def rpc(self, url, token, body=None, **kwargs):
        if url.startswith("http://source"):
            return {"lineage": "test-lineage", "heads": {str(p): 10 for p in range(4)}}
        partition = int(url.split("worker")[1].split("/")[0])
        return part(partition, {"hot": 6, "local" + str(partition): 9})

    def test_exact_global_top_n_never_truncates_each_worker_first(self):
        with (
            QueryService(config()) as service,
            patch("ad_click.query_service.http_json", side_effect=self.rpc),
        ):
            result = service.aggregate({"from": 600, "to": 660, "count": 1})
        self.assertEqual(result["ads"], [{"ad_id": "hot", "count": 24}])
        self.assertTrue(result["closed"])

    def test_deterministic_ties_and_missing_ad(self):
        def rpc(url, token, body=None, **kwargs):
            result = self.rpc(url, token, body, **kwargs)
            if "totals" in result:
                result["totals"] = {} if "ad_id" in body else {"b": 1, "a": 1}
            return result

        with (
            QueryService(config()) as service,
            patch("ad_click.query_service.http_json", side_effect=rpc),
        ):
            result = service.aggregate({"from": 600, "to": 660})
            self.assertEqual([row["ad_id"] for row in result["ads"]], ["a", "b"])
            self.assertEqual(
                service.aggregate({"from": 600, "to": 660, "ad_id": "missing"})[
                    "count"
                ],
                0,
            )

    def test_worker_candidate_budget_preserves_422(self):
        def rpc(url, token, body=None, **kwargs):
            if "worker0" in url:
                raise HTTPError(url, 422, "budget", {}, None)
            return self.rpc(url, token, body, **kwargs)

        with (
            QueryService(config()) as service,
            patch("ad_click.query_service.http_json", side_effect=rpc),
            self.assertRaises(Error) as caught,
        ):
            service.aggregate({"from": 600, "to": 660})
        self.assertEqual(caught.exception.status, 422)

    def test_malformed_or_missing_worker_never_returns_partial_ranking(self):
        for malformed in (
            {"totals": {"hot": True}},
            {"totals": {"hot": -1}},
            {
                "checkpoint": {
                    "partition": 0,
                    "lineage": "wrong",
                    "seq": 10,
                    "watermark": 1,
                }
            },
            {
                "checkpoint": {
                    "partition": 0,
                    "lineage": "test-lineage",
                    "seq": True,
                    "watermark": 1,
                }
            },
            {
                "checkpoint": {
                    "partition": 0,
                    "lineage": "test-lineage",
                    "seq": 10,
                    "watermark": float("nan"),
                }
            },
        ):

            def rpc(url, token, body=None, **kwargs):
                result = self.rpc(url, token, body, **kwargs)
                return {**result, **malformed} if "worker0" in url else result

            with (
                QueryService(config()) as service,
                patch("ad_click.query_service.http_json", side_effect=rpc),
                self.assertRaises(Error) as caught,
            ):
                service.aggregate({"from": 600, "to": 660})
            self.assertEqual(caught.exception.status, 503)

    def test_failure_cancels_other_accepted_work(self):
        cancelled = threading.Event()

        def rpc(url, token, body=None, **kwargs):
            if "worker0" in url:
                raise OSError("controlled missing worker")
            if "worker" in url:
                if kwargs["cancel"].wait(2):
                    cancelled.set()
                raise OSError("cancelled")
            return self.rpc(url, token, body, **kwargs)

        with (
            QueryService(config(), workers=4) as service,
            patch("ad_click.query_service.http_json", side_effect=rpc),
            self.assertRaises(Error),
        ):
            service.aggregate({"from": 600, "to": 660})
        # It is also valid for all other tasks to have been cancelled before start.
        self.assertEqual(service._active, 0)

    def test_capacity_and_close_wait_for_real_accepted_work(self):
        entered, release, closed = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        def rpc(url, token, body=None, **kwargs):
            if "worker" in url:
                entered.set()
                release.wait(3)
            return self.rpc(url, token, body, **kwargs)

        service = QueryService(config(), workers=1, capacity=1)
        with (
            patch("ad_click.query_service.http_json", side_effect=rpc),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            running = executor.submit(service.aggregate, {"from": 600, "to": 660})
            try:
                self.assertTrue(entered.wait(1))
                with self.assertRaises(Error) as caught:
                    service.aggregate({"from": 600, "to": 660})
                self.assertEqual(caught.exception.status, 503)
                closing = executor.submit(lambda: (service.close(), closed.set()))
                self.assertFalse(closed.wait(0.05))
            finally:
                release.set()
            running.result(3)
            closing.result(3)
        with self.assertRaises(RuntimeError):
            service.aggregate({"from": 600, "to": 660})

    def test_one_deadline_cancels_waiting_workers(self):
        def rpc(url, token, body=None, **kwargs):
            if "worker" in url:
                kwargs["cancel"].wait(2)
                raise OSError("cancelled")
            return self.rpc(url, token, body, **kwargs)

        with (
            QueryService(config(), workers=2, timeout=0.1) as service,
            patch("ad_click.query_service.http_json", side_effect=rpc),
        ):
            started = time.monotonic()
            with self.assertRaises(Error):
                service.aggregate({"from": 600, "to": 660})
            self.assertLess(time.monotonic() - started, 1)

    def test_config_is_owned_and_invalid_options_rejected(self):
        original = config()
        with QueryService(original) as service:
            original["worker_urls"].clear()
            self.assertEqual(len(service.config["worker_urls"]), 4)
        for options in ({"workers": True}, {"capacity": 0}, {"timeout": float("nan")}):
            with self.assertRaises(ValueError):
                QueryService(config(), **options)

    def test_source_topology_is_complete(self):
        with (
            QueryService(config(3)) as service,
            patch("ad_click.query_service.http_json", side_effect=self.rpc),
            self.assertRaises(Error),
        ):
            service.aggregate({"from": 600, "to": 660})

    def test_global_candidate_budget_after_merge(self):
        def rpc(url, token, body=None, **kwargs):
            result = self.rpc(url, token, body, **kwargs)
            if "totals" in result:
                partition = result["checkpoint"]["partition"]
                result["totals"] = {f"a{partition}_{i}": 1 for i in range(3000)}
            return result

        with (
            QueryService(config()) as service,
            patch("ad_click.query_service.http_json", side_effect=rpc),
            self.assertRaises(Error) as caught,
        ):
            service.aggregate({"from": 600, "to": 660})
        self.assertEqual(caught.exception.status, 422)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.log = EventLog(self.root / "raw.sqlite", partitions=1, clock=lambda: 1000)
        self.events = [
            {
                "event_id": str(i),
                "ad_id": "ad",
                "click_timestamp": 601,
                "user_id": "u",
                "ip": "127.0.0.1",
                "country": "US",
            }
            for i in range(4)
        ]
        self.log.append(self.events)
        self.worker = Aggregator(self.root / "worker.sqlite", 0, clock=lambda: 1000)
        self.worker.apply(self.log.read(0, 0))

    def rpc(self, url, token, body=None, **kwargs):
        if url.endswith("/internal/status"):
            return self.log.status()
        if url.endswith("/internal/audit"):
            return self.worker.audit()
        params = parse_qs(urlsplit(url).query)
        return self.log.read(0, int(params["after"][0]), until=int(params["until"][0]))

    def test_independent_batch_reconciliation_and_separate_replay(self):
        with patch("ad_click.recovery.http_json", side_effect=self.rpc):
            self.assertTrue(reconcile(config(1))["matches"])
            target = Aggregator(self.root / "rebuild.sqlite", 0, clock=lambda: 1000)
            result = replay(config(1), target)
        self.assertEqual(result["digest"], self.worker.audit()["digest"])
        self.assertEqual(result["count"], 4)

    def test_bad_replay_page_rejected_before_output_or_checkpoint(self):
        for mutation in ("hash", "gap", "empty", "overshoot", "identity"):

            def rpc(url, token, body=None, **kwargs):
                result = self.rpc(url, token, body, **kwargs)
                if "/internal/log?" in url:
                    result = json.loads(json.dumps(result))
                    if mutation == "hash":
                        result["entries"][0]["hash"] = "bad"
                    if mutation == "gap":
                        result["entries"][0]["seq"] = 2
                    if mutation == "empty":
                        result["entries"] = []
                    if mutation == "overshoot":
                        result["entries"][-1]["seq"] = 5
                    if mutation == "identity":
                        result["lineage"] = "other"
                return result

            target = Aggregator(
                self.root / (mutation + ".sqlite"), 0, clock=lambda: 1000
            )
            with (
                patch("ad_click.recovery.http_json", side_effect=rpc),
                self.assertRaises(Error),
            ):
                replay(config(1), target)
            self.assertEqual(target.status()["seq"], 0)

    def test_reconciliation_mismatch_and_work_budget_are_explicit(self):
        def rpc(url, token, body=None, **kwargs):
            result = self.rpc(url, token, body, **kwargs)
            return (
                {**result, "digest": "bad"}
                if url.endswith("/internal/audit")
                else result
            )

        with patch("ad_click.recovery.http_json", side_effect=rpc):
            self.assertFalse(reconcile(config(1))["matches"])
            with self.assertRaises(Error) as caught:
                reconcile(config(1), max_events=1)
            self.assertEqual(caught.exception.status, 413)

    def test_replay_ahead_of_target_and_budget_rejected(self):
        with patch("ad_click.recovery.http_json", side_effect=self.rpc):
            with self.assertRaises(Error):
                replay(config(1), self.worker, until=1)
            target = Aggregator(self.root / "bounded.sqlite", 0, clock=lambda: 1000)
            with self.assertRaises(Error):
                replay(config(1), target, max_events=1)
        self.assertEqual(target.status()["seq"], 0)


if __name__ == "__main__":
    unittest.main()
