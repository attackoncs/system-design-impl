import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from object_storage.common import Error, sha, shard_for
from object_storage.metadata import Metadata
from object_storage.node import DataNode
from object_storage.service import Catalog


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_packed_offsets_index_recovery_and_incomplete_tail(self):
        node = DataNode(self.root / "node")
        values = [("small-" + str(i)).encode() for i in range(20)]
        for data in values:
            node.put(sha(data), data)
        self.assertEqual(node.status()["packed_chunks"], 20)
        self.assertEqual(node.status()["large_files"], 0)
        self.assertEqual(len(list(node.root.glob("pack-*.bin"))), 1)
        orphan = b"fsync-before-index"
        with node.pack.open("ab") as handle:
            node.record(handle, sha(orphan), orphan, 1000)
            handle.flush()
            os.fsync(handle.fileno())
            end = handle.tell()
            handle.write(b"S3P1xx")
        node = DataNode(node.root)
        self.assertEqual(node.get(sha(orphan)), orphan)
        self.assertEqual(node.pack.stat().st_size, end)
        for data in values:
            self.assertEqual(node.get(sha(data)), data)

    def test_gc_tombstones_large_files_and_compaction(self):
        node = DataNode(self.root / "node", clock=lambda: 1000)
        keep, drop, large = b"keep", b"drop", b"L" * 70000
        for data in (keep, drop, large):
            node.put(sha(data), data)
        old_bytes = node.status()["pack_bytes"]
        self.assertEqual(node.collect([sha(keep)], 0)["removed"], 2)
        node = DataNode(node.root)
        self.assertEqual(node.get(sha(keep)), keep)
        with self.assertRaises(Error):
            node.get(sha(drop))
        compact = node.compact()
        self.assertLess(compact["pack_bytes"], old_bytes)
        node = DataNode(node.root)
        self.assertEqual(node.status()["chunks"], 1)
        self.assertEqual(node.status()["large_files"], 0)

    def test_corruption_repair_and_input_checksum(self):
        node = DataNode(self.root / "node")
        data = b"original"
        node.put(sha(data), data)
        with node.transaction() as db:
            offset = db.execute("SELECT offset FROM chunks").fetchone()[0]
        with node.pack.open("r+b") as handle:
            handle.seek(offset)
            handle.write(b"X")
        with self.assertRaises(Error):
            node.get(sha(data))
        node.put(sha(data), data)
        self.assertEqual(DataNode(node.root).get(sha(data)), data)
        with self.assertRaises(Error):
            node.put(sha(data), b"other")
        with self.assertRaises(ValueError):
            node.get("../../escape")

    def test_versions_markers_idempotency_and_retention(self):
        meta = Metadata(self.root / "meta.sqlite", clock=lambda: 1000)
        one = meta.publish(
            "bucket", "key", "one", [{"hash": sha(b"one"), "size": 3}], 3, sha(b"one")
        )
        self.assertEqual(
            meta.publish(
                "bucket",
                "key",
                "one",
                [{"hash": sha(b"one"), "size": 3}],
                3,
                sha(b"one"),
            )["version"],
            one["version"],
        )
        with self.assertRaises(Error):
            meta.publish("bucket", "key", "one", [], 0, sha(b""))
        marker = meta.publish("bucket", "key", "delete", [], 0, sha(b""), True)
        self.assertEqual(meta.get("bucket", "key")["marker"], 1)
        self.assertEqual(meta.listing("bucket")["rows"], [])
        self.assertEqual(len(meta.listing("bucket", True)["rows"]), 2)
        meta.remove("bucket", "key", marker["version"])
        self.assertEqual(meta.get("bucket", "key")["id"], one["version"])
        meta.publish("bucket", "key", "two", [], 0, sha(b""))
        self.assertEqual(meta.prune("bucket", 1)["pruned"], 1)
        self.assertIn(sha(b"one"), meta.references(60)["hashes"])
        self.assertNotIn(sha(b"one"), meta.references(0)["hashes"])

    def test_topology_and_manifest_validation(self):
        meta = Metadata(self.root / "meta.sqlite", 0, 2)
        key = next(str(i) for i in range(100) if shard_for("bucket", str(i), 2) == 1)
        with self.assertRaises(Error):
            meta.get("bucket", key)
        with self.assertRaises(Error):
            Metadata(meta.path, 0, 1)
        meta = Metadata(self.root / "single.sqlite")
        for manifest, size in [([], 1), ([{"hash": sha(b"x"), "size": 2}], 1)]:
            with self.assertRaises(ValueError):
                meta.publish("bucket", "key", "request", manifest, size, sha(b"x"))

    def test_catalog_private_buckets_and_part_immutability(self):
        catalog = Catalog(self.root / "catalog.sqlite", clock=lambda: 1000)
        bucket = catalog.create("bucket-test", "alice")
        with self.assertRaises(Error):
            catalog.bucket("bucket-test", "bob")
        upload = catalog.start(
            bucket["id"],
            "alice",
            {
                "key": "path/key",
                "request_key": "upload",
                "sha256": sha(b"x"),
                "size": 1,
            },
        )
        self.assertEqual(
            catalog.start(
                bucket["id"],
                "alice",
                {
                    "key": "path/key",
                    "request_key": "upload",
                    "sha256": sha(b"x"),
                    "size": 1,
                },
            )["id"],
            upload["id"],
        )
        manifest = [{"hash": sha(b"x"), "size": 1}]
        catalog.add_part(upload["id"], "alice", 1, b"x", manifest)
        with self.assertRaises(Error):
            catalog.add_part(upload["id"], "alice", 1, b"y", manifest)
        with self.assertRaises(Error):
            catalog.upload(upload["id"], "bob")


if __name__ == "__main__":
    unittest.main()
