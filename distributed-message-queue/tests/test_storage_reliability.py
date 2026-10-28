"""Behavior regressions for durable partitions and independent broker I/O."""

from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from message_queue.broker import Broker
from message_queue.common import Error, ZERO, envelope
from message_queue.log import (
    Corruption,
    HEADER,
    MAX_FRAME_BYTES,
    PartitionLog,
    atomic_json,
)


def records(count, start=0, previous=ZERO, text="message"):
    values = []
    for offset in range(start, start + count):
        value = envelope(
            {"offset": offset, "timestamp": float(offset), "value": text}, previous
        )
        values.append(value)
        previous = value["hash"]
    return values


class StorageReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "partition"
        self.log = PartitionLog(self.root, segment_bytes=1024 * 1024)

    def test_invalid_batch_tail_leaves_disk_and_memory_unchanged(self):
        original = records(2)
        self.log.append(original)
        batch = records(2, 2, original[-1]["hash"])
        batch[-1]["hash"] = ZERO
        with self.assertRaises(ValueError):
            self.log.append(batch)
        self.assertEqual(self.log.read(0), original)
        self.assertEqual(PartitionLog(self.root).read(0), original)

    def test_oversized_frame_rejected_before_any_append(self):
        batch = records(2)
        batch[-1] = envelope(
            {"offset": 1, "timestamp": 1.0, "value": "x" * MAX_FRAME_BYTES},
            batch[0]["hash"],
        )
        with self.assertRaises(Error) as raised:
            self.log.append(batch)
        self.assertEqual(raised.exception.status, 413)
        self.assertEqual(self.log.next, 0)
        self.assertEqual(list(self.log.directory.glob("*.log")), [])

    def test_invalid_scalar_types_are_rejected_before_any_append(self):
        for field, value in (
            ("offset", False),
            ("timestamp", True),
            ("timestamp", float("nan")),
        ):
            with self.subTest(field=field, value=value):
                record = {"offset": 0, "timestamp": 0.0, "value": "x"}
                record[field] = value
                try:
                    item = envelope(record, ZERO)
                except ValueError:
                    item = {"record": record, "previous": ZERO, "hash": ZERO}
                with self.assertRaises(ValueError):
                    self.log.append([item])
        self.assertEqual(self.log.next, 0)

    def test_batch_is_owned_before_disk_callbacks(self):
        values = records(5)
        expected = copy.deepcopy(values)
        original_fsync = os.fsync

        def mutate_input(descriptor):
            if values:
                values[0]["record"]["value"] = "changed by caller"
                values.clear()
            original_fsync(descriptor)

        with patch("message_queue.log.os.fsync", side_effect=mutate_input):
            self.log.append(values)
        self.assertEqual(self.log.read(0), expected)
        self.assertEqual(self.log.next, 5)

    def test_batch_flushes_once_per_touched_data_and_index_file(self):
        # This verifies the durability/performance contract: 100 records in one
        # segment require two fsyncs, not 200 per-message flushes.
        with patch("message_queue.log.os.fsync", wraps=os.fsync) as flush:
            with patch("message_queue.log._sync_directory"):
                self.log.append(records(100))
        self.assertEqual(flush.call_count, 2)
        self.assertEqual(PartitionLog(self.root).read(0), records(100))

    def test_append_io_failure_requires_reopening_and_recovers_written_prefix(self):
        with patch("message_queue.log.os.fsync", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                self.log.append(records(3))
        for action in (
            lambda: self.log.append(records(1)),
            self.log.scan,
            lambda: self.log.read(0),
        ):
            with self.assertRaises(Corruption):
                action()
        recovered = PartitionLog(self.root)
        self.assertEqual(recovered.read(0), records(3))
        recovered.append(records(1, 3, records(3)[-1]["hash"]))
        self.assertEqual(recovered.next, 4)

    def test_malformed_json_with_valid_crc_is_storage_corruption(self):
        self.log.append(records(1))
        path = next(self.log.directory.glob("*.log"))
        payload = b"{not json}"
        path.write_bytes(
            HEADER.pack(b"MQ01", len(payload), zlib.crc32(payload)) + payload
        )
        with self.assertRaises(Corruption):
            self.log.read(0)
        with self.assertRaises(Corruption):
            PartitionLog(self.root)

    def test_oversized_stored_header_is_rejected_before_reading_payload(self):
        self.log.append(records(1))
        path = next(self.log.directory.glob("*.log"))
        path.write_bytes(HEADER.pack(b"MQ01", 2**31, 0))
        with self.assertRaises(Corruption):
            self.log.read(0)

    def test_manifest_scalar_validation_is_corruption(self):
        manifest_path = self.log.directory / "manifest.json"
        original = self.log.manifest
        for field, value in (
            ("base", True),
            ("epoch", -1),
            ("base_hash", "invalid"),
            ("committed", 4),
        ):
            with self.subTest(field=field):
                atomic_json(manifest_path, {**original, field: value})
                with self.assertRaises(Corruption):
                    PartitionLog(self.root)
        atomic_json(manifest_path, original)

    def test_generation_pointer_rejects_path_escape(self):
        (self.root / "current.json").write_text(
            json.dumps({"generation": "generation-../../escape"}), encoding="utf-8"
        )
        with self.assertRaises(Corruption):
            PartitionLog(self.root)

    def test_committed_checkpoint_cannot_roll_back_after_restart(self):
        values = records(4)
        self.log.append(values)
        self.log.align(2, 3, values[3]["hash"])
        reopened = PartitionLog(self.root)
        for epoch in (2, 3):
            with self.assertRaises(Error) as raised:
                reopened.align(epoch, 1, values[1]["hash"])
            self.assertEqual(raised.exception.status, 409)
        self.assertEqual(reopened.read(0), values)

    def test_new_epoch_persists_even_if_suffix_deletion_fails(self):
        small = PartitionLog(Path(self.temporary.name) / "small", segment_bytes=256)
        values = records(5, text="x" * 80)
        small.append(values)
        with patch.object(Path, "unlink", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                small.align(5, 1, values[1]["hash"])
        reopened = PartitionLog(small.root, 256)
        self.assertEqual(reopened.manifest["epoch"], 5)
        with self.assertRaises(Error):
            reopened.align(4, 1, values[1]["hash"])
        reopened.align(5, 1, values[1]["hash"])
        self.assertEqual(reopened.read(0), values[:2])

    def test_malformed_sync_does_not_truncate_provisional_suffix(self):
        values = records(4)
        self.log.append(values)
        invalid = records(1, 2, values[1]["hash"])
        invalid[0]["hash"] = ZERO
        with self.assertRaises(ValueError):
            self.log.synchronize(2, 1, values[1]["hash"], 0, ZERO, invalid)
        self.assertEqual(self.log.read(0), values)
        with self.assertRaises(Error):
            self.log.synchronize(2, 1, values[1]["hash"], 1, ZERO, [])
        self.assertEqual(self.log.read(0), values)

    def test_install_validation_precedes_generation_creation(self):
        original = records(2)
        self.log.append(original)
        generation = self.log.directory
        invalid = records(3)
        invalid[-1]["previous"] = ZERO
        with self.assertRaises((Error, ValueError)):
            self.log.install(invalid, 0, ZERO, 4)
        self.assertEqual(self.log.directory, generation)
        self.assertEqual(len(list(self.root.glob("generation-*"))), 1)
        self.assertEqual(self.log.read(0), original)

    def test_install_rejects_missing_or_wrong_committed_checkpoint(self):
        values = records(3)
        for committed, digest in ((3, values[-1]["hash"]), (2, ZERO)):
            with self.subTest(committed=committed):
                with self.assertRaises(Error):
                    self.log.install(values, 0, ZERO, 1, committed, digest)
        self.assertEqual(self.log.next, 0)

    def test_install_cannot_change_or_roll_back_observed_committed_history(self):
        values = records(3)
        self.log.append(values)
        self.log.align(2, 2, values[2]["hash"])
        with self.assertRaises(Error):
            self.log.install(values[:2], 0, ZERO, 3, 1, values[1]["hash"])
        changed = records(3, text="different history")
        with self.assertRaises(Error):
            self.log.install(changed, 0, ZERO, 3, 2, changed[2]["hash"])
        self.assertEqual(self.log.read(0), values)

    def test_cleanup_failure_does_not_undo_successful_snapshot_publication(self):
        values = records(3)
        with patch.object(
            self.log, "_remove_generation", side_effect=OSError("cleanup")
        ):
            self.log.install(values, 0, ZERO, 1, 2, values[2]["hash"])
        self.assertEqual(self.log.read(0), values)
        self.assertEqual(PartitionLog(self.root).read(0), values)
        self.assertEqual(len(list(self.root.glob("generation-*"))), 1)

    def test_pointer_publish_error_recovers_selected_generation_on_reopen(self):
        original_write = atomic_json

        def write_then_fail(path, value):
            original_write(path, value)
            if path.name == "current.json":
                raise OSError("directory sync failure after replacement")

        values = records(3)
        with patch("message_queue.log.atomic_json", side_effect=write_then_fail):
            with self.assertRaises(OSError):
                self.log.install(values, 0, ZERO, 1, 2, values[2]["hash"])
        with self.assertRaises(Corruption):
            self.log.read(0)
        self.assertEqual(PartitionLog(self.root).read(0), values)

    def test_retention_is_idempotent_and_advances_checkpoint(self):
        values = records(4)
        self.log.append(values)
        self.log.retain(3, values[2]["hash"])
        self.log.retain(3, values[2]["hash"])
        reopened = PartitionLog(self.root)
        self.assertEqual(reopened.manifest["committed"], 2)
        self.assertEqual(reopened.read(3), values[3:])
        with self.assertRaises(Error):
            reopened.align(2, 1, values[1]["hash"])

    def test_manifest_results_are_owned_copies(self):
        view = self.log.manifest
        view["epoch"] = 100
        self.assertEqual(self.log.manifest["epoch"], 0)

    def test_concurrent_same_partition_sync_retries_preserve_one_batch(self):
        broker = Broker(Path(self.temporary.name) / "broker")
        values = records(3)
        body = {
            "topic": "events",
            "partition": 0,
            "epoch": 1,
            "committed": -1,
            "committed_hash": ZERO,
            "base": 0,
            "base_hash": ZERO,
            "records": values,
        }
        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(
                workers.map(lambda _: broker.dispatch("/sync", body), range(16))
            )
        self.assertTrue(all(value["next"] == 3 for value in results))
        self.assertEqual(
            broker.dispatch("/read", {"topic": "events", "partition": 0, "offset": 0})[
                "records"
            ],
            values,
        )

    def test_slow_cold_partition_does_not_block_existing_other_partition(self):
        broker = Broker(Path(self.temporary.name) / "broker")
        broker.dispatch("/read", {"topic": "events", "partition": 1, "offset": 0})
        entered, release = threading.Event(), threading.Event()
        original = PartitionLog.__init__

        def delayed(log, root, segment_bytes):
            if Path(root).name == "0":
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test failed to release partition")
            original(log, root, segment_bytes)

        with patch.object(PartitionLog, "__init__", delayed):
            with ThreadPoolExecutor(max_workers=2) as workers:
                slow = workers.submit(
                    broker.dispatch,
                    "/read",
                    {"topic": "events", "partition": 0, "offset": 0},
                )
                self.assertTrue(entered.wait(2))
                try:
                    other = workers.submit(
                        broker.dispatch,
                        "/read",
                        {"topic": "events", "partition": 1, "offset": 0},
                    )
                    self.assertEqual(other.result(timeout=1), {"records": []})
                finally:
                    release.set()
                self.assertEqual(slow.result(timeout=2), {"records": []})

    def test_unknown_endpoint_does_not_create_partition_files(self):
        broker = Broker(Path(self.temporary.name) / "broker")
        with self.assertRaises(Error) as raised:
            broker.dispatch("/unknown", {"topic": "events", "partition": 0})
        self.assertEqual(raised.exception.status, 404)
        self.assertEqual(list(broker.root.iterdir()), [])

    def test_atomic_json_failure_removes_only_its_temporary_file(self):
        target = self.root / "metadata.json"
        target.write_text("original", encoding="utf-8")
        unrelated = self.root / "metadata.json.tmp-unrelated"
        unrelated.write_text("keep", encoding="utf-8")
        with patch(
            "message_queue.log.os.replace", side_effect=OSError("replace failed")
        ):
            with self.assertRaises(OSError):
                atomic_json(target, {"changed": True})
        self.assertEqual(target.read_text(encoding="utf-8"), "original")
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")
        self.assertEqual(list(self.root.glob("metadata.json.tmp-*")), [unrelated])


if __name__ == "__main__":
    unittest.main()
