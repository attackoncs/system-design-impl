"""File/checkpoint behavior with a real raw log and controlled RPC faults."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ad_click.common import Error, dumps
from ad_click.log import EventLog
from ad_click.tailer import ingest_file


def click(event_id="one"):
    return {
        "event_id": event_id,
        "ad_id": "hot",
        "click_timestamp": 601,
        "user_id": "u",
        "ip": "127.0.0.1",
        "country": "US",
    }


class TailerReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "events.jsonl"
        self.checkpoint = self.root / "tail.json"
        self.log = EventLog(self.root / "raw.sqlite", 1, clock=lambda: 1000)
        self.config = {"ingress_url": "http://unused", "write_token": "private-token"}

    def rpc(self, url, token, body):
        return self.log.append(body["events"])

    def write(self, values):
        self.source.write_text(
            "".join(dumps(value) + "\n" for value in values), encoding="utf-8"
        )

    def test_partial_final_record_waits_and_complete_prefix_is_not_resent(self):
        self.source.write_text(
            dumps(click()) + "\n" + dumps(click("two")), encoding="utf-8"
        )
        with patch("ad_click.tailer.http_json", side_effect=self.rpc) as request:
            self.assertEqual(
                ingest_file(self.config, self.source, self.checkpoint)["records"], 1
            )
            self.assertEqual(
                ingest_file(self.config, self.source, self.checkpoint)["records"], 0
            )
            self.assertEqual(request.call_count, 1)
            with self.source.open("a", encoding="utf-8") as handle:
                handle.write("\n")
            self.assertEqual(
                ingest_file(self.config, self.source, self.checkpoint)["records"], 1
            )
        self.assertEqual(self.log.status()["raw_events"], 2)

    def test_lost_ack_does_not_checkpoint_and_replay_is_idempotent(self):
        self.write([click()])

        def lost_ack(*args):
            self.rpc(*args)
            raise OSError("controlled lost confirmation")

        with patch("ad_click.tailer.http_json", side_effect=lost_ack):
            with self.assertRaises(OSError):
                ingest_file(self.config, self.source, self.checkpoint)
        self.assertFalse(self.checkpoint.exists())
        with patch("ad_click.tailer.http_json", side_effect=self.rpc):
            self.assertEqual(
                ingest_file(self.config, self.source, self.checkpoint)["records"], 1
            )
        self.assertEqual(self.log.status()["raw_events"], 1)

    def test_incomplete_ack_does_not_advance_position(self):
        self.write([click()])
        with patch(
            "ad_click.tailer.http_json",
            return_value={"lineage": "line", "receipts": []},
        ):
            with self.assertRaises(Error):
                ingest_file(self.config, self.source, self.checkpoint)
        self.assertFalse(self.checkpoint.exists())

    def test_changed_ingress_lineage_preserves_existing_checkpoint(self):
        self.write([click()])
        with patch("ad_click.tailer.http_json", side_effect=self.rpc):
            ingest_file(self.config, self.source, self.checkpoint)
        original = self.checkpoint.read_bytes()
        with self.source.open("a", encoding="utf-8") as handle:
            handle.write(dumps(click("two")) + "\n")
        other = EventLog(self.root / "other.sqlite", 1, clock=lambda: 1000)
        with patch(
            "ad_click.tailer.http_json",
            side_effect=lambda url, token, body: other.append(body["events"]),
        ):
            with self.assertRaises(Error):
                ingest_file(self.config, self.source, self.checkpoint)
        self.assertEqual(self.checkpoint.read_bytes(), original)

    def test_only_one_tailer_may_own_a_checkpoint(self):
        self.write([click()])
        entered, release = threading.Event(), threading.Event()

        def waiting(*args):
            entered.set()
            release.wait(3)
            return self.rpc(*args)

        with (
            patch("ad_click.tailer.http_json", side_effect=waiting),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            first = executor.submit(
                ingest_file, self.config, self.source, self.checkpoint
            )
            try:
                self.assertTrue(entered.wait(1))
                with self.assertRaises(Error) as caught:
                    ingest_file(self.config, self.source, self.checkpoint)
                self.assertEqual(caught.exception.status, 409)
            finally:
                release.set()
            first.result(3)

    def test_source_cannot_be_checkpoint_or_its_lock_sidecar(self):
        self.write([click()])
        contents = self.source.read_bytes()
        with self.assertRaises(ValueError):
            ingest_file(self.config, self.source, self.source)
        lock_checkpoint = self.root / "tail.json"
        lock_source = self.root / "tail.json.lock"
        lock_source.write_bytes(contents)
        with self.assertRaises(ValueError):
            ingest_file(self.config, lock_source, lock_checkpoint)
        self.assertEqual(self.source.read_bytes(), contents)

    def test_hardlinked_source_and_checkpoint_are_rejected(self):
        self.write([click()])
        os.link(self.source, self.checkpoint)
        with self.assertRaises(ValueError):
            ingest_file(self.config, self.source, self.checkpoint)

    def test_bad_checkpoint_is_rejected_before_rpc(self):
        self.write([click()])
        for body in (
            {"offset": -1, "prefix": "0" * 64},
            {"offset": True, "prefix": "0" * 64},
            {"offset": 0, "prefix": "bad"},
        ):
            self.checkpoint.write_text(json.dumps(body), encoding="utf-8")
            with patch("ad_click.tailer.http_json") as request:
                with self.assertRaises(ValueError):
                    ingest_file(self.config, self.source, self.checkpoint)
                request.assert_not_called()
        self.checkpoint.write_bytes(b" " * 4097)
        with self.assertRaises(ValueError):
            ingest_file(self.config, self.source, self.checkpoint)

    def test_jsonl_duplicate_fields_are_not_silently_accepted(self):
        self.source.write_text('{"event_id":"a","event_id":"b"}\n', encoding="utf-8")
        with patch("ad_click.tailer.http_json") as request:
            with self.assertRaises(ValueError):
                ingest_file(self.config, self.source, self.checkpoint)
            request.assert_not_called()

    def test_atomic_checkpoint_failure_preserves_old_state_and_cleans_temp(self):
        self.write([click()])
        with patch("ad_click.tailer.http_json", side_effect=self.rpc):
            ingest_file(self.config, self.source, self.checkpoint)
        old = self.checkpoint.read_bytes()
        with self.source.open("a", encoding="utf-8") as handle:
            handle.write(dumps(click("two")) + "\n")
        with (
            patch("ad_click.tailer.http_json", side_effect=self.rpc),
            patch(
                "ad_click.checkpoints.os.replace",
                side_effect=OSError("controlled replace failure"),
            ),
        ):
            with self.assertRaises(OSError):
                ingest_file(self.config, self.source, self.checkpoint)
        self.assertEqual(self.checkpoint.read_bytes(), old)
        self.assertEqual(list(self.root.glob("tail.json.tmp-*")), [])
        with patch("ad_click.tailer.http_json", side_effect=self.rpc):
            ingest_file(self.config, self.source, self.checkpoint)
        self.assertEqual(self.log.status()["raw_events"], 2)

    def test_batch_budget_is_resumable(self):
        self.write([click(str(index)) for index in range(201)])
        with patch("ad_click.tailer.http_json", side_effect=self.rpc):
            first = ingest_file(
                self.config, self.source, self.checkpoint, max_batches=1
            )
            self.assertEqual(first["records"], 200)
            self.assertTrue(first["limit_reached"])
            last = ingest_file(self.config, self.source, self.checkpoint, max_batches=2)
            self.assertEqual(last["records"], 1)
            self.assertFalse(last["limit_reached"])
        self.assertEqual(self.log.status()["raw_events"], 201)

    def test_prefix_mutation_and_overlong_records_fail_without_rpc(self):
        self.write([click()])
        with patch("ad_click.tailer.http_json", side_effect=self.rpc):
            ingest_file(self.config, self.source, self.checkpoint)
        self.source.write_text("changed\n", encoding="utf-8")
        with patch("ad_click.tailer.http_json") as request:
            with self.assertRaises(Error):
                ingest_file(self.config, self.source, self.checkpoint)
            request.assert_not_called()
        self.checkpoint.unlink()
        self.source.write_bytes(b"x" * 8193)
        with self.assertRaises(ValueError):
            ingest_file(self.config, self.source, self.checkpoint)


if __name__ == "__main__":
    unittest.main()
