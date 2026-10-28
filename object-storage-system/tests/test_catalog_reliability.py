from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from object_storage.catalog import Catalog
from object_storage.common import Error, sha


class CatalogReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1000
        self.catalog = Catalog(
            Path(self.temp.name) / "catalog.sqlite", clock=lambda: self.now
        )
        self.bucket = self.catalog.create("bucket-test", "alice")["id"]

    def start(self, key="key", request="upload", size=4):
        return self.catalog.start(
            self.bucket,
            "alice",
            {"key": key, "request_key": request, "sha256": sha(b"data"), "size": size},
        )

    def part(self, upload, data=b"data"):
        return self.catalog.add_part(
            upload, "alice", 1, data, [{"hash": sha(data), "size": len(data)}]
        )

    def test_bucket_listing_is_owned_sorted_and_empty_for_other_user(self):
        self.catalog.create("aaa-first", "alice")
        self.catalog.create("other-user", "bob")
        self.assertEqual(
            [row["name"] for row in self.catalog.listing("alice")["buckets"]],
            ["aaa-first", "bucket-test"],
        )
        self.assertEqual(self.catalog.listing("outsider"), {"buckets": []})
        with self.assertRaises(Error):
            self.catalog.bucket("bucket-test", "bob")

    def test_create_is_idempotent_but_cannot_take_another_users_name(self):
        self.assertEqual(self.catalog.create("bucket-test", "alice")["id"], self.bucket)
        with self.assertRaises(Error) as caught:
            self.catalog.create("bucket-test", "bob")
        self.assertEqual(caught.exception.status, 409)

    def test_owner_bucket_budget_is_checked_inside_transaction(self):
        with patch("object_storage.catalog.MAX_BUCKETS", 1):
            with self.assertRaises(Error) as caught:
                self.catalog.create("one-too-many", "alice")
            self.assertEqual(caught.exception.status, 422)
            self.assertEqual(len(self.catalog.listing("alice")["buckets"]), 1)

    def test_invalid_bucket_and_identity_are_explicit_validation_failures(self):
        for invalid in ("A", "ab", "a/b", [], True):
            with self.assertRaises(ValueError):
                self.catalog.create(invalid, "alice")
        for invalid in ("", "../user", [], True):
            with self.assertRaises(ValueError):
                self.catalog.listing(invalid)

    def test_upload_start_verifies_catalog_bucket_owner(self):
        body = {
            "key": "key",
            "request_key": "upload",
            "sha256": sha(b"data"),
            "size": 4,
        }
        for bucket, user in ((self.bucket, "bob"), ("missing", "alice")):
            with self.assertRaises(Error) as caught:
                self.catalog.start(bucket, user, body)
            self.assertEqual(caught.exception.status, 404)

    def test_concurrent_start_receipt_returns_same_upload(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(lambda _: self.start(), range(16)))
        self.assertEqual(len({row["id"] for row in rows}), 1)
        self.assertTrue(all(row["parts"] == [] for row in rows))

    def test_duplicate_start_retains_parts_and_has_same_response_shape(self):
        row = self.start()
        self.part(row["id"])
        duplicate = self.start()
        self.assertEqual(set(row), set(duplicate))
        self.assertEqual(len(duplicate["parts"]), 1)
        self.assertEqual(duplicate["id"], row["id"])

    def test_add_part_is_owned_and_immutable(self):
        row = self.start()
        self.part(row["id"])
        self.assertTrue(self.part(row["id"])["duplicate"])
        with self.assertRaises(Error) as conflict:
            self.part(row["id"], b"evil")
        self.assertEqual(conflict.exception.status, 409)
        with self.assertRaises(Error):
            self.catalog.add_part(
                row["id"], "bob", 1, b"data", [{"hash": sha(b"data"), "size": 4}]
            )

    def test_part_cannot_exceed_declared_total(self):
        row = self.start(size=3)
        with self.assertRaises(Error) as caught:
            self.part(row["id"])
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.catalog.upload(row["id"], "alice")["parts"], [])

    def test_part_validates_data_and_manifest_before_recording(self):
        row = self.start()
        for data, manifest in (
            (b"", []),
            (bytearray(b"data"), []),
            (b"data", [{"hash": sha(b"other"), "size": 4}]),
            (b"data", []),
        ):
            with self.assertRaises((ValueError, Error)):
                self.catalog.add_part(row["id"], "alice", 1, data, manifest)
        self.assertEqual(self.catalog.upload(row["id"], "alice")["parts"], [])

    def test_part_rechecks_state_and_clock_after_waiting_for_writer(self):
        for change in ("aborted", "completing", "expired"):
            with self.subTest(change=change):
                row = self.start(key=change)
                entered, release = threading.Event(), threading.Event()
                original = self.catalog.transaction

                @contextmanager
                def blocked(write=False):
                    if write:
                        entered.set()
                        if not release.wait(3):
                            raise AssertionError("writer not released")
                    with original(write) as db:
                        yield db

                self.catalog.transaction = blocked
                try:
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        pending = pool.submit(self.part, row["id"])
                        self.assertTrue(entered.wait(3))
                        with original(True) as db:
                            if change == "expired":
                                db.execute(
                                    "UPDATE uploads SET expires=0 WHERE id=?",
                                    (row["id"],),
                                )
                            else:
                                db.execute(
                                    "UPDATE uploads SET state=? WHERE id=?",
                                    (change, row["id"]),
                                )
                        release.set()
                        with self.assertRaises(Error) as caught:
                            pending.result(timeout=3)
                        self.assertEqual(caught.exception.status, 409)
                finally:
                    release.set()
                    self.catalog.transaction = original
                self.assertEqual(self.catalog.upload(row["id"], "alice")["parts"], [])

    def test_start_freezes_caller_fields_before_waiting_for_writer(self):
        body = {
            "key": "key",
            "request_key": "upload",
            "sha256": sha(b"data"),
            "size": 4,
        }
        entered, release = threading.Event(), threading.Event()
        original = self.catalog.transaction

        @contextmanager
        def blocked(write=False):
            if write:
                entered.set()
                if not release.wait(3):
                    raise AssertionError("writer not released")
            with original(write) as db:
                yield db

        self.catalog.transaction = blocked
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(self.catalog.start, self.bucket, "alice", body)
                self.assertTrue(entered.wait(3))
                body.update(key="changed", size=8, sha256=sha(b"changed!"))
                self.now = 2000
                release.set()
                row = pending.result(timeout=3)
        finally:
            release.set()
            self.catalog.transaction = original
        self.assertEqual((row["key"], row["size"], row["expires"]), ("key", 4, 5600))


if __name__ == "__main__":
    unittest.main()
