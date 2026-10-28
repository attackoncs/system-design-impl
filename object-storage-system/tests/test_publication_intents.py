from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from object_storage.catalog import Catalog
from object_storage.common import Error, sha


class PublicationIntentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "catalog.sqlite"
        self.now = 1000
        self.catalog = Catalog(self.path, clock=lambda: self.now)

    @staticmethod
    def payload(request="request", key="key", data=b"data", bucket="bucket"):
        return {
            "bucket": bucket,
            "key": key,
            "request": request,
            "manifest": [{"hash": sha(data), "size": len(data)}] if data else [],
            "size": len(data),
            "sha": sha(data),
            "marker": False,
        }

    def test_prepare_is_durable_and_pending_contains_complete_replay_payload(self):
        payload = self.payload()
        self.assertEqual(
            self.catalog.prepare_publication(**payload),
            {"done": False, "duplicate": False},
        )
        self.catalog = Catalog(self.path)
        self.assertEqual(self.catalog.pending_publications(), [payload])
        self.assertEqual(
            self.catalog.prepare_publication(**payload),
            {"done": False, "duplicate": True},
        )

    def test_finish_is_idempotent_and_done_retry_does_not_reopen(self):
        payload = self.payload()
        self.catalog.prepare_publication(**payload)
        for _ in range(2):
            self.assertEqual(
                self.catalog.finish_publication("bucket", "key", "request"),
                {"done": True},
            )
        self.assertEqual(self.catalog.pending_publications(), [])
        self.catalog = Catalog(self.path)
        self.assertEqual(
            self.catalog.prepare_publication(**payload),
            {"done": True, "duplicate": True},
        )
        self.assertEqual(self.catalog.pending_publications(), [])

    def test_changed_same_request_conflicts_even_after_done(self):
        self.catalog.prepare_publication(**self.payload())
        for finish in (False, True):
            if finish:
                self.catalog.finish_publication("bucket", "key", "request")
            with self.assertRaises(Error) as caught:
                self.catalog.prepare_publication(**self.payload(data=b"evil"))
            self.assertEqual(caught.exception.status, 409)

    def test_pending_and_done_are_scoped_by_full_object_request_identity(self):
        for payload in (
            self.payload(),
            self.payload(key="another"),
            self.payload(bucket="other"),
            self.payload(request="another"),
        ):
            self.catalog.prepare_publication(**payload)
        self.catalog.finish_publication("bucket", "key", "request")
        pending = self.catalog.pending_publications()
        self.assertEqual(len(pending), 3)
        self.assertNotIn(self.payload(), pending)

    def test_concurrent_same_prepare_has_one_original_intent(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda _: self.catalog.prepare_publication(**self.payload()),
                    range(16),
                )
            )
        self.assertEqual(sum(not result["duplicate"] for result in results), 1)
        self.assertEqual(len(self.catalog.pending_publications()), 1)

    def test_returned_pending_mutation_cannot_change_durable_payload(self):
        self.catalog.prepare_publication(**self.payload())
        rows = self.catalog.pending_publications()
        rows[0]["manifest"][0]["hash"] = sha(b"evil")
        rows[0]["size"] = 999
        self.assertEqual(self.catalog.pending_publications(), [self.payload()])

    def test_prepare_freezes_manifest_before_waiting_for_writer(self):
        payload = self.payload()
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
                pending = pool.submit(self.catalog.prepare_publication, **payload)
                self.assertTrue(entered.wait(3))
                payload["manifest"][0]["hash"] = sha(b"evil")
                self.now = 2000
                release.set()
                pending.result(timeout=3)
        finally:
            release.set()
            self.catalog.transaction = original
        self.assertEqual(self.catalog.pending_publications(), [self.payload()])
        with self.catalog.transaction() as db:
            self.assertEqual(
                db.execute("SELECT created FROM publications").fetchone()[0], 2000
            )

    def test_pending_budget_never_returns_an_incomplete_recovery_set(self):
        for index in range(3):
            self.catalog.prepare_publication(**self.payload(request=str(index)))
        with self.assertRaises(Error) as caught:
            self.catalog.pending_publications(limit=2)
        self.assertEqual(caught.exception.status, 422)
        self.assertEqual(len(self.catalog.pending_publications(limit=3)), 3)
        for invalid in (True, 0, 129, "128"):
            with self.assertRaises(ValueError):
                self.catalog.pending_publications(invalid)

    def test_corrupt_payload_or_checksum_blocks_recovery_and_collection(self):
        for corrupt in (
            "not-json",
            "null",
            json.dumps({**self.payload(), "bucket": "changed"}),
            json.dumps(self.payload(data=b"evil")),
        ):
            with self.subTest(corrupt=corrupt):
                self.catalog.prepare_publication(**self.payload())
                with self.catalog.transaction(True) as db:
                    db.execute("UPDATE publications SET payload=?", (corrupt,))
                with self.assertRaises(Error) as caught:
                    self.catalog.pending_publications()
                self.assertEqual(caught.exception.status, 409)
                with self.catalog.transaction(True) as db:
                    db.execute("DELETE FROM publications")

    def test_unknown_finish_does_not_create_a_done_receipt(self):
        with self.assertRaises(Error) as caught:
            self.catalog.finish_publication("bucket", "key", "missing")
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(self.catalog.pending_publications(), [])

    def test_marker_intent_has_no_data_and_is_replayable(self):
        payload = {**self.payload(data=b""), "marker": True}
        self.catalog.prepare_publication(**payload)
        self.assertEqual(self.catalog.pending_publications(), [payload])

    def test_invalid_manifest_does_not_create_intent(self):
        for changed in (
            {"manifest": []},
            {"marker": 1},
            {"size": True},
            {"manifest": [{"hash": "bad", "size": 4}]},
        ):
            with self.assertRaises(ValueError):
                self.catalog.prepare_publication(**{**self.payload(), **changed})
        self.assertEqual(self.catalog.pending_publications(), [])

    def test_legacy_catalog_receives_publication_table_without_data_loss(self):
        legacy = Path(self.temp.name) / "legacy.sqlite"
        db = sqlite3.connect(legacy)
        try:
            db.executescript(
                "CREATE TABLE buckets(name TEXT PRIMARY KEY,id TEXT UNIQUE,user TEXT);"
            )
            db.execute(
                "INSERT INTO buckets VALUES('bucket-test','legacy-bucket','alice')"
            )
            db.commit()
        finally:
            db.close()
        catalog = Catalog(legacy)
        self.assertEqual(catalog.listing("alice")["buckets"][0]["id"], "legacy-bucket")
        self.assertEqual(catalog.pending_publications(), [])
        catalog.prepare_publication(**self.payload())
        self.assertEqual(catalog.pending_publications(), [self.payload()])


if __name__ == "__main__":
    unittest.main()
