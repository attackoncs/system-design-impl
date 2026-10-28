"""Real durable outbox/TSDB with explicitly controlled transports and clocks."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from metrics_monitor.collector import Collector, Dispatcher, partition_for
from metrics_monitor.common import Error, dumps
from metrics_monitor.discovery import CollectorRing, ScrapeScheduler, target_config
from metrics_monitor.pipeline import consume_once
from metrics_monitor.queue_adapter import QueueAdapter
from metrics_monitor.storage import Store


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.now = [2_000_000_000.0]
        self.collector = Collector(
            self.root / "collector.sqlite", clock=lambda: self.now[0], partitions=3
        )

    def sample(self, metric="cpu", value=1):
        return {
            "metric": metric,
            "labels": {"instance": "host"},
            "timestamp": self.now[0],
            "value": value,
        }

    def rows(self):
        with self.collector.transaction() as db:
            return [
                dict(row) for row in db.execute("SELECT * FROM outbox ORDER BY rowid")
            ]

    def metrics_for_partitions(self):
        found = {}
        for index in range(100):
            value = self.sample("m" + str(index))
            found.setdefault(partition_for(value, 3, "metric"), value["metric"])
        self.assertEqual(len(found), 3)
        return found

    def test_chunk_partition_and_size_contract(self):
        samples = [self.sample("m" + str(i), i) for i in range(300)]
        self.collector.enqueue(samples, "batch")
        for row in self.rows():
            self.assertLessEqual(len(row["payload"].encode()), 12000)
            body = json.loads(row["payload"])
            self.assertLessEqual(len(body["samples"]), 100)
            for sample in body["samples"]:
                self.assertEqual(partition_for(sample, 3, "metric"), row["partition"])

    def test_input_is_frozen_before_writer_wait_and_replay(self):
        sample = self.sample()
        original = json.loads(json.dumps(sample))
        from contextlib import contextmanager

        transaction = self.collector.transaction

        @contextmanager
        def changing(write=False):
            if write:
                sample["value"] = 99
                sample["labels"]["instance"] = "changed"
            with transaction(write) as db:
                yield db

        with patch.object(self.collector, "transaction", changing):
            first = self.collector.enqueue([sample], "stable")
        self.assertEqual(
            json.loads(self.rows()[0]["payload"])["samples"][0]["value"], 1
        )
        self.assertEqual(first, self.collector.enqueue([original], "stable"))

    def test_backlog_accounting_rollback_and_parallel_send(self):
        first = self.collector.enqueue([self.sample()], "first")
        size = self.collector.health()["pending_bytes"]
        self.collector.max_bytes = size
        with self.assertRaises(Error) as caught:
            self.collector.enqueue([self.sample(value=2)], "overflow")
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(self.collector.health()["pending_bytes"], size)
        self.assertEqual(self.collector.enqueue([self.sample()], "first"), first)
        queue = type("Queue", (), {"publish": lambda _self, _payload: None})()
        with ThreadPoolExecutor(max_workers=6) as executor:
            outcomes = list(
                executor.map(lambda _: self.collector.flush_once(queue), range(6))
            )
        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(self.collector.health()["pending_bytes"], 0)

    def test_partition_fifo_and_other_partition_progress(self):
        metrics = self.metrics_for_partitions()
        for index, partition in enumerate((0, 0, 1)):
            self.collector.enqueue(
                [self.sample(metrics[partition], index)], "r" + str(index)
            )
        calls = []

        class Queue:
            def publish(_self, payload):
                body = json.loads(payload)
                calls.append(body)
                if body["partition"] == 0:
                    raise OSError("controlled unavailable partition")

        with self.assertLogs("metrics_monitor.collector", level="WARNING"):
            self.assertFalse(self.collector.flush_once(Queue()))
        self.assertTrue(self.collector.flush_once(Queue()))
        self.assertEqual([body["partition"] for body in calls], [0, 1])
        self.now[0] += 1
        sent = []
        queue = type(
            "Queue",
            (),
            {"publish": lambda _self, payload: sent.append(json.loads(payload))},
        )()
        self.assertTrue(self.collector.flush_once(queue))
        self.assertTrue(self.collector.flush_once(queue))
        self.assertEqual([body["samples"][0]["value"] for body in sent], [0, 1])

    def test_stale_claim_cannot_clear_new_claim_or_decrement_bytes_twice(self):
        self.collector.enqueue([self.sample()], "old")

        def replace(_queue, _payload):
            with self.collector.transaction(True) as db:
                db.execute("UPDATE outbox SET owner='replacement',lease=9999999999")

        queue = type("Queue", (), {"publish": replace})()
        size = self.collector.health()["pending_bytes"]
        self.assertFalse(self.collector.flush_once(queue))
        self.assertEqual(self.rows()[0]["owner"], "replacement")
        self.assertEqual(self.collector.health()["pending_bytes"], size)

    def test_expired_claim_recovery_reuses_payload_after_reopen(self):
        self.collector.enqueue([self.sample()], "old")
        body = self.rows()[0]["payload"]
        with self.collector.transaction(True) as db:
            db.execute("UPDATE outbox SET owner='crashed',lease=?", (self.now[0] + 1,))
        self.now[0] += 2
        reopened = Collector(
            self.collector.path, clock=lambda: self.now[0], partitions=3
        )
        sent = []
        queue = type(
            "Queue", (), {"publish": lambda _self, payload: sent.append(payload)}
        )()
        self.assertTrue(reopened.flush_once(queue))
        self.assertEqual(sent, [body])
        self.assertEqual(reopened.health()["pending_bytes"], 0)

    def test_dispatcher_close_waits_for_accepted_calls(self):
        entered, release, closed = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        class SlowCollector:
            def flush_once(_self, _queue):
                entered.set()
                release.wait(3)
                return True

        dispatcher = Dispatcher(SlowCollector(), object(), workers=2)
        with ThreadPoolExecutor(max_workers=2) as executor:
            running = executor.submit(dispatcher.run_once)
            try:
                self.assertTrue(entered.wait(1))
                closing = executor.submit(lambda: (dispatcher.close(), closed.set()))
                self.assertFalse(closed.wait(0.05))
            finally:
                release.set()
            self.assertTrue(running.result(2))
            closing.result(2)
        with self.assertRaises(RuntimeError):
            dispatcher.run_once()

    def test_scrape_trusted_labels_and_failure_up(self):
        target = {
            "instance": "real",
            "url": "http://unused",
            "labels": {"region": "west"},
            "retries": 1,
        }
        exporter = {
            "samples": [
                {**self.sample(), "labels": {"instance": "fake", "region": "fake"}}
            ]
        }
        with patch(
            "metrics_monitor.collector.http_json",
            side_effect=[OSError("lost"), exporter],
        ) as transport:
            self.collector.scrape(target)
        self.assertEqual(transport.call_count, 2)
        samples = [
            sample
            for row in self.rows()
            for sample in json.loads(row["payload"])["samples"]
        ]
        self.assertTrue(
            all(
                sample["labels"] == {"instance": "real", "region": "west"}
                for sample in samples
            )
        )
        self.assertEqual(
            next(s for s in samples if s["metric"] == "collector_up")["value"], 1
        )
        with patch("metrics_monitor.collector.http_json", side_effect=OSError("fail")):
            self.collector.scrape({**target, "instance": "down"})
        self.assertIn("OSError", str(self.collector.health()["scrapes"]))

    def adapter(self):
        adapter = QueueAdapter("http://unused", "private-token", partitions=3)
        adapter._ready = True
        return adapter

    def test_queue_lost_ack_and_partition_producer_independence(self):
        adapter = self.adapter()
        calls, failed = [], [False]

        def call(path, **body):
            if path == "/producer":
                return {"producer": body["producer"], "epoch": 0}
            calls.append(json.loads(json.dumps(body)))
            if not failed[0]:
                failed[0] = True
                raise OSError("lost acknowledgement")
            return {"committed": True, "partition": body["partition"], "offsets": [0]}

        adapter.call = call
        original = dumps({"batch_id": "x", "partition": 0, "samples": [self.sample()]})
        with self.assertRaises(OSError):
            adapter.publish(original)
        with self.assertRaises(Error):
            adapter.publish(dumps({"partition": 0, "samples": []}))
        adapter.publish(original)
        adapter.publish(
            dumps({"batch_id": "y", "partition": 1, "samples": [self.sample()]})
        )
        self.assertEqual(calls[0], calls[1])
        self.assertNotEqual(calls[1]["producer"], calls[2]["producer"])

    def test_queue_fetch_rotates_all_assignments(self):
        adapter = self.adapter()
        membership = {"session": "s", "generation": 1, "assignments": [0, 2]}
        seen = []

        def call(path, **body):
            if path in ("/join", "/heartbeat"):
                return membership
            seen.append(body["partition"])
            return {"records": []}

        adapter.call = call
        for _ in range(4):
            adapter.fetch()
        self.assertEqual(seen, [0, 2, 0, 2])

    def test_queue_ensure_requires_explicit_expansion(self):
        adapter = QueueAdapter("http://unused", "private-token", partitions=3)

        def call(path, **body):
            if path == "/topic":
                raise HTTPError("http://unused", 409, "exists", {}, None)
            return {"topics": [{"name": "metrics", "partitions": 1}]}

        adapter.call = call
        with self.assertRaises(Error):
            adapter.ensure()
        self.assertFalse(adapter._ready)

    def test_temporary_storage_failure_is_not_poison_acknowledged(self):
        store = Store(self.root / "store.sqlite", clock=lambda: self.now[0])

        class Queue:
            def __init__(_self):
                _self.calls = []

            def fetch(_self):
                return {"partition": 0}, {
                    "records": [
                        {"value": dumps({"batch_id": "x", "samples": [self.sample()]})}
                    ],
                    "next_offset": 1,
                }

            def call(_self, *args, **kwargs):
                _self.calls.append(args)

        queue = Queue()
        with patch.object(
            store, "ingest", side_effect=Error(503, "temporarily unavailable")
        ):
            with self.assertRaises(Error):
                consume_once(store, queue)
        self.assertEqual(queue.calls, [])
        with store.transaction() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM rejections").fetchone()[0], 0
            )

    def test_poison_is_durable_before_cursor_commit(self):
        store = Store(self.root / "store.sqlite", clock=lambda: self.now[0])

        class Queue:
            def fetch(_self):
                return {"partition": 0}, {
                    "records": [{"value": '{"batch_id":"x","samples":[]}'}],
                    "next_offset": 1,
                }

            def call(_self, path, **body):
                with store.transaction() as db:
                    self.assertEqual(
                        db.execute("SELECT count(*) FROM rejections").fetchone()[0], 1
                    )
                self.assertEqual(path, "/commit")

        self.assertTrue(consume_once(store, Queue()))


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.collector = Collector(
            self.root / "collector.sqlite", clock=lambda: 2_000_000_000
        )
        self.path = self.root / "targets.json"
        self.now = [100.0]

    def write(self, targets):
        self.path.write_text(json.dumps(targets))

    def test_ring_single_owner_and_expansion_moves_only_to_new_member(self):
        old, new = CollectorRing(["a", "b"]), CollectorRing(["b", "c", "a"])
        reverse = CollectorRing(["b", "a"])
        for index in range(1000):
            instance = "host" + str(index)
            before, after = old.owner(instance), new.owner(instance)
            self.assertEqual(before, reverse.owner(instance))
            if before != after:
                self.assertEqual(after, "c")

    def test_interval_reload_invalid_config_retains_last_valid_and_recovers(self):
        self.write([{"instance": "host", "url": "http://unused", "interval": 10}])
        scheduler = ScrapeScheduler(
            self.collector, self.path, clock=lambda: self.now[0]
        )
        self.addCleanup(scheduler.close)
        with patch.object(self.collector, "scrape", return_value={}) as scrape:
            self.assertEqual(scheduler.run_once(), 1)
            self.assertEqual(scheduler.run_once(), 0)
            self.path.write_text("not json")
            self.now[0] += 10
            with self.assertLogs("metrics_monitor.discovery", level="WARNING"):
                self.assertEqual(scheduler.run_once(), 1)
            self.assertIsNotNone(scheduler.last_error)
            self.write([])
            self.assertEqual(scheduler.run_once(), 0)
            self.assertIsNone(scheduler.last_error)
            self.assertEqual(scrape.call_count, 2)
        self.assertEqual(self.collector.health()["scrapes"][0]["error"], "")

    def test_membership_schedulers_partition_targets_without_duplicate_scrapes(self):
        targets = [
            {"instance": "host" + str(i), "url": "http://unused"} for i in range(16)
        ]
        self.write(targets)
        observed = []
        with patch.object(
            self.collector,
            "scrape",
            side_effect=lambda target: observed.append(target["instance"]),
        ):
            for member in ("a", "b"):
                scheduler = ScrapeScheduler(
                    self.collector,
                    self.path,
                    workers=16,
                    collector_id=member,
                    collectors=["a", "b"],
                    clock=lambda: self.now[0],
                )
                try:
                    scheduler.run_once()
                finally:
                    scheduler.close()
        self.assertEqual(sorted(observed), sorted(t["instance"] for t in targets))

    def test_bounded_workers_and_fairness(self):
        self.write(
            [{"instance": "h" + str(i), "url": "http://unused"} for i in range(5)]
        )
        active, peak, seen = [0], [0], []
        lock, both = threading.Lock(), threading.Barrier(2)

        def scrape(target):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
                seen.append(target["instance"])
            both.wait(timeout=2)
            with lock:
                active[0] -= 1

        scheduler = ScrapeScheduler(
            self.collector, self.path, workers=2, clock=lambda: self.now[0]
        )
        try:
            with patch.object(self.collector, "scrape", side_effect=scrape):
                self.assertEqual(scheduler.run_once(), 2)
                self.assertEqual(scheduler.run_once(), 2)
            with patch.object(
                self.collector,
                "scrape",
                side_effect=lambda target: seen.append(target["instance"]),
            ):
                self.assertEqual(scheduler.run_once(), 1)
            self.assertEqual(peak[0], 2)
            self.assertEqual(len(set(seen)), 5)
        finally:
            scheduler.close()
        with self.assertRaises(RuntimeError):
            scheduler.run_once()

    def test_config_validation_before_target_side_effects(self):
        for target in (
            {"instance": "h", "url": "ftp://unused"},
            {"instance": "h", "url": "http://unused", "timeout": float("inf")},
            {"instance": "h", "url": "http://unused", "retries": True},
            {"instance": "_discovery", "url": "http://unused"},
        ):
            with self.assertRaises(ValueError):
                target_config(target, 5)
        with self.assertRaises(ValueError):
            CollectorRing(["a", "a"])


if __name__ == "__main__":
    unittest.main()
