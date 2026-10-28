"""Client behavior with controlled transport; actual cluster tests stay separate."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from message_queue.client import Client, Producer


class RecordingClient:
    def __init__(self, count=3):
        self.count = count
        self.calls = []
        self.lock = threading.Lock()

    def call(self, path, **body):
        if path == "/metadata":
            return {"topics": [{"name": "events", "partitions": self.count}]}
        with self.lock:
            self.calls.append(json.loads(json.dumps(body)))
        return {
            "topic": body["topic"],
            "partition": body["partition"],
            "offsets": list(range(len(body["messages"]))),
            "committed": True,
            "replicas": ["a", "b"],
        }


class ClientTests(unittest.TestCase):
    def producer(self, client=None):
        return Producer(client or RecordingClient(), {"producer": "p", "epoch": 0})

    def test_send_many_routes_every_key_and_preserves_partition_order(self):
        client = RecordingClient()
        producer = self.producer(client)
        messages = [{"key": str(i), "value": str(i)} for i in range(25)]
        receipts = producer.send_many("events", messages, batch_size=4)
        observed = {}
        for call in client.calls:
            self.assertLessEqual(len(call["messages"]), 4)
            for record in call["messages"]:
                expected = zlib.crc32(record["key"].encode()) % 3
                self.assertEqual(call["partition"], expected)
                observed.setdefault(expected, []).append(record["value"])
        for partition, values in observed.items():
            self.assertEqual(
                values,
                [
                    m["value"]
                    for m in messages
                    if zlib.crc32(m["key"].encode()) % 3 == partition
                ],
            )
        self.assertEqual(
            sorted(i for r in receipts for i in r["input_indexes"]), list(range(25))
        )

    def test_send_rejects_implicit_batch_spanning_partitions(self):
        client = RecordingClient()
        with self.assertRaises(ValueError):
            self.producer(client).send(
                "events", [{"key": str(i), "value": "x"} for i in range(10)]
            )
        self.assertEqual(client.calls, [])

    def test_keyless_routing_is_random_and_refreshes_partition_count(self):
        client = RecordingClient()
        producer = self.producer(client)
        with patch(
            "message_queue.producer.secrets.randbelow", side_effect=[1, 4]
        ) as random:
            self.assertEqual(producer.send("events", [{"value": "x"}])["partition"], 1)
            client.count = 5
            self.assertEqual(producer.send("events", [{"value": "y"}])["partition"], 4)
        self.assertEqual([call.args[0] for call in random.call_args_list], [3, 5])

    def test_retry_owns_payload_even_if_caller_changes_it(self):
        client = RecordingClient()
        original = [{"value": "original", "tags": ["safe"]}]
        call = client.call
        attempts = []

        def flaky(path, **body):
            if path == "/produce":
                attempts.append(json.loads(json.dumps(body)))
                if len(attempts) == 1:
                    original[0]["value"] = "changed"
                    original[0]["tags"].append("new")
                    raise OSError("lost response")
            return call(path, **body)

        client.call = flaky
        with patch("message_queue.producer.time.sleep"):
            self.producer(client).send("events", original, partition=0)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(attempts[1]["messages"][0]["tags"], ["safe"])

    def test_uncertain_sequence_blocks_new_payload_and_can_be_confirmed(self):
        client = RecordingClient()
        producer = self.producer(client)
        with patch.object(client, "call", side_effect=OSError("uncertain")):
            with self.assertRaises(OSError):
                producer.send("events", [{"value": "old"}], partition=0, retries=0)
        with self.assertRaises(ValueError):
            producer.send("events", [{"value": "new"}], partition=0)
        producer.retry_pending("events", 0)
        producer.send("events", [{"value": "new"}], partition=0)
        self.assertEqual([c["sequence"] for c in client.calls], [0, 1])

    def test_keyless_retry_reuses_unconfirmed_partition(self):
        client = RecordingClient()
        producer = self.producer(client)
        call = client.call
        failed = [False]

        def flaky(path, **body):
            if path == "/produce" and not failed[0]:
                failed[0] = True
                raise OSError("uncertain")
            return call(path, **body)

        client.call = flaky
        with patch("message_queue.producer.secrets.randbelow", side_effect=[1, 2]):
            with self.assertRaises(OSError):
                producer.send("events", [{"value": "old"}], retries=0)
            self.assertEqual(
                producer.send("events", [{"value": "old"}])["partition"], 1
            )

    def test_same_partition_concurrency_allocates_unique_sequences(self):
        client = RecordingClient()
        producer = self.producer(client)
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(
                executor.map(
                    lambda i: producer.send("events", [{"value": str(i)}], partition=0),
                    range(30),
                )
            )
        self.assertEqual([c["sequence"] for c in client.calls], list(range(30)))

    def test_other_partition_progresses_during_blocked_request(self):
        client = RecordingClient()
        producer = self.producer(client)
        # Find a second partition on another fixed stripe.
        second = next(
            p
            for p in range(1, 64)
            if producer._lock(("events", p)) is not producer._lock(("events", 0))
        )
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        call = client.call

        def blocked(path, **body):
            if body.get("partition") == 0:
                entered.set()
                release.wait(3)
            return call(path, **body)

        client.call = blocked
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(producer.send, "events", [{"value": "slow"}], 0)
            try:
                self.assertTrue(entered.wait(1))
                other = executor.submit(
                    lambda: (
                        producer.send("events", [{"value": "fast"}], second),
                        done.set(),
                    )
                )
                self.assertTrue(done.wait(1))
                other.result()
            finally:
                release.set()
            first.result()

    def test_buffer_batches_by_size_and_close_drains(self):
        client = RecordingClient()
        with self.producer(client).buffered(
            "events", partition=0, batch_size=4, linger_seconds=5
        ) as buffer:
            futures = [buffer.submit({"value": str(i)}) for i in range(4)]
            for future in futures:
                self.assertTrue(future.result(2)["committed"])
            last = buffer.submit({"value": "tail"})
        self.assertTrue(last.done())
        self.assertEqual([len(c["messages"]) for c in client.calls], [4, 1])
        with self.assertRaises(RuntimeError):
            buffer.submit({"value": "late"})

    def test_buffer_flush_cancel_and_input_ownership(self):
        client = RecordingClient()
        with self.producer(client).buffered(
            "events", partition=0, linger_seconds=5
        ) as buffer:
            original = {"value": "safe", "tags": ["one"]}
            good = buffer.submit(original)
            cancelled = buffer.submit({"value": "cancel"})
            self.assertTrue(cancelled.cancel())
            original["tags"].append("two")
            buffer.flush()
            self.assertTrue(good.done())
        self.assertEqual(
            client.calls[0]["messages"], [{"value": "safe", "tags": ["one"]}]
        )

    def test_buffer_capacity_includes_inflight_and_failures_are_visible(self):
        client = RecordingClient()
        entered, release = threading.Event(), threading.Event()

        def fail(path, **body):
            entered.set()
            release.wait(3)
            raise OSError("controlled failure")

        client.call = fail
        buffer = self.producer(client).buffered(
            "events", partition=0, max_pending=1, linger_seconds=0, retries=0
        )
        try:
            future = buffer.submit({"value": "x"})
            self.assertTrue(entered.wait(1))
            with self.assertRaises(BufferError):
                buffer.submit({"value": "overflow"})
            release.set()
            with self.assertRaises(OSError):
                future.result(2)
        finally:
            release.set()
            buffer.close()

    def test_buffer_partial_failure_keeps_successful_partition_future(self):
        client = RecordingClient(count=2)
        keys = {zlib.crc32(str(i).encode()) % 2: str(i) for i in range(8)}
        call = client.call

        def flaky(path, **body):
            if body.get("partition") == 1:
                raise OSError("second group failed")
            return call(path, **body)

        client.call = flaky
        with self.producer(client).buffered(
            "events", batch_size=2, linger_seconds=5, retries=0
        ) as buffer:
            first = buffer.submit({"key": keys[0], "value": "safe"})
            second = buffer.submit({"key": keys[1], "value": "uncertain"})
            self.assertTrue(first.result(2)["committed"])
            with self.assertRaises(OSError):
                second.result(2)

    def test_async_capacity_input_capture_and_close_drains(self):
        entered, release = threading.Event(), threading.Event()
        seen = []

        def call(url, path, body, token, **kwargs):
            entered.set()
            release.wait(3)
            seen.append(body)
            return {}

        client = Client("http://unused", "private-token", async_capacity=1)
        original = {"messages": [{"value": "safe"}]}
        with patch("message_queue.client.rpc", side_effect=call):
            future = client.fire_and_forget(original)
            try:
                self.assertTrue(entered.wait(1))
                original["messages"][0]["value"] = "changed"
                with self.assertRaises(BufferError):
                    client.fire_and_forget({})
            finally:
                release.set()
                client.close()
            self.assertIsNone(future.result())
        self.assertEqual(seen[0]["messages"][0]["value"], "safe")
        with self.assertRaises(RuntimeError):
            client.call("/health")

    def test_async_failure_is_not_swallowed(self):
        with (
            Client("http://unused", "private-token") as client,
            patch("message_queue.client.rpc", side_effect=OSError("fail")),
        ):
            future = client.fire_and_forget({})
            with self.assertRaises(OSError):
                future.result(2)

    def test_async_callback_cannot_close_its_own_executor(self):
        entered, release, callback_done = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        def rpc_call(*args, **kwargs):
            entered.set()
            release.wait(3)
            return {}

        client = Client("http://unused", "private-token")

        def callback(_future):
            try:
                with self.assertRaises(RuntimeError):
                    client.close()
                self.assertFalse(client._closed)
            finally:
                callback_done.set()

        with patch("message_queue.client.rpc", side_effect=rpc_call):
            future = client.fire_and_forget({})
            try:
                self.assertTrue(entered.wait(1))
                future.add_done_callback(callback)
                release.set()
                self.assertTrue(callback_done.wait(2))
            finally:
                release.set()
                client.close()

    def test_archive_uses_captured_high_water_and_exclusive_path(self):
        client = Client("http://unused", "private-token")
        calls = []

        def call(path, **body):
            calls.append(path)
            if path == "/metadata":
                return {
                    "partitions": [
                        {"topic": "events", "partition": 0, "floor": 0, "hwm": 1}
                    ]
                }
            self.assertEqual(body["limit"], 2)
            return {
                "records": [{"offset": 0, "value": "a"}, {"offset": 1, "value": "b"}],
                "next_offset": 2,
            }

        client.call = call
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "archive.json"
            self.assertEqual(client.archive("events", 0, path), {"records": 2})
            self.assertEqual(len(json.loads(path.read_text())["records"]), 2)
            before = calls.count("/fetch")
            with self.assertRaises(FileExistsError):
                client.archive("events", 0, path)
            self.assertEqual(calls.count("/fetch"), before)

    def test_incomplete_archive_cleans_new_file(self):
        client = Client("http://unused", "private-token")
        client.call = lambda path, **body: (
            {"partitions": [{"topic": "events", "partition": 0, "floor": 0, "hwm": 0}]}
            if path == "/metadata"
            else {"records": [], "next_offset": 0}
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "archive.json"
            with self.assertRaises(ValueError):
                client.archive("events", 0, path)
            self.assertFalse(path.exists())

    def test_replay_validates_entire_archive_before_sending(self):
        producer = self.producer()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "archive.json"
            path.write_text(
                json.dumps({"records": [{"value": "safe"}, {"value": 123}]})
            )
            with self.assertRaises(ValueError):
                Client("http://unused", "private-token").replay(
                    path, producer, "events"
                )
        self.assertEqual(producer.client.calls, [])

    def test_invalid_configuration_and_message_fields(self):
        producer = self.producer()
        for message in (
            {"value": "x", "unknown": 1},
            {"value": "x", "tags": ["a", "a"]},
            {"value": "x", "delay_seconds": True},
            {"value": "x", "deliver_at": 1, "delay_seconds": 0},
        ):
            with self.assertRaises(ValueError):
                producer.send("events", [message], partition=0)
        for options in (
            {"linger_seconds": float("nan")},
            {"batch_size": 0},
            {"max_pending": True},
        ):
            with self.assertRaises(ValueError):
                producer.buffered("events", **options)


if __name__ == "__main__":
    unittest.main()
