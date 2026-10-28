from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from message_queue.broker import Broker
from message_queue.common import Error
from message_queue.coordinator import Coordinator


class StateTests(unittest.TestCase):
    """In-process transport substitution for deterministic clock/failure injection only."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = [2000000000.0]
        self.brokers = {f"b{i}": Broker(self.root / f"b{i}") for i in range(3)}
        self.failed = set()
        self.coordinator = self.open()

    def open(self):
        coordinator = Coordinator(
            self.root / "coordinator",
            {node: "http://unused" for node in self.brokers},
            "private-token",
            lease_seconds=2,
            clock=lambda: self.now[0],
        )

        def call(node, path, body):
            if node in self.failed:
                raise OSError("injected transport failure")
            return self.brokers[node].dispatch(path, body)

        coordinator.call = call
        return coordinator

    def tearDown(self):
        self.temp.cleanup()

    def topic(self, topic="input", **options):
        self.coordinator.create(topic, **options)

    def produce(self, topic="input", sequence=0, messages=None, **extra):
        with self.coordinator.transaction() as db:
            exists = db.execute("SELECT 1 FROM producers WHERE id='p'").fetchone()
        if not exists:
            self.coordinator.producer_open("p")
        return self.coordinator.produce(
            topic, "p", 0, sequence, messages or [{"value": "x"}], partition=0, **extra
        )

    def source(self, member):
        return {
            "topic": "input",
            "group": "workers",
            "member": member["member"],
            "session": member["session"],
            "generation": member["generation"],
            "partition": 0,
        }

    def test_expired_members_rebalance_and_fence_old_commits(self):
        self.topic(partitions=2)
        self.produce()
        first = self.coordinator.join("input", "workers", "a")
        second = self.coordinator.join("input", "workers", "b")
        self.now[0] += 1.5
        refreshed = self.coordinator.heartbeat(
            "input", "workers", "a", first["session"]
        )
        source = self.source(refreshed)
        self.coordinator.group_fetch(**source)
        self.now[0] += 1
        latest = self.coordinator.heartbeat("input", "workers", "a", first["session"])
        self.assertEqual(latest["assignments"], [0, 1])
        with self.assertRaises(Error):
            self.coordinator.commit(**source, next_offset=1)
        with self.assertRaises(Error):
            self.coordinator.heartbeat("input", "workers", "b", second["session"])

    def test_delayed_head_preserves_order(self):
        self.topic()
        self.produce(
            messages=[{"value": "later", "delay_seconds": 10}, {"value": "behind"}]
        )
        self.assertEqual(self.coordinator.fetch("input", 0)["records"], [])
        self.now[0] += 10
        self.assertEqual(
            [r["value"] for r in self.coordinator.fetch("input", 0)["records"]],
            ["later", "behind"],
        )

    def test_retention_floor_receipt_expiry_and_new_append_chain(self):
        self.topic(retention_seconds=10, segment_bytes=512)
        self.produce(messages=[{"value": "x" * 100} for _ in range(8)])
        self.now[0] += 11
        self.coordinator.retain("input")
        with self.assertRaises(Error) as caught:
            self.coordinator.fetch("input", 0, 0)
        self.assertEqual(caught.exception.status, 410)
        with self.assertRaises(Error) as caught:
            self.produce()
        self.assertEqual(caught.exception.status, 410)
        self.assertEqual(self.produce(sequence=1)["offsets"], [8])
        self.assertEqual(self.coordinator.fetch("input", 0, 8)["next_offset"], 9)

    def test_failed_transaction_is_invisible_and_retry_is_atomic(self):
        self.topic()
        self.topic("output")
        self.produce()
        member = self.coordinator.join("input", "workers", "a")
        source = self.source(member)
        self.coordinator.group_fetch(**source)
        body = {
            "txid": "one",
            "source": source,
            "expected_offset": 0,
            "next_offset": 1,
            "outputs": [{"topic": "output", "messages": [{"value": "done"}]}],
        }
        self.failed.update(["b1", "b2"])
        with self.assertRaises(Error):
            self.coordinator.process(**body)
        self.assertEqual(self.coordinator.fetch("output", 0)["records"], [])
        self.assertEqual(len(self.coordinator.group_fetch(**source)["records"]), 1)
        self.failed.clear()
        first = self.coordinator.process(**body)
        self.assertEqual(self.coordinator.process(**body), first)
        self.assertEqual(len(self.coordinator.fetch("output", 0)["records"]), 1)
        self.coordinator = self.open()
        self.assertEqual(self.coordinator.process(**body), first)

    def test_provisional_pending_journal_survives_coordinator_restart(self):
        self.topic()
        self.failed.update(["b1", "b2"])
        provisional = self.produce(acks="leader")
        self.assertFalse(provisional["committed"])
        self.coordinator = self.open()
        self.failed.clear()
        self.coordinator.maintenance()
        result = self.coordinator.fetch("input", 0)
        self.assertEqual(len(result["records"]), 1)
        self.assertTrue(self.produce()["committed"])


if __name__ == "__main__":
    unittest.main()
