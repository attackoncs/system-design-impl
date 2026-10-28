"""Coordinator boundaries: concurrency, immutable replicas and upload races."""

from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_core import ROOT  # noqa: F401 -- allow standalone test discovery
from object_storage.common import Error, sha
from object_storage.service import Catalog, Service
from object_storage.metadata import Metadata
from object_storage.node import DataNode


CONFIG = {
    "internal_token": "t",
    "continuation_secret": "s",
    "data_urls": ["http://a", "http://b", "http://c"],
    "metadata_urls": ["http://meta"],
    "write_quorum": 2,
}


class ServiceReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.catalog = Catalog(Path(self.temp.name) / "catalog.sqlite")
        self.service = Service(CONFIG, self.catalog)
        self.addCleanup(self.service.close)
        self.bucket = self.catalog.create("bucket-test", "alice")["id"]

    def test_readers_and_publications_overlap_but_gc_waits(self):
        entered = threading.Barrier(3)
        release = threading.Event()
        collecting = threading.Event()

        def request(method):
            with (
                self.service.operation(deadline=time.monotonic() + 3),
                self.service.guard(method, "/v1/buckets/bucket-test/object"),
            ):
                entered.wait(timeout=2)
                release.wait(timeout=2)

        def collect():
            with (
                self.service.operation(deadline=time.monotonic() + 3),
                self.service.guard("POST", "/admin/maintenance"),
            ):
                collecting.set()

        with ThreadPoolExecutor(max_workers=3) as pool:
            first, second = pool.submit(request, "GET"), pool.submit(request, "PUT")
            entered.wait(timeout=2)
            gc = pool.submit(collect)
            self.assertFalse(collecting.wait(0.05))
            release.set()
            first.result(timeout=2)
            second.result(timeout=2)
            gc.result(timeout=2)
        self.assertTrue(collecting.is_set())

    def test_listing_snapshot_and_bucket_delete_exclude_writes(self):
        for method, path in (
            ("GET", "/v1/buckets/bucket-test/objects"),
            ("DELETE", "/v1/buckets/bucket-test"),
        ):
            with (
                self.service.operation(deadline=time.monotonic() + 1),
                self.service.guard(method, path),
            ):
                with ThreadPoolExecutor(max_workers=1) as pool:

                    def blocked():
                        with (
                            self.service.operation(deadline=time.monotonic() + 0.05),
                            self.service.guard("PUT", "/v1/buckets/bucket-test/object"),
                        ):
                            self.fail("write overlapped exclusive operation")

                    with self.assertRaises(Error):
                        pool.submit(blocked).result(timeout=1)

    def test_cancelled_operation_does_not_issue_rpc(self):
        stop = threading.Event()
        stop.set()
        with patch("object_storage.service.request") as request:
            with (
                self.assertRaises(Error),
                self.service.operation(deadline=time.monotonic() + 1, cancel=stop),
            ):
                self.service.read_chunk("0" * 64)
            request.assert_not_called()

    def test_replication_receipts_are_exact_and_no_quorum_no_publish(self):
        with (
            patch(
                "object_storage.service.request",
                return_value={"hash": sha(b"x"), "size": True},
            ),
            patch.object(self.service, "publish") as publish,
        ):
            with self.assertRaises(Error) as caught:
                self.service.put(self.bucket, "key", "req", b"x", sha(b"x"))
            self.assertEqual(caught.exception.status, 503)
            publish.assert_not_called()

    def test_replication_captures_one_request_deadline(self):
        calls = []

        def rpc(*args, **kwargs):
            calls.append(kwargs)
            return {"hash": sha(b"x"), "size": 1}

        deadline = time.monotonic() + 2
        with (
            patch("object_storage.service.request", side_effect=rpc),
            self.service.operation(deadline=deadline),
        ):
            self.assertEqual(self.service.write_chunk(b"x")["size"], 1)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call["deadline"] == deadline for call in calls))

    def test_replication_error_drains_accepted_calls(self):
        entered = threading.Event()
        release = threading.Event()

        def rpc(*args, **kwargs):
            entered.set()
            release.wait(timeout=2)
            return {"hash": sha(b"x"), "size": 1}

        def write():
            with self.service.operation(deadline=time.monotonic() + 0.05):
                self.service.write_chunk(b"x")

        with (
            patch("object_storage.service.request", side_effect=rpc),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            future = pool.submit(write)
            self.assertTrue(entered.wait(1))
            self.assertFalse(future.done())
            time.sleep(0.08)
            self.assertFalse(future.done())
            release.set()
            with self.assertRaises(Error):
                future.result(timeout=1)

    def test_read_falls_back_from_corrupt_chunk(self):
        with patch("object_storage.service.request", side_effect=[b"corrupt", b"good"]):
            self.assertEqual(self.service.read_chunk(sha(b"good")), b"good")

    def test_publication_receipts_reject_invalid_version_or_duplicate_flag(self):
        for result in (
            {},
            {"version": "bad\r\n", "duplicate": False},
            {"version": "version", "duplicate": 1},
        ):
            with patch.object(self.service, "meta", return_value=result):
                with self.assertRaises(Error) as caught:
                    self.service.publish(self.bucket, "key", "req", [], 0, sha(b""))
                self.assertEqual(caught.exception.status, 502)

    def test_metadata_identity_manifest_and_header_id_are_checked(self):
        good = {
            "id": "version",
            "bucket": self.bucket,
            "key": "key",
            "manifest": [],
            "size": 0,
            "sha": sha(b""),
            "marker": 0,
        }
        for field, bad in (
            ("id", "bad\r\nHeader"),
            ("bucket", "other"),
            ("key", "other"),
            ("size", True),
            ("manifest", [{"hash": "0" * 64, "size": 1}]),
        ):
            with (
                self.subTest(field=field),
                patch.object(self.service, "meta", return_value={**good, field: bad}),
            ):
                with self.assertRaises(Error) as caught:
                    self.service.get(self.bucket, "key")
                self.assertEqual(caught.exception.status, 502)
        with patch.object(self.service, "meta", return_value=good):
            self.assertEqual(self.service.get(self.bucket, "key")[1], b"")

    def test_abort_waits_for_frozen_completion_and_never_undoes_it(self):
        data = b"part"
        upload = self.catalog.start(
            self.bucket,
            "alice",
            {
                "key": "key",
                "request_key": "req",
                "sha256": sha(data),
                "size": len(data),
            },
        )["id"]
        manifest = [{"hash": sha(data), "size": len(data)}]
        self.catalog.add_part(upload, "alice", 1, data, manifest)
        entered, release = threading.Event(), threading.Event()

        def publish(*args):
            entered.set()
            release.wait(timeout=2)
            return {"id": "version"}

        with (
            patch.object(self.service, "read_chunk", return_value=data),
            patch.object(self.service, "publish", side_effect=publish),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            complete = pool.submit(self.service.complete, upload, "alice")
            self.assertTrue(entered.wait(1))
            abort = pool.submit(self.service.abort, upload, "alice")
            self.assertFalse(abort.done())
            release.set()
            self.assertEqual(complete.result(timeout=2)["id"], "version")
            with self.assertRaises(Error):
                abort.result(timeout=2)
        self.assertEqual(self.catalog.upload(upload, "alice")["state"], "complete")

    def test_missing_or_malformed_metadata_stops_gc(self):
        for value in ({}, {"hashes": ["bad"]}, {"hashes": "0" * 64}):
            with patch("object_storage.service.request", return_value=value) as request:
                with self.assertRaises(Error):
                    self.service.maintenance(0)
                self.assertFalse(
                    any("/collect" in call.args[0] for call in request.call_args_list)
                )

    def test_close_prevents_new_operation(self):
        self.service.close()
        with (
            self.assertRaises(Error),
            self.service.operation(deadline=time.monotonic() + 1),
        ):
            pass

    def test_nonfinite_operation_deadlines_are_rejected_before_admission(self):
        for deadline in (float("nan"), float("inf"), True, "later"):
            with (
                self.assertRaises(ValueError),
                self.service.operation(deadline=deadline),
            ):
                pass

    def test_lost_metadata_ack_is_replayed_before_gc_and_survives_restart(self):
        metadata = Metadata(Path(self.temp.name) / "metadata.sqlite")
        nodes = {
            url: DataNode(Path(self.temp.name) / str(index))
            for index, url in enumerate(CONFIG["data_urls"])
        }
        lose_ack = [True]

        def rpc(url, token, body=None, method=None, raw=False, **kwargs):
            if url.endswith("/publish"):
                result = metadata.publish(**body)
                if lose_ack[0]:
                    lose_ack[0] = False
                    raise OSError("committed metadata acknowledgement lost")
                return result
            if url.endswith("/references"):
                return metadata.references(**body)
            if url.endswith("/get"):
                return metadata.get(**body)
            base, path = url.split("/", 3)[2:]
            node = nodes["http://" + base]
            if path == "status":
                return node.status()
            if path == "collect":
                return node.collect(**body)
            if path.startswith("chunks/"):
                key = path.split("/")[1]
                return node.put(key, body) if method == "PUT" else node.get(key)
            self.fail("unexpected route " + url)

        with patch("object_storage.service.request", side_effect=rpc):
            with self.assertRaises(OSError):
                self.service.put(self.bucket, "key", "request", b"data", sha(b"data"))
            self.assertEqual(len(self.catalog.pending_publications()), 1)
            self.service.close()
            self.service = Service(CONFIG, Catalog(self.catalog.path))
            self.addCleanup(self.service.close)
            result = self.service.maintenance(0)
            self.assertEqual(result["resumed_publications"], 1)
            self.assertEqual(self.catalog.pending_publications(), [])
            self.assertEqual(self.service.get(self.bucket, "key")[1], b"data")

    def test_failed_publication_recovery_never_starts_collection(self):
        self.catalog.prepare_publication(
            self.bucket, "key", "req", [], 0, sha(b""), False
        )
        with patch(
            "object_storage.service.request",
            side_effect=OSError("metadata unavailable"),
        ) as request:
            with self.assertRaises(OSError):
                self.service.maintenance(0)
            self.assertFalse(
                any("/collect" in call.args[0] for call in request.call_args_list)
            )
            self.assertEqual(len(self.catalog.pending_publications()), 1)
