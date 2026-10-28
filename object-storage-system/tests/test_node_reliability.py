from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_core import ROOT  # noqa: F401 -- initialize the source import path
from object_storage.common import CHUNK, Error, sha
from object_storage.node import DataNode, FOOTER, HEADER
from object_storage.node_lock import NodeLock


class NodeReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.node = DataNode(self.root / "data", clock=lambda: 1000)

    def test_duplicate_concurrent_uploads_have_one_record(self):
        data = b"concurrent"
        with ThreadPoolExecutor(max_workers=8) as pool:
            receipts = list(
                pool.map(lambda _: self.node.put(sha(data), data), range(32))
            )
        self.assertEqual(sum(not value["duplicate"] for value in receipts), 1)
        self.assertEqual(
            self.node.pack.stat().st_size, HEADER.size + len(data) + FOOTER.size
        )
        self.assertEqual(DataNode(self.node.root).get(sha(data)), data)

    def test_large_reads_can_progress_concurrently(self):
        data = b"x" * 70000
        self.node.put(sha(data), data)
        barrier = threading.Barrier(2)
        original = Path.open

        def opening(path, mode="r", *args, **kwargs):
            if path.suffix == ".dat" and mode == "rb":
                barrier.wait(timeout=2)
            return original(path, mode, *args, **kwargs)

        with patch.object(Path, "open", opening):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: self.node.get(sha(data)), range(2)))
        self.assertEqual(results, [data, data])

    def test_compaction_and_concurrent_get_preserve_every_chunk(self):
        values = [f"item-{i}".encode() for i in range(12)]
        for value in values:
            self.node.put(sha(value), value)
        with ThreadPoolExecutor(max_workers=5) as pool:
            reads = [
                pool.submit(lambda: [self.node.get(sha(v)) for v in values])
                for _ in range(4)
            ]
            compact = pool.submit(self.node.compact)
            for result in reads:
                self.assertEqual(result.result(timeout=5), values)
            self.assertEqual(compact.result(timeout=5)["packed_chunks"], len(values))
        self.assertEqual(DataNode(self.node.root).status()["chunks"], len(values))

    def test_partial_append_rolls_back_before_later_acknowledgement(self):
        first, failed, last = b"first", b"failed", b"last"
        self.node.put(sha(first), first)
        old_size = self.node.pack.stat().st_size

        def broken_record(handle, *args):
            handle.write(b"S3P1partial")
            raise OSError("injected disk write failure")

        with patch.object(self.node, "record", side_effect=broken_record):
            with self.assertRaises(OSError):
                self.node.put(sha(failed), failed)
        self.assertEqual(self.node.pack.stat().st_size, old_size)
        self.node.put(sha(last), last)
        recovered = DataNode(self.node.root)
        self.assertEqual(recovered.get(sha(first)), first)
        self.assertEqual(recovered.get(sha(last)), last)
        with self.assertRaises(Error):
            recovered.get(sha(failed))

    def test_fsync_failure_rolls_back_unacknowledged_record(self):
        data = b"sync-failed"
        original = os.fsync
        attempts = 0

        def syncing(descriptor):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("injected fsync failure")
            return original(descriptor)

        with patch("object_storage.node.os.fsync", side_effect=syncing):
            with self.assertRaises(OSError):
                self.node.put(sha(data), data)
        self.assertFalse(self.node.status()["needs_recovery"])
        self.node.put(sha(b"later"), b"later")
        self.assertEqual(DataNode(self.node.root).get(sha(b"later")), b"later")

    def test_failed_rollback_blocks_serving_until_recovery(self):
        data = b"durable"
        self.node.put(sha(data), data)
        with patch(
            "object_storage.node.os.fsync", side_effect=OSError("disk unavailable")
        ):
            with self.assertRaises(OSError):
                self.node.put(sha(b"other"), b"other")
        self.assertTrue(self.node.status()["needs_recovery"])
        for operation in (
            lambda: self.node.get(sha(data)),
            lambda: self.node.put(sha(b"new"), b"new"),
            lambda: self.node.collect([], 0),
            self.node.compact,
        ):
            with self.assertRaises(Error) as caught:
                operation()
            self.assertEqual(caught.exception.status, 503)
        self.node.recover()
        self.assertEqual(self.node.get(sha(data)), data)

    def test_index_commit_failure_requires_recovery_but_preserves_bytes(self):
        data = b"committed-pack-only"
        original = self.node.transaction

        @contextmanager
        def transaction(write=False):
            if write:
                raise sqlite3.OperationalError("injected index failure")
            with original(write) as db:
                yield db

        with patch.object(self.node, "transaction", transaction):
            with self.assertRaises(sqlite3.OperationalError):
                self.node.put(sha(data), data)
        with self.assertRaises(Error) as caught:
            self.node.get(sha(data))
        self.assertEqual(caught.exception.status, 503)
        self.node.recover()
        self.assertEqual(self.node.get(sha(data)), data)

    def test_tombstone_index_failure_cannot_acknowledge_stale_bytes(self):
        data = b"collect-me"
        self.node.put(sha(data), data)
        original = self.node.transaction

        @contextmanager
        def transaction(write=False):
            if write:
                raise sqlite3.OperationalError("injected delete-index failure")
            with original(write) as db:
                yield db

        with patch.object(self.node, "transaction", transaction):
            with self.assertRaises(sqlite3.OperationalError):
                self.node.collect([], 0)
        with self.assertRaises(Error) as caught:
            self.node.put(sha(data), data)
        self.assertEqual(caught.exception.status, 503)
        self.node.recover()
        with self.assertRaises(Error) as caught:
            self.node.get(sha(data))
        self.assertEqual(caught.exception.status, 404)

    def test_failed_compaction_rebuild_never_uses_old_offsets(self):
        values = (b"z-last", b"a-first")
        for data in values:
            self.node.put(sha(data), data)
        old = self.node.pack
        with patch.object(
            self.node, "recover", side_effect=OSError("injected rebuild failure")
        ):
            with self.assertRaises(OSError):
                self.node.compact()
        self.assertNotEqual(old, self.node.pack)
        with self.assertRaises(Error) as caught:
            self.node.get(sha(values[0]))
        self.assertEqual(caught.exception.status, 503)
        recovered = DataNode(self.node.root)
        for data in values:
            self.assertEqual(recovered.get(sha(data)), data)

    def test_failed_current_publish_preserves_previous_generation(self):
        data = b"current-safe"
        self.node.put(sha(data), data)
        old = self.node.pack
        original = os.replace

        def replacing(source, destination):
            if Path(destination).name == "CURRENT":
                raise OSError("injected CURRENT replace failure")
            return original(source, destination)

        with patch("object_storage.node.os.replace", side_effect=replacing):
            with self.assertRaises(OSError):
                self.node.compact()
        self.assertEqual(self.node.pack, old)
        self.assertEqual(self.node.get(sha(data)), data)
        self.assertEqual(list(self.node.root.glob("current-*.tmp")), [])
        self.assertEqual(DataNode(self.node.root).get(sha(data)), data)

    def test_interrupt_after_current_publish_blocks_old_generation_writes(self):
        data = b"published"
        self.node.put(sha(data), data)
        old = self.node.pack
        switch = self.node.switch

        def interrupted(name):
            switch(name)
            raise KeyboardInterrupt("after CURRENT publication")

        with patch.object(self.node, "switch", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.node.compact()
        self.assertNotEqual(self.node.pack, old)
        with self.assertRaises(Error) as caught:
            self.node.put(sha(b"later"), b"later")
        self.assertEqual(caught.exception.status, 503)
        self.node.recover()
        self.node.put(sha(b"later"), b"later")
        self.assertEqual(DataNode(self.node.root).get(sha(b"later")), b"later")

    def test_large_file_corruption_is_bounded_and_repairable(self):
        data = b"L" * 70000
        self.node.put(sha(data), data)
        with (self.node.large / (sha(data) + ".dat")).open("r+b") as handle:
            handle.truncate(CHUNK + 100)
        with self.assertRaises(Error):
            self.node.get(sha(data))
        self.assertEqual(DataNode(self.node.root).status()["chunks"], 0)
        self.node.put(sha(data), data)
        self.assertEqual(DataNode(self.node.root).get(sha(data)), data)

    def test_invalid_chunk_inputs_cannot_mutate_pack(self):
        for value in ("text", bytearray(b"x"), None, b"x" * (CHUNK + 1)):
            with self.assertRaises(Error):
                self.node.put(sha(b"x"), value)
        self.assertEqual(self.node.status()["chunks"], 0)
        self.assertEqual(self.node.pack.stat().st_size, 0)

    def test_gc_requires_bounded_valid_references_and_grace(self):
        data = b"protected"
        self.node.put(sha(data), data)
        for live, grace in (
            (sha(data), 0),
            ([], True),
            ([], -1),
            ([], float("nan")),
            (["bad-hash"], 0),
            ([sha(data)] * 50001, 0),
        ):
            with self.assertRaises(ValueError):
                self.node.collect(live, grace)
        self.assertEqual(self.node.get(sha(data)), data)
        self.assertEqual(self.node.collect([sha(data)], 0)["removed"], 0)

    def test_gc_grace_boundary_is_preserved_across_compaction(self):
        data = b"young"
        self.node.put(sha(data), data)
        self.node.clock = lambda: 1059
        self.node.compact()
        self.assertEqual(self.node.collect([], 60)["removed"], 0)
        self.node.clock = lambda: 1060
        self.assertEqual(self.node.collect([], 60)["removed"], 1)
        with self.assertRaises(Error):
            DataNode(self.node.root).get(sha(data))

    def test_invalid_clock_fails_before_writing(self):
        for clock in (lambda: True, lambda: float("nan"), lambda: -1):
            self.node.clock = clock
            with self.assertRaises(ValueError):
                self.node.put(sha(b"bad-time"), b"bad-time")
        self.assertEqual(self.node.pack.stat().st_size, 0)

    def test_queued_collector_cannot_remove_republished_duplicate(self):
        data = b"new-reference-to-old-chunk"
        self.node.put(sha(data), data)
        generation = self.node.status()["gc_generation"]
        started = threading.Event()

        def delayed_collect():
            started.set()
            return self.node.collect([], 0, generation=generation)

        with ThreadPoolExecutor(max_workers=1) as pool:
            # Simulate a request already accepted by HTTP but still waiting for
            # node ownership when the coordinator's RPC deadline expires.
            with self.node.lock:
                future = pool.submit(delayed_collect)
                self.assertTrue(started.wait(timeout=1))
                self.assertFalse(future.done())
                receipt = self.node.put(sha(data), data)
                self.assertTrue(receipt["duplicate"])
                self.assertNotEqual(self.node.status()["gc_generation"], generation)
            with self.assertRaises(Error) as caught:
                future.result(timeout=2)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.node.get(sha(data)), data)

    def test_matching_generation_collects_once_and_replay_is_rejected(self):
        keep, drop = b"keep-generation", b"drop-generation"
        for data in (keep, drop):
            self.node.put(sha(data), data)
        generation = self.node.status()["gc_generation"]
        self.assertEqual(
            self.node.collect([sha(keep)], 0, generation=generation)["removed"], 1
        )
        with self.assertRaises(Error) as caught:
            self.node.collect([], 0, generation=generation)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.node.get(sha(keep)), keep)

    def test_restart_recovery_and_compaction_invalidate_gc_generations(self):
        data = b"retained"
        self.node.put(sha(data), data)
        generation = self.node.status()["gc_generation"]
        restarted = DataNode(self.node.root)
        self.assertNotEqual(restarted.status()["gc_generation"], generation)
        with self.assertRaises(Error):
            restarted.collect([], 0, generation=generation)
        generation = restarted.status()["gc_generation"]
        restarted.recover()
        self.assertNotEqual(restarted.status()["gc_generation"], generation)
        generation = restarted.status()["gc_generation"]
        restarted.compact()
        with self.assertRaises(Error):
            restarted.collect([], 0, generation=generation)
        self.assertEqual(restarted.get(sha(data)), data)

    def test_malformed_gc_generation_cannot_mutate_data(self):
        data = b"valid"
        self.node.put(sha(data), data)
        generation = self.node.status()["gc_generation"]
        for value in (True, 1, "", "bad", "g" * 32, "a" * 64):
            with self.assertRaises(ValueError):
                self.node.collect([], 0, generation=value)
        self.assertEqual(self.node.status()["gc_generation"], generation)
        self.assertEqual(self.node.get(sha(data)), data)

    def test_new_put_invalidates_old_collector_generation(self):
        generation = self.node.status()["gc_generation"]
        self.node.put(sha(b"new-data"), b"new-data")
        with self.assertRaises(Error) as caught:
            self.node.collect([], 0, generation=generation)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.node.get(sha(b"new-data")), b"new-data")


class NodeLockTests(unittest.TestCase):
    def test_writer_can_reenter_read_helper(self):
        lock = NodeLock()
        with lock, lock:
            with lock.read():
                pass

    def test_read_to_write_upgrade_is_explicitly_rejected(self):
        lock = NodeLock()
        with lock.read():
            with self.assertRaises(RuntimeError):
                with lock:
                    pass


if __name__ == "__main__":
    unittest.main()
