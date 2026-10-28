from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from message_queue.broker import Broker
from message_queue.common import Error
from message_queue.coordinator import Coordinator
from message_queue.coordination_locks import OperationLocks, partition_key
from message_queue.messages import freeze_messages


class CoordinatorReliabilityTests(unittest.TestCase):
    """Real SQLite/logs with controlled in-process transport and clock injection."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.now = [2_000_000_000.0]
        self.brokers = {f"b{i}": Broker(self.root / f"b{i}") for i in range(3)}
        self.failed = set()
        self.service = self.open()
        self.service.create("input", partitions=4)
        self.service.producer_open("p")

    def open(self):
        service = Coordinator(
            self.root / "coordinator",
            {node: "http://unused" for node in self.brokers},
            "private-token",
            lease_seconds=10,
            clock=lambda: self.now[0],
        )

        def call(node, path, body):
            if node in self.failed:
                raise OSError("controlled unavailable peer")
            return self.brokers[node].dispatch(path, body)

        service.call = call
        return service

    def produce(self, partition=0, sequence=0, producer="p", messages=None, **extra):
        return self.service.produce(
            "input",
            producer,
            0,
            sequence,
            messages or [{"value": "work"}],
            partition=partition,
            **extra,
        )

    def source(self, member=None):
        member = member or self.service.join("input", "workers", "a")
        return dict(
            topic="input",
            group="workers",
            member=member["member"],
            session=member["session"],
            generation=member["generation"],
            partition=0,
        )

    def assert_status(self, status, action, *args, **kwargs):
        with self.assertRaises(Error) as caught:
            action(*args, **kwargs)
        self.assertEqual(caught.exception.status, status)

    def gate(self, path, predicate=lambda body: True):
        entered, release = threading.Event(), threading.Event()
        old = self.service.call

        def call(node, operation, body):
            if operation == path and predicate(body):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test release was not signaled")
            return old(node, operation, body)

        self.service.call = call
        self.addCleanup(release.set)
        return entered, release

    def test_slow_partition_does_not_block_metadata_other_partition_or_heartbeat(self):
        member = self.service.join("input", "workers", "a")
        self.service.producer_open("other")
        first_stripes = {
            self.service.operations.index(partition_key("input", 0)),
            self.service.operations.index(("producer", "p")),
        }
        other_partition = next(
            p
            for p in range(1, 4)
            if self.service.operations.index(partition_key("input", p))
            not in first_stripes
        )
        self.assertNotIn(
            self.service.operations.index(("producer", "other")), first_stripes
        )
        entered, release = self.gate("/sync", lambda body: body["partition"] == 0)
        with ThreadPoolExecutor(4) as pool:
            slow = pool.submit(self.produce)
            self.assertTrue(entered.wait(2))
            try:
                meta = pool.submit(self.service.dispatch, "/metadata", {}).result(2)
                self.assertEqual(meta["role"], "coordinator")
                heartbeat = pool.submit(
                    self.service.heartbeat, "input", "workers", "a", member["session"]
                ).result(2)
                self.assertEqual(heartbeat["session"], member["session"])
                fast = pool.submit(self.produce, other_partition, 0, "other").result(2)
                self.assertEqual(fast["offsets"], [0])
            finally:
                release.set()
            self.assertTrue(slow.result(3)["committed"])

    def test_direct_same_partition_concurrent_appends_have_unique_offsets(self):
        producers = [f"p{i}" for i in range(8)]
        for producer in producers:
            self.service.producer_open(producer)
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda p: self.produce(producer=p), producers))
        self.assertEqual(
            sorted(result["offsets"][0] for result in results), list(range(8))
        )
        self.assertEqual(len(self.service.fetch("input", 0)["records"]), 8)

    def test_direct_same_sequence_concurrent_retry_is_one_committed_batch(self):
        with ThreadPoolExecutor(6) as pool:
            results = list(pool.map(lambda _: self.produce(), range(6)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(len(self.service.fetch("input", 0)["records"]), 1)

    def test_implicit_batch_rejects_keys_spanning_partitions_before_staging(self):
        key = "first"
        target = zlib.crc32(key.encode()) % 4
        other = next(
            str(i) for i in range(100) if zlib.crc32(str(i).encode()) % 4 != target
        )
        messages = [{"value": "one", "key": key}, {"value": "two", "key": other}]
        with self.assertRaises(ValueError):
            self.service.produce("input", "p", 0, 0, messages)
        self.assertTrue(
            all(part["hwm"] == -1 for part in self.service.metadata()["partitions"])
        )
        with self.service.transaction(write=False) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM pending").fetchone()[0], 0
            )
        # An explicit destination deliberately overrides keyed routing.
        self.assertEqual(
            self.service.produce("input", "p", 0, 0, messages, partition=0)["offsets"],
            [0, 1],
        )

    def test_implicit_single_key_bucket_and_keyless_retries_remain_stable(self):
        key = "first"
        target = zlib.crc32(key.encode()) % 4
        other = next(
            str(i) for i in range(100) if zlib.crc32(str(i).encode()) % 4 == target
        )
        result = self.service.produce(
            "input",
            "p",
            0,
            0,
            [
                {"value": "no key"},
                {"value": "one", "key": key},
                {"value": "two", "key": other},
            ],
        )
        self.assertEqual(result["partition"], target)
        self.service.producer_open("keyless")
        body = [{"value": "none", "key": None}, {"value": "absent"}]
        first = self.service.produce("input", "keyless", 0, 0, body)
        self.assertEqual(first["partition"], zlib.crc32(b"keyless") % 4)
        self.assertEqual(self.service.produce("input", "keyless", 0, 0, body), first)

    def test_at_most_once_concurrent_requests_deliver_once(self):
        self.produce()
        source = self.source()
        with ThreadPoolExecutor(6) as pool:
            results = list(
                pool.map(
                    lambda _: self.service.group_fetch(
                        **source, semantics="at_most_once"
                    ),
                    range(6),
                )
            )
        self.assertEqual(sum(len(result["records"]) for result in results), 1)
        self.assertEqual(self.service.group_fetch(**source)["records"], [])

    def test_rebalance_during_remote_read_fences_result_without_delivery_bound(self):
        self.produce()
        source = self.source()
        entered, release = self.gate("/read")
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.service.group_fetch, **source)
            self.assertTrue(entered.wait(2))
            self.service.join("input", "workers", "b")
            release.set()
            self.assert_status(409, pending.result, 3)
        with self.service.transaction(write=False) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM delivered").fetchone()[0], 0
            )

    def test_producer_reopen_waits_for_accepted_append_and_then_fences_old_epoch(self):
        entered, release = self.gate("/sync")
        with ThreadPoolExecutor(2) as pool:
            append = pool.submit(self.produce)
            self.assertTrue(entered.wait(2))
            opened = pool.submit(self.service.producer_open, "p")
            try:
                time.sleep(0.05)
                self.assertFalse(opened.done())
            finally:
                release.set()
            self.assertTrue(append.result(3)["committed"])
            self.assertEqual(opened.result(3)["epoch"], 1)
        self.assert_status(409, self.produce, sequence=1)

    def test_input_batch_frozen_before_blocked_replication(self):
        messages = [{"value": "original", "tags": ["blue"]}]
        entered, release = self.gate("/sync")
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.produce, messages=messages)
            self.assertTrue(entered.wait(2))
            messages[0]["value"] = "mutated"
            messages[0]["tags"].append("red")
            release.set()
            pending.result(3)
        record = self.service.fetch("input", 0)["records"][0]
        self.assertEqual(record["value"], "original")
        self.assertEqual(record["tags"], ["blue"])
        self.assertEqual(
            self.produce(messages=[{"value": "original", "tags": ["blue"]}])["offsets"],
            [0],
        )

    def transaction_request(self, txid="tx1"):
        self.service.create("output")
        self.produce()
        source = self.source()
        self.service.group_fetch(**source)
        return dict(
            txid=txid,
            source=source,
            expected_offset=0,
            next_offset=1,
            outputs=[{"topic": "output", "messages": [{"value": "done"}]}],
        )

    def test_concurrent_same_transaction_retries_publish_once(self):
        body = self.transaction_request()
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _: self.service.process(**body), range(4)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(len(self.service.fetch("output", 0)["records"]), 1)

    def test_different_transactions_cannot_advance_same_input_twice(self):
        body = self.transaction_request()

        def process(txid):
            try:
                return self.service.process(**{**body, "txid": txid})
            except Error as exc:
                return exc.status

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(process, ["a", "b"]))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertIn(409, results)
        self.assertEqual(len(self.service.fetch("output", 0)["records"]), 1)

    def test_multi_output_remains_invisible_until_atomic_publication(self):
        body = self.transaction_request()
        self.service.create("output2")
        body["outputs"].append({"topic": "output2", "messages": [{"value": "second"}]})
        entered, release = self.gate("/sync", lambda body: body["topic"] == "output2")
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.service.process, **body)
            self.assertTrue(entered.wait(2))
            try:
                metadata = self.service.metadata()
                self.assertEqual(
                    [
                        p["hwm"]
                        for p in metadata["partitions"]
                        if p["topic"].startswith("output")
                    ],
                    [-1, -1],
                )
                with self.service.transaction(write=False) as db:
                    self.assertEqual(self.service._cursor(db, "input", "workers", 0), 0)
            finally:
                release.set()
            pending.result(3)
        self.assertEqual(len(self.service.fetch("output2", 0)["records"]), 1)

    def test_retention_failure_retried_after_restart_without_new_floor(self):
        self.service.create("short", retention_seconds=1)
        self.service.produce(
            "short", "p", 0, 0, [{"value": "short lived"}], partition=0
        )
        self.failed.add("b2")
        self.now[0] += 2
        with self.assertLogs("message_queue.coordinator", level="WARNING"):
            self.service.retain("short")
        with self.service.transaction(write=False) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM retention_pending").fetchone()[0], 1
            )
        self.service = self.open()
        self.failed.clear()
        self.service.maintenance()
        with self.service.transaction(write=False) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM retention_pending").fetchone()[0], 0
            )
        self.assertEqual(
            self.brokers["b2"].dispatch(
                "/status", dict(topic="short", partition=0, committed=0)
            )["base"],
            1,
        )
        self.assertIsNone(self.service.last_error)

    def test_empty_or_tampered_committed_peer_page_is_not_silently_delivered(self):
        self.produce()
        original = self.service.call
        for tamper in (
            lambda body: [],
            lambda body: [{"record": {"offset": 0}, "hash": "bad", "previous": "bad"}],
        ):

            def call(node, path, body):
                return (
                    {"records": tamper(body)}
                    if path == "/read"
                    else original(node, path, body)
                )

            self.service.call = call
            self.assert_status(503, self.service.fetch, "input", 0)
        self.service.call = original
        self.assertEqual(len(self.service.fetch("input", 0)["records"]), 1)

    def test_filter_advances_over_nonmatching_records_but_not_delayed_head(self):
        self.produce(
            messages=[
                {"value": "skip", "tags": ["red"]},
                {"value": "wait", "tags": ["red"], "delay_seconds": 5},
                {"value": "blue", "tags": ["blue"]},
            ]
        )
        result = self.service.fetch("input", 0, tags=["blue"])
        self.assertEqual(result["records"], [])
        self.assertEqual(result["next_offset"], 1)
        self.now[0] += 5
        result = self.service.fetch("input", 0, offset=1, tags=["blue"])
        self.assertEqual([record["value"] for record in result["records"]], ["blue"])
        self.assertEqual(result["next_offset"], 3)

    def test_group_filter_delivery_bound_can_commit_scanned_nonmatches(self):
        self.produce(
            messages=[
                {"value": "red", "tags": ["red"]},
                {"value": "blue", "tags": ["blue"]},
            ]
        )
        source = self.source()
        result = self.service.group_fetch(**source, tags=["missing"])
        self.assertEqual(result["records"], [])
        self.assertEqual(result["next_offset"], 2)
        self.service.commit(**source, next_offset=2)
        self.assertEqual(self.service.group_fetch(**source)["records"], [])

    def test_absolute_schedule_and_relative_delay_are_exclusive(self):
        self.produce(messages=[{"value": "scheduled", "deliver_at": self.now[0] + 10}])
        self.assertEqual(self.service.fetch("input", 0)["records"], [])
        self.now[0] += 10
        self.assertEqual(len(self.service.fetch("input", 0)["records"]), 1)
        with self.assertRaises(ValueError):
            self.produce(
                sequence=1,
                messages=[
                    {"value": "bad", "deliver_at": self.now[0], "delay_seconds": 0}
                ],
            )

    def test_leave_immediately_reassigns_and_fences_old_session_and_generation(self):
        first = self.service.join("input", "workers", "a")
        second = self.service.join("input", "workers", "b")
        first = self.service.heartbeat("input", "workers", "a", first["session"])
        self.service.leave("input", "workers", "b", second["session"])
        self.assert_status(409, self.service.group_fetch, **self.source(first))
        latest = self.service.heartbeat("input", "workers", "a", first["session"])
        self.assertEqual(latest["assignments"], [0, 1, 2, 3])
        self.assert_status(
            409, self.service.heartbeat, "input", "workers", "b", second["session"]
        )

    def test_expand_preserves_old_offsets_and_requires_group_refresh(self):
        self.produce()
        member = self.service.join("input", "workers", "a")
        result = self.service.expand("input", 6)
        self.assertEqual(result["previous_partitions"], 4)
        self.assert_status(409, self.service.group_fetch, **self.source(member))
        latest = self.service.heartbeat("input", "workers", "a", member["session"])
        self.assertEqual(latest["assignments"], list(range(6)))
        self.assertEqual(len(self.service.fetch("input", 0)["records"]), 1)
        self.assertEqual(self.produce(partition=5)["offsets"], [0])
        self.assert_status(409, self.service.expand, "input", 4)

    def test_long_poll_releases_partition_and_is_woken_by_committed_append(self):
        with ThreadPoolExecutor(2) as pool:
            future = pool.submit(self.service.fetch, "input", 0, wait_seconds=2)
            time.sleep(0.1)
            self.assertFalse(future.done())
            self.produce()
            self.assertEqual(len(future.result(2)["records"]), 1)

    def test_group_long_poll_rechecks_membership_after_leave(self):
        source = self.source()
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.service.group_fetch, **source, wait_seconds=2)
            time.sleep(0.1)
            self.service.leave("input", "workers", "a", source["session"])
            self.assert_status(409, future.result, 2)

    def test_empty_long_poll_timeout_and_invalid_wait_budget(self):
        start = time.monotonic()
        self.assertEqual(
            self.service.fetch("input", 0, wait_seconds=0.15)["records"], []
        )
        self.assertGreaterEqual(time.monotonic() - start, 0.12)
        for value in (True, float("nan"), float("inf"), 10**1000, -1, 6):
            with self.assertRaises(ValueError):
                self.service.fetch("input", 0, wait_seconds=value)

    def test_wal_metadata_read_does_not_require_writer_slot(self):
        with self.service.transaction():
            with ThreadPoolExecutor(1) as pool:
                self.assertEqual(
                    pool.submit(self.service.metadata).result(2)["role"], "coordinator"
                )

    def test_strict_group_generation_fields_and_message_contract(self):
        source = self.source()
        for generation in (True, None, -1, 10**1000):
            with self.assertRaises(ValueError):
                self.service.group_fetch(**{**source, "generation": generation})
        invalid_messages = [
            {"value": "x", "tags": ["tag", "tag"]},
            {"value": "x", "tags": "blue"},
            {"value": "x", "tags": ["bad tag"]},
            {"value": "x", "tags": ["a"] * 17},
            {"value": "x", "typo": 1},
            {"value": "x", "deliver_at": float("inf")},
            {"value": "x", "delay_seconds": True},
        ]
        for message in invalid_messages:
            with self.assertRaises(ValueError):
                self.produce(messages=[message])

    def test_constructor_rejects_invalid_lease_and_clock(self):
        for lease in (True, 0, -1, float("nan"), float("inf"), 10**1000):
            with self.assertRaises(ValueError):
                Coordinator(
                    self.root / "bad",
                    {"b": "http://unused"},
                    "private-token",
                    lease_seconds=lease,
                )
        with self.assertRaises(ValueError):
            Coordinator(
                self.root / "bad", {"b": "http://unused"}, "private-token", clock=None
            )

    def test_interrupted_lock_acquisition_releases_only_owned_locks(self):
        class Lock:
            def __init__(self, interrupt=False):
                self.interrupt, self.held = interrupt, False

            def acquire(self):
                if self.interrupt:
                    raise KeyboardInterrupt()
                self.held = True

            def release(self):
                if not self.held:
                    raise RuntimeError("unowned release")
                self.held = False

        operations = OperationLocks()
        first, second = Lock(), Lock(interrupt=True)
        operations._locks = (first, second)
        operations.index = lambda key: key
        with self.assertRaises(KeyboardInterrupt):
            with operations.hold(0, 1):
                self.fail("interrupted acquisition must not enter")
        self.assertFalse(first.held)

    def test_partial_pages_progress_and_catchup_reserves_pending_budget(self):
        self.produce(messages=[{"value": "a"}, {"value": "b"}, {"value": "c"}])
        original = self.service.call

        def call(node, path, body):
            response = original(node, path, body)
            if path == "/read":
                response["records"] = response["records"][:1]
            return response

        self.service.call = call
        self.assertEqual(self.service.fetch("input", 0)["next_offset"], 1)
        with self.service.transaction(write=False) as db:
            part = self.service.part(db, "input", 0)
        pending = self.service._values(
            part, freeze_messages([{"value": "d"}]), "p", 0, 1
        )
        self.assertEqual(len(self.service._retained(part, "b0", pending)), 3)
        with patch("message_queue.coordinator_replication.MAX_BATCH_RECORDS", 3):
            self.assert_status(503, self.service._retained, part, "b0", pending)
        with patch("message_queue.coordinator_replication.MAX_BATCH_BYTES", 1):
            self.assert_status(503, self.service._retained, part, "b0", pending)

    def test_restart_abandons_transaction_stage_without_exposing_broker_tail(self):
        body = self.transaction_request()
        with self.service.transaction(write=False) as db:
            part = self.service.part(db, "output", 0)
        pending = {
            "topic": "output",
            "partition": 0,
            "transaction": "abandoned",
            "values": self.service._values(
                part, freeze_messages([{"value": "orphan"}]), "tx_abandoned", 0, 0
            ),
        }
        from message_queue.common import dumps

        with self.service.transaction() as db:
            db.execute(
                "INSERT INTO pending VALUES(?,?,?)", ("output", 0, dumps(pending))
            )
        self.service._replicate(pending)
        self.service = self.open()
        self.assertEqual(self.service.fetch("output", 0)["records"], [])
        self.service.process(**body)
        self.assertEqual(
            [record["value"] for record in self.service.fetch("output", 0)["records"]],
            ["done"],
        )

    def test_opposite_transaction_partition_orders_complete_without_deadlock(self):
        first = self.transaction_request("forward")
        self.service.produce(
            "output", "p", 0, 0, [{"value": "reverse input"}], partition=0
        )
        member = self.service.join("output", "workers", "a")
        source = {**self.source(member), "topic": "output"}
        self.service.group_fetch(**source)
        second = dict(
            txid="reverse",
            source=source,
            expected_offset=0,
            next_offset=1,
            outputs=[{"topic": "input", "messages": [{"value": "reverse output"}]}],
        )
        with ThreadPoolExecutor(2) as pool:
            forward = pool.submit(self.service.process, **first)
            reverse = pool.submit(self.service.process, **second)
            self.assertEqual(forward.result(3)["next_offset"], 1)
            self.assertEqual(reverse.result(3)["next_offset"], 1)
        self.assertEqual(len(self.service.fetch("input", 0)["records"]), 2)
        self.assertEqual(len(self.service.fetch("output", 0)["records"]), 2)


if __name__ == "__main__":
    unittest.main()
