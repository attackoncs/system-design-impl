from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from object_storage.catalog import Catalog
from object_storage.common import Error, sha
from object_storage.listing import _encode, list_objects


def row(key, index, *, version=None):
    return {
        "key": key,
        "id": version or f"version-{index}",
        "seq": index + 1,
        "marker": 0,
        "size": 4,
        "sha": sha(b"data"),
        "created": 1000,
    }


class ListingReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1000
        self.catalog = Catalog(
            Path(self.temp.name) / "catalog.sqlite", clock=lambda: self.now
        )
        self.bucket = self.catalog.create("bucket-test", "alice")["id"]
        self.config = {
            "continuation_secret": "s" * 48,
            "metadata_urls": ["http://node-0", "http://node-1"],
            "internal_token": "t",
        }
        self.rows = [
            row(key, index)
            for index, key in enumerate(
                (
                    "CA/cities/la.txt",
                    "CA/cities/sf.txt",
                    "NY/cities/ny.txt",
                    "federal.txt",
                )
            )
        ]

    def request(self, url, token, body):
        index = int(url.split("node-")[1].split("/")[0])
        self.assertEqual(body["bucket"], self.bucket)
        return {"rows": deepcopy(self.rows[index::2])}

    def listing(self, **kwargs):
        return list_objects(
            self.config,
            self.catalog,
            self.bucket,
            "alice",
            request_fn=self.request,
            **kwargs,
        )

    def test_default_listing_remains_recursive_with_literal_prefix(self):
        value = self.listing(prefix="CA/")
        self.assertEqual(
            [item["key"] for item in value["objects"]],
            ["CA/cities/la.txt", "CA/cities/sf.txt"],
        )
        self.assertEqual(value["common_prefixes"], [])
        self.assertIsNone(value["continuation"])

    def test_delimiter_rolls_up_common_prefixes_once(self):
        value = self.listing(delimiter="/")
        self.assertEqual(value["common_prefixes"], ["CA/", "NY/"])
        self.assertEqual([item["key"] for item in value["objects"]], ["federal.txt"])
        nested = self.listing(prefix="CA/", delimiter="/")
        self.assertEqual(nested["common_prefixes"], ["CA/cities/"])
        self.assertEqual(nested["objects"], [])

    def test_prefixes_count_against_page_limit(self):
        first = self.listing(delimiter="/", limit=2)
        self.assertEqual(first["common_prefixes"], ["CA/", "NY/"])
        self.assertEqual(first["objects"], [])
        self.assertIsNotNone(first["continuation"])
        second = self.listing(delimiter="/", limit=2, token=first["continuation"])
        self.assertEqual([item["key"] for item in second["objects"]], ["federal.txt"])
        self.assertEqual(second["common_prefixes"], [])
        self.assertIsNone(second["continuation"])

    def test_frozen_pagination_does_not_repeat_prefixes_after_changes(self):
        first = self.listing(delimiter="/", limit=1)
        self.rows.clear()
        self.rows.append(row("AA/new.txt", 100))
        pages, token = [first], first["continuation"]
        while token:
            page = self.listing(delimiter="/", limit=1, token=token)
            pages.append(page)
            token = page["continuation"]
        self.assertEqual(
            [prefix for page in pages for prefix in page["common_prefixes"]],
            ["CA/", "NY/"],
        )
        self.assertEqual(
            [item["key"] for page in pages for item in page["objects"]], ["federal.txt"]
        )

    def test_token_binds_delimiter_prefix_versions_and_limit(self):
        first = self.listing(limit=1)
        for changes in (
            {"delimiter": "/"},
            {"prefix": "CA/"},
            {"versions": True},
            {"limit": 2},
        ):
            options = {"limit": 1, "token": first["continuation"], **changes}
            with self.assertRaises(Error) as caught:
                self.listing(**options)
            self.assertEqual(caught.exception.status, 409)

    def test_token_cannot_cross_bucket_or_owner(self):
        token = self.listing(limit=1)["continuation"]
        other = self.catalog.create("bucket-other", "alice")["id"]
        for bucket, user in ((other, "alice"), (self.bucket, "bob")):
            with self.assertRaises(Error):
                list_objects(
                    self.config,
                    self.catalog,
                    bucket,
                    user,
                    limit=1,
                    token=token,
                    request_fn=self.request,
                )

    def test_expiry_and_restart_preserve_durable_snapshot_semantics(self):
        first = self.listing(limit=1)
        self.catalog = Catalog(self.catalog.path, clock=lambda: self.now)
        second = self.listing(limit=1, token=first["continuation"])
        self.assertEqual(second["objects"][0]["key"], "CA/cities/sf.txt")
        self.now += 300
        with self.assertRaises(Error) as caught:
            self.listing(limit=1, token=first["continuation"])
        self.assertEqual(caught.exception.status, 409)

    def test_token_shape_signature_and_empty_token_fail(self):
        token = self.listing(limit=1)["continuation"]
        for invalid in ("", token + "x", "x.y.z", ".", [], "x" * 2049):
            with self.assertRaises(Error) as caught:
                self.listing(limit=1, token=invalid)
            self.assertEqual(caught.exception.status, 400)

    def test_query_requires_strict_limits_and_supported_delimiter(self):
        for options in (
            {"versions": "false"},
            {"versions": 0},
            {"limit": True},
            {"limit": 0},
            {"delimiter": "-"},
            {"delimiter": []},
            {"prefix": "a\x00"},
            {"prefix": "a" * 513},
        ):
            with self.assertRaises(ValueError):
                self.listing(**options)

    def test_malformed_metadata_and_duplicate_ids_fail_without_snapshot(self):
        for response in (
            {},
            {"rows": "bad"},
            {"rows": [{}]},
            {"rows": [self.rows[0], self.rows[0]]},
        ):
            with self.subTest(response=response):
                with self.assertRaises(Error) as caught:
                    list_objects(
                        self.config,
                        self.catalog,
                        self.bucket,
                        "alice",
                        request_fn=lambda *args: response,
                    )
                self.assertEqual(caught.exception.status, 502)
        with self.catalog.transaction() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM snapshots").fetchone()[0], 0
            )

    def test_duplicate_latest_key_and_nonfinite_metadata_are_rejected(self):
        for values in (
            [row("same", 0), row("same", 1)],
            [dict(row("valid", 0), created=float("nan"))],
        ):
            with self.assertRaises(Error) as caught:
                list_objects(
                    self.config,
                    self.catalog,
                    self.bucket,
                    "alice",
                    request_fn=lambda *args: {"rows": values},
                )
            self.assertEqual(caught.exception.status, 502)

    def test_versions_keep_descending_order_for_direct_objects(self):
        self.rows = [row("key", 1), row("key", 5)]
        value = self.listing(versions=True, delimiter="/")
        self.assertEqual([item["seq"] for item in value["objects"]], [6, 2])

    def test_snapshot_quotas_and_global_row_budget_fail_explicitly(self):
        for name, maximum in (
            ("MAX_SNAPSHOTS", 0),
            ("MAX_OWNER_SNAPSHOTS", 0),
            ("MAX_SNAPSHOT_BYTES", 1),
            ("MAX_ROWS", 2),
        ):
            with patch("object_storage.listing." + name, maximum):
                with self.assertRaises(Error) as caught:
                    self.listing(limit=1)
                self.assertEqual(caught.exception.status, 422)

    def test_single_page_listing_does_not_accumulate_unused_snapshots(self):
        for _ in range(4):
            self.listing()
        with self.catalog.transaction() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM snapshots").fetchone()[0], 0
            )

    def test_corrupted_snapshot_fails_closed(self):
        first = self.listing(limit=1)
        with self.catalog.transaction(True) as db:
            db.execute("UPDATE snapshots SET rows='null'")
        with self.assertRaises(Error) as caught:
            self.listing(limit=1, token=first["continuation"])
        self.assertEqual(caught.exception.status, 409)

    def test_legacy_snapshot_schema_and_recursive_tokens_migrate(self):
        path = Path(self.temp.name) / "legacy.sqlite"
        db = sqlite3.connect(path)
        try:
            db.executescript("""
                CREATE TABLE buckets(name TEXT PRIMARY KEY,id TEXT UNIQUE,user TEXT);
                CREATE TABLE snapshots(id TEXT PRIMARY KEY,user TEXT,bucket TEXT,prefix TEXT,
                    versions INTEGER,page_size INTEGER,expires REAL,rows TEXT);
            """)
            db.execute(
                "INSERT INTO buckets VALUES('legacy-bucket','legacy-id','alice')"
            )
            db.execute(
                "INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?)",
                (
                    "old-snapshot",
                    "alice",
                    "legacy-id",
                    "",
                    0,
                    1,
                    2000,
                    json.dumps([row("a", 0), row("b", 1)]),
                ),
            )
            db.commit()
        finally:
            db.close()
        catalog = Catalog(path, clock=lambda: 1000)
        token = _encode("old-snapshot", 1, self.config["continuation_secret"].encode())
        value = list_objects(
            self.config, catalog, "legacy-id", "alice", limit=1, token=token
        )
        self.assertEqual(value["objects"][0]["key"], "b")
        self.assertEqual(value["common_prefixes"], [])
        with catalog.transaction() as db:
            self.assertEqual(
                db.execute("SELECT delimiter FROM snapshots").fetchone()[0], ""
            )


if __name__ == "__main__":
    unittest.main()
