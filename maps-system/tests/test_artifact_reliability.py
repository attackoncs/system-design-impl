"""Actual precomputed tiles, immutable batches, corrupt data and concurrency."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from maps_service.artifacts import TileArchive, precompute
from maps_service.geo import png
from maps_service.render import render_tile
from maps_service.store import Error, Store


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "catalog.sqlite")
        self.graph = json.loads((ROOT / "examples/roads.json").read_text())
        self.head = self.store.import_graph(self.graph)
        self.archive = TileArchive(self.root / "tiles.sqlite")

    def publish(self, records):
        return self.archive.publish(
            self.head["graph"], self.head["graph_hash"], records
        )

    def get(self, z=1, x=0, y=0, format="png"):
        return self.archive.get(
            self.head["graph"], self.head["graph_hash"], z, x, y, format
        )

    def rows(self):
        with closing(sqlite3.connect(self.archive.path)) as db, db:
            return db.execute("SELECT count(*) FROM tile_objects").fetchone()[0]

    def test_real_png_and_geojson_survive_restart_and_skip_rendering(self):
        result = precompute(self.store, self.archive, [[0, 0, 0], [0, 1, 0]])
        self.assertEqual((result["tiles"], result["objects"]), (1, 2))
        reopened = Store(self.store.path)
        reopened.tile_archive = TileArchive(self.archive.path)
        with (
            patch.object(
                reopened,
                "spatial_edges",
                side_effect=AssertionError("must not load roads"),
            ),
            patch(
                "maps_service.render.png",
                side_effect=AssertionError("must not render PNG"),
            ),
        ):
            payload, content_type = render_tile(
                reopened, self.head["graph"], 0, 0, 0, "png"
            )
            vector, vector_type = render_tile(reopened, self.head["graph"], 0, 0, 0)
        self.assertEqual(content_type, "image/png")
        self.assertEqual(payload[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(vector_type, "application/geo+json")
        document = json.loads(vector)
        self.assertEqual(document["graph"], self.head["graph"])
        self.assertTrue(document["features"])

    def test_precompute_bypasses_callers_archive_and_shared_render_cache(self):
        precompute(self.store, self.archive, [[0, 0, 0]])
        self.store.tile_archive = self.archive
        tiles = self.store.tiles
        render_tile(self.store, self.head["graph"], 0, 0, 0, "png")
        with patch("maps_service.render.png", wraps=png) as rendered:
            precompute(self.store, self.archive, [[0, 0, 0]])
        self.assertEqual(rendered.call_count, 1)
        self.assertIs(self.store.tile_archive, self.archive)
        self.assertIs(self.store.tiles, tiles)

    def test_conflicting_existing_object_rolls_back_entire_batch(self):
        self.publish([(1, 1, 0, "png", b"existing")])
        with self.assertRaises(Error) as failure:
            self.publish([(1, 0, 0, "png", b"new"), (1, 1, 0, "png", b"changed")])
        self.assertEqual(failure.exception.status, 409)
        self.assertIsNone(self.get())
        self.assertEqual(self.get(x=1)[0], b"existing")
        self.assertEqual(self.rows(), 1)

    def test_conflicting_duplicates_do_not_create_partial_graph_registration(self):
        with self.assertRaises(Error):
            self.publish([(1, 0, 0, "png", b"one"), (1, 0, 0, "png", b"two")])
        self.assertEqual(self.rows(), 0)
        with closing(sqlite3.connect(self.archive.path)) as db, db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM tile_graphs").fetchone()[0], 0
            )

    def test_graph_hash_mismatch_is_rejected_even_for_missing_xyz(self):
        self.publish([(1, 0, 0, "png", b"one")])
        with self.assertRaises(Error) as failure:
            self.archive.get(self.head["graph"], "0" * 64, 1, 1, 0, "png")
        self.assertEqual(failure.exception.status, 503)
        with self.assertRaises(Error) as failure:
            self.archive.publish(
                self.head["graph"], "0" * 64, [(1, 1, 0, "png", b"two")]
            )
        self.assertEqual(failure.exception.status, 409)

    def test_checksum_and_non_blob_corruption_are_not_served_or_overwritten(self):
        self.publish([(1, 0, 0, "png", b"original")])
        for payload in (b"corrupt", "wrong-type"):
            with closing(sqlite3.connect(self.archive.path)) as db, db:
                db.execute("UPDATE tile_objects SET payload=?", (payload,))
            with self.subTest(payload=type(payload).__name__), self.assertRaises(Error):
                self.get()
            with self.assertRaises(Error):
                self.publish([(1, 0, 0, "png", b"original")])

    def test_oversized_stored_body_is_rejected_before_blob_select(self):
        self.publish([(1, 0, 0, "png", b"original")])
        with closing(sqlite3.connect(self.archive.path)) as db, db:
            db.execute(
                "UPDATE tile_objects SET payload=zeroblob(?)", (16 * 1024 * 1024 + 1,)
            )
        statements, original = [], self.archive.connect

        def connection():
            db = original()
            db.set_trace_callback(statements.append)
            return db

        with patch.object(self.archive, "connect", side_effect=connection):
            for operation in (
                self.get,
                lambda: self.publish([(1, 0, 0, "png", b"original")]),
            ):
                with self.assertRaises(Error):
                    operation()
        self.assertTrue(any("length(payload)" in sql for sql in statements))
        self.assertFalse(
            any(sql.startswith("SELECT payload FROM") for sql in statements)
        )

    def test_iterator_capture_is_owned_and_does_not_hold_writer(self):
        entered, release = threading.Event(), threading.Event()
        record = [1, 0, 0, "png", b"first"]

        def records():
            yield record
            entered.set()
            self.assertTrue(release.wait(3))
            record[4] = b"mutated-later"
            yield (1, 1, 0, "png", b"second")

        with ThreadPoolExecutor(2) as pool:
            future = pool.submit(self.publish, records())
            try:
                self.assertTrue(entered.wait(1))
                pool.submit(self.publish, [(1, 0, 1, "png", b"unrelated")]).result(1)
            finally:
                release.set()
            future.result(3)
        self.assertEqual(self.get()[0], b"first")
        self.assertEqual(self.get(x=1)[0], b"second")

    def test_broken_or_unbounded_iterators_leave_no_partial_batch(self):
        def broken():
            yield (1, 0, 0, "png", b"one")
            raise OSError("source unavailable")

        with self.assertRaises(OSError):
            self.publish(broken())
        with self.assertRaises(Error):
            self.publish((10, i, 0, "png", b"one") for i in range(513))
        self.assertEqual(self.rows(), 0)

    def test_payload_and_total_batch_byte_budgets_cannot_be_bypassed(self):
        with self.assertRaises(Error):
            self.publish([(1, 0, 0, "png", bytes(16 * 1024 * 1024 + 1))])
        payload = bytes(4 * 1024 * 1024)
        with self.assertRaises(Error):
            self.publish([(5, i, 0, "png", payload) for i in range(17)])
        self.assertEqual(self.rows(), 0)

    def test_precompute_budget_failure_and_graph_swap_publish_no_objects(self):
        with self.assertRaises(Error) as failure:
            precompute(self.store, self.archive, [[0, 0, 0]], max_bytes=1)
        self.assertEqual(failure.exception.status, 413)
        original = render_tile

        def swapped(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[5] == "geojson":
                self.store.import_graph(self.graph)
            return result

        with patch("maps_service.render.render_tile", side_effect=swapped):
            with self.assertRaises(Error) as failure:
                precompute(self.store, self.archive, [[0, 0, 0]])
        self.assertEqual(failure.exception.status, 409)
        self.assertEqual(self.rows(), 0)

    def test_concurrent_identical_publish_and_conflict_choose_complete_content(self):
        records = [(1, 0, 0, "png", b"same"), (1, 1, 0, "png", b"other")]
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(lambda _: self.publish(records), range(16)))
        self.assertEqual(self.rows(), 2)
        self.assertEqual(self.get()[0], b"same")
        with self.assertRaises(Error):
            self.publish([(1, 0, 0, "png", b"conflicting")])

    def test_configuration_identity_and_tile_bounds_are_strict(self):
        for path in (None, "", ":memory:"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                TileArchive(path)
        for graph, digest in (
            ("bad", "0" * 64),
            (True, "0" * 64),
            (str(uuid.uuid4()), "ABC"),
        ):
            with self.subTest(graph=graph), self.assertRaises(ValueError):
                self.archive.publish(graph, digest, [(0, 0, 0, "png", b"x")])
        for options in (
            {"max_tiles": True},
            {"max_tiles": 257},
            {"max_bytes": True},
            {"max_bytes": 64 * 1024 * 1024 + 1},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                precompute(self.store, self.archive, [[0, 0, 0]], **options)
        with self.assertRaises(ValueError):
            precompute(self.store, TileArchive(self.store.path), [[0, 0, 0]])


if __name__ == "__main__":
    unittest.main()
