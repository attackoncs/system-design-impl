import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_store  # noqa: F401 -- initialize this standalone test's source path
from distributed_email.common import Error
from distributed_email.objects import MAX_OBJECT_BYTES, Objects, ObjectStore


class ObjectReliabilityTests(unittest.TestCase):
    def test_concurrent_duplicate_content_is_one_verified_object(self):
        with tempfile.TemporaryDirectory() as root:
            store = ObjectStore(root)
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(store.put, [b"same"] * 32))
            self.assertTrue(all(item == results[0] for item in results))
            self.assertEqual(store.get(results[0]["key"]), b"same")
            self.assertEqual(len(list(Path(root).glob("*/*"))), 1)

    def test_oversized_corrupt_file_is_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            store = ObjectStore(root)
            key = store.put(b"small")["key"]
            with store.path(key).open("wb") as handle:
                handle.truncate(MAX_OBJECT_BYTES + 100)
            with self.assertRaises(Error) as caught:
                store.get(key)
            self.assertEqual(caught.exception.status, 409)

    def test_remote_put_checks_receipt_and_forwards_deadline(self):
        objects = Objects({"object_url": "http://127.0.0.1", "internal_token": "t"})
        receipt = {"key": hashlib.sha256(b"data").hexdigest(), "size": 4}
        with patch(
            "distributed_email.objects.http_json", return_value=receipt
        ) as request:
            self.assertEqual(objects.put(b"data", timeout=2, deadline=123), receipt)
            self.assertEqual(request.call_args.kwargs["deadline"], 123)
        for bad in ({}, {**receipt, "key": "0" * 64}, {**receipt, "size": True}):
            with patch("distributed_email.objects.http_json", return_value=bad):
                with self.assertRaises(Error):
                    objects.put(b"data")

    def test_remote_get_rejects_malformed_lengths_encoding_and_checksum(self):
        objects = Objects({"object_url": "http://127.0.0.1", "internal_token": "t"})
        key = hashlib.sha256(b"data").hexdigest()
        valid = {"data": base64.b64encode(b"data").decode(), "size": 4}
        with patch("distributed_email.objects.http_json", return_value=valid):
            self.assertEqual(objects.get(key), b"data")
        for bad in (
            {},
            {**valid, "size": 3},
            {**valid, "data": "!!"},
            {**valid, "data": "YQ==", "size": 1},
            {**valid, "size": True},
        ):
            with patch("distributed_email.objects.http_json", return_value=bad):
                with self.assertRaises(Error):
                    objects.get(key)
        with patch("distributed_email.objects.http_json") as request:
            with self.assertRaises(ValueError):
                objects.get("../bad")
            request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
