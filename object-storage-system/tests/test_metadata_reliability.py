from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from object_storage.common import Error, sha, shard_for
from object_storage.metadata import Metadata


class MetadataReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1000
        self.meta = Metadata(self.root / "metadata.sqlite", clock=lambda: self.now)

    def publish(self, request="first", key="key", data=b"data", bucket="bucket"):
        manifest = [{"hash": sha(data), "size": len(data)}] if data else []
        return self.meta.publish(bucket, key, request, manifest, len(data), sha(data))

    def test_concurrent_same_request_creates_one_durable_version(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.publish(), range(24)))
        self.assertEqual(len({result["version"] for result in results}), 1)
        self.assertEqual(sum(not result["duplicate"] for result in results), 1)
        self.assertEqual(len(self.meta.listing("bucket", versions=True)["rows"]), 1)
        reopened = Metadata(self.meta.path)
        self.assertEqual(reopened.get("bucket", "key")["id"], results[0]["version"])

    def test_input_is_frozen_before_waiting_for_writer(self):
        manifest = [{"hash": sha(b"data"), "size": 4}]
        entered, release = threading.Event(), threading.Event()
        original = self.meta.transaction

        @contextmanager
        def blocked(write=False):
            if write:
                entered.set()
                if not release.wait(3):
                    raise AssertionError("writer not released")
            with original(write) as db:
                yield db

        self.meta.transaction = blocked
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(
                    self.meta.publish,
                    "bucket",
                    "key",
                    "first",
                    manifest,
                    4,
                    sha(b"data"),
                )
                self.assertTrue(entered.wait(3))
                manifest[0]["hash"] = sha(b"evil")
                manifest.append({"hash": sha(b"extra"), "size": 5})
                release.set()
                pending.result(timeout=3)
        finally:
            release.set()
            self.meta.transaction = original
        row = self.meta.get("bucket", "key")
        self.assertEqual(row["manifest"], [{"hash": sha(b"data"), "size": 4}])
        self.assertTrue(self.publish()["duplicate"])

    def test_key_scope_and_bucket_scope_do_not_share_receipts(self):
        first = self.publish()
        other_key = self.publish(key="other")
        other_bucket = self.publish(bucket="another")
        self.assertEqual(
            len({r["version"] for r in (first, other_key, other_bucket)}), 3
        )
        self.assertEqual(len(self.meta.listing("bucket")["rows"]), 2)
        self.assertEqual(len(self.meta.listing("another")["rows"]), 1)

    def test_removed_version_receipt_does_not_resurrect_data(self):
        first = self.publish()
        self.meta.remove("bucket", "key", first["version"])
        duplicate = self.publish()
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["version"], first["version"])
        with self.assertRaises(Error) as caught:
            self.meta.get("bucket", "key", first["version"])
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(self.meta.listing("bucket")["rows"], [])

    def test_marker_hides_latest_without_destroying_historical_version(self):
        first = self.publish()
        marker = self.meta.publish("bucket", "key", "delete", [], 0, sha(b""), True)
        self.assertEqual(self.meta.get("bucket", "key")["marker"], 1)
        self.assertEqual(
            self.meta.get("bucket", "key", first["version"])["id"], first["version"]
        )
        self.meta.remove("bucket", "key", marker["version"])
        self.assertEqual(self.meta.get("bucket", "key")["id"], first["version"])

    def test_metadata_does_not_cross_shard_boundaries(self):
        meta = Metadata(self.root / "shard.sqlite", shard=0, shards=2)
        key = next(str(i) for i in range(100) if shard_for("bucket", str(i), 2) == 1)
        for action in (
            lambda: meta.get("bucket", key),
            lambda: meta.publish("bucket", key, "first", [], 0, sha(b"")),
            lambda: meta.remove("bucket", key, "version"),
        ):
            with self.assertRaises(Error) as caught:
                action()
            self.assertEqual(caught.exception.status, 409)

    def test_empty_version_is_not_latest_lookup(self):
        self.publish()
        for invalid in ("", False, [], "../version"):
            with self.assertRaises(ValueError):
                self.meta.get("bucket", "key", invalid)
            with self.assertRaises(ValueError):
                self.meta.remove("bucket", "key", invalid)

    def test_invalid_bucket_is_rejected_by_all_entry_points(self):
        for invalid in ("", [], "../bucket", True):
            for action in (
                lambda: self.meta.get(invalid, "key"),
                lambda: self.meta.listing(invalid),
                lambda: self.meta.prune(invalid, 1),
            ):
                with self.assertRaises(ValueError):
                    action()

    def test_versions_flag_is_strict_boolean(self):
        for invalid in (0, 1, "true", "false", [], None):
            with self.assertRaises(ValueError):
                self.meta.listing("bucket", invalid)

    def test_empty_content_requires_its_actual_checksum(self):
        for marker in (True, False):
            with self.assertRaises(ValueError):
                self.meta.publish(
                    "bucket", "key", "empty", [], 0, sha(b"other"), marker
                )
        self.meta.publish("bucket", "key", "empty", [], 0, sha(b""))

    def test_manifest_size_and_shape_inputs_are_strict(self):
        for manifest, size, marker in (
            ([{"hash": sha(b"data"), "size": True}], 1, False),
            ([{"hash": sha(b"data"), "size": 4, "extra": 1}], 4, False),
            ([{"hash": sha(b"data"), "size": 4}], 4, 1),
            ([{"hash": sha(b"data"), "size": 4}], 4, True),
            ([{"hash": sha(b"data"), "size": 262145}], 262145, False),
            ([{"hash": sha(b"data"), "size": 1}] * 257, 257, False),
        ):
            with self.assertRaises(ValueError):
                self.meta.publish(
                    "bucket", "key", "first", manifest, size, sha(b"data"), marker
                )
        self.assertEqual(self.meta.listing("bucket", True)["rows"], [])

    def test_invalid_clock_rolls_back_publication(self):
        for invalid in (float("nan"), float("inf"), -1, True):
            self.meta.clock = lambda: invalid
            with self.assertRaises(ValueError):
                self.publish()
        self.assertEqual(self.meta.listing("bucket", True)["rows"], [])

    def test_publication_samples_clock_after_write_lock(self):
        entered, release = threading.Event(), threading.Event()
        original = self.meta.transaction

        @contextmanager
        def blocked(write=False):
            if write:
                entered.set()
                if not release.wait(3):
                    raise AssertionError("writer not released")
            with original(write) as db:
                yield db

        self.meta.transaction = blocked
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(self.publish)
                self.assertTrue(entered.wait(3))
                self.now = 2000
                release.set()
                pending.result(timeout=3)
        finally:
            release.set()
            self.meta.transaction = original
        self.assertEqual(self.meta.get("bucket", "key")["created"], 2000)

    def test_remove_is_idempotent_and_keeps_first_deletion_time(self):
        first = self.publish()
        self.meta.remove("bucket", "key", first["version"])
        self.now += 100
        self.meta.remove("bucket", "key", first["version"])
        with self.meta.transaction() as db:
            self.assertEqual(
                db.execute("SELECT removed FROM versions").fetchone()[0], 1000
            )

    def test_grace_boundary_and_shared_references(self):
        one = self.publish()
        two = self.publish("second", "another")
        self.meta.remove("bucket", "key", one["version"])
        self.now += 60
        self.assertEqual(self.meta.references(60)["hashes"], [sha(b"data")])
        self.meta.remove("bucket", "another", two["version"])
        self.assertEqual(self.meta.references(0)["hashes"], [])
        self.assertEqual(self.meta.references(1)["hashes"], [sha(b"data")])

    def test_prune_applies_per_key_without_affecting_another_bucket(self):
        for key in ("a", "b"):
            for index in range(4):
                self.publish(str(index), key=key, data=str(index).encode())
        other = self.publish(bucket="other")
        self.now = 2000
        self.assertEqual(self.meta.prune("bucket", 2), {"pruned": 4})
        rows = self.meta.listing("bucket", True)["rows"]
        self.assertEqual([row["key"] for row in rows], ["a", "a", "b", "b"])
        self.assertEqual(self.meta.get("other", "key")["id"], other["version"])
        with self.meta.transaction() as db:
            removed = [
                row[0]
                for row in db.execute(
                    "SELECT removed FROM versions WHERE removed IS NOT NULL"
                )
            ]
        self.assertEqual(removed, [2000] * 4)
        self.assertEqual(self.meta.prune("bucket", 2), {"pruned": 0})

    def test_prune_samples_one_clock_value_for_all_removed_rows(self):
        for index in range(4):
            self.publish(str(index))
        calls = []

        def clock():
            calls.append(None)
            return 2000 + len(calls)

        self.meta.clock = clock
        self.meta.prune("bucket", 1)
        self.assertEqual(len(calls), 1)

    def test_invalid_prune_and_reference_limits_do_not_modify_rows(self):
        self.publish()
        for invalid in (True, 0, 1001, "1"):
            with self.assertRaises(ValueError):
                self.meta.prune("bucket", invalid)
        for invalid in (True, -1, 86401, "1"):
            with self.assertRaises(ValueError):
                self.meta.references(invalid)
        self.assertEqual(len(self.meta.listing("bucket", True)["rows"]), 1)

    def test_corrupt_persisted_manifest_blocks_collection(self):
        self.publish()
        for corrupt in ("not-json", json.dumps([{"hash": "bad", "size": 4}]), "null"):
            with self.meta.transaction(True) as db:
                db.execute("UPDATE versions SET manifest=?", (corrupt,))
            with self.assertRaises(Error) as caught:
                self.meta.references()
            self.assertEqual(caught.exception.status, 409)

    def test_reference_scan_budgets_fail_closed_even_for_duplicate_content(self):
        for index in range(3):
            self.publish(str(index))
        for name in (
            "MAX_REFERENCE_ROWS",
            "MAX_REFERENCE_CHUNKS",
            "MAX_REFERENCE_HASHES",
        ):
            with patch("object_storage.metadata." + name, 0):
                with self.assertRaises(Error) as caught:
                    self.meta.references()
                self.assertEqual(caught.exception.status, 422)


if __name__ == "__main__":
    unittest.main()
