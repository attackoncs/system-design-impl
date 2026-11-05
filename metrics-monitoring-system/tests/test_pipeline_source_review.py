"""Real TSDB/rejection transactions with a controlled queue page and commit sink."""

from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from metrics_monitor.common import dumps
from metrics_monitor.pipeline import consume_once
from metrics_monitor.storage import Store


class QueuePage:
    def __init__(self, payloads):
        self.payloads = payloads
        self.commits = []

    def fetch(self):
        return {"partition": 0}, {
            "records": [{"value": payload} for payload in self.payloads],
            "next_offset": len(self.payloads),
        }

    def call(self, path, **body):
        self.commits.append((path, body))


class PipelineSourceReviewTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.now = 2_000_000_000
        self.samples = [{"metric": "cpu", "timestamp": self.now, "value": 5}]

    def rejected_before_healthy(self, payload, name):
        store = Store(self.root / (name + ".sqlite"), clock=lambda: self.now)
        healthy = dumps({"batch_id": "healthy", "samples": self.samples})
        queue = QueuePage([payload, healthy])
        self.assertTrue(consume_once(store, queue))
        self.assertEqual(store.health()["rejected_batches"], 1)
        self.assertEqual(store.health()["raw_samples"], 1)
        with store.transaction() as db:
            self.assertEqual(
                [row[0] for row in db.execute("SELECT id FROM batches")], ["healthy"]
            )
        self.assertEqual(
            queue.commits, [("/commit", {"partition": 0, "next_offset": 2})]
        )
        # Replayed poison and the healthy batch must remain idempotent too.
        self.assertTrue(consume_once(store, queue))
        self.assertEqual(store.health()["rejected_batches"], 1)
        self.assertEqual(store.health()["raw_samples"], 1)

    def test_deep_json_is_rejected_without_blocking_later_samples(self):
        payload = '{"batch_id":"deep","samples":' + "[" * 4000 + "0" + "]" * 4000 + "}"
        self.assertLess(len(payload.encode()), 12000)
        self.rejected_before_healthy(payload, "deep")

    def test_duplicate_fields_are_not_silently_ingested(self):
        samples = dumps(self.samples)
        payloads = [
            '{"batch_id":"first","batch_id":"last","samples":' + samples + "}",
            '{"batch_id":"labels","samples":[{"metric":"cpu","timestamp":'
            + str(self.now)
            + ',"value":5,"labels":{"zone":"west","zone":"east"}}]}',
        ]
        for index, payload in enumerate(payloads):
            with self.subTest(index=index):
                self.rejected_before_healthy(payload, "duplicate" + str(index))

    def test_nonfinite_envelope_metadata_is_rejected(self):
        for constant in ("NaN", "Infinity", "1e400"):
            with self.subTest(constant=constant):
                payload = (
                    '{"batch_id":"invalid","partition":'
                    + constant
                    + ',"samples":'
                    + dumps(self.samples)
                    + "}"
                )
                self.rejected_before_healthy(payload, "nonfinite" + constant)

    def test_rejection_storage_failure_does_not_commit_queue_position(self):
        store = Store(self.root / "unavailable.sqlite", clock=lambda: self.now)
        queue = QueuePage(["not JSON"])
        with patch.object(
            store, "reject", side_effect=sqlite3.OperationalError("busy")
        ):
            with self.assertRaises(sqlite3.OperationalError):
                consume_once(store, queue)
        self.assertEqual(queue.commits, [])
        self.assertEqual(store.health()["rejected_batches"], 0)


if __name__ == "__main__":
    unittest.main()
