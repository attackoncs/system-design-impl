"""Persistent immutable precomputed tiles, independent of catalog/private state."""

from contextlib import closing
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .geo import tile
from .store import Error

_TYPES = {"png": "image/png", "geojson": "application/geo+json"}
_MAX_OBJECT = 16 * 1024 * 1024
_MAX_BATCH_BYTES = 64 * 1024 * 1024
_MAX_BATCH_OBJECTS = 512


def _identity(graph, graph_hash):
    if not isinstance(graph, str) or str(uuid.UUID(graph)) != graph:
        raise ValueError("canonical graph UUID required")
    if (
        not isinstance(graph_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", graph_hash) is None
    ):
        raise ValueError("canonical SHA-256 graph hash required")


def _format(value):
    if not isinstance(value, str) or value not in _TYPES:
        raise ValueError("unsupported precomputed tile format")
    return value


def _records(records):
    """Own a finite iterator and checksum payloads before acquiring the writer."""
    try:
        iterator = iter(records)
    except TypeError:
        raise ValueError("tile records must be iterable") from None
    prepared, size = [], 0
    for record in iterator:
        if len(prepared) == _MAX_BATCH_OBJECTS:
            raise Error(413, "precomputed object count exceeded")
        if not isinstance(record, (tuple, list)) or len(record) != 5:
            raise ValueError("invalid precomputed tile record")
        z, x, y, format, payload = tuple(record)
        z, x, y = tile(z, x, y)
        _format(format)
        if not isinstance(payload, bytes):
            raise ValueError("precomputed payload must be immutable bytes")
        size += len(payload)
        if len(payload) > _MAX_OBJECT or size > _MAX_BATCH_BYTES:
            raise Error(413, "precomputed byte budget exceeded")
        prepared.append((z, x, y, format, payload, hashlib.sha256(payload).hexdigest()))
    if not prepared:
        raise ValueError("precomputed batch must not be empty")
    return prepared


class TileArchive:
    """A local object-store reference with atomic batches and checksums.

    Keys include graph UUID and canonical XYZ/format. Existing content cannot be
    replaced under the same immutable key. It is independent of private catalog
    state, not a cloud object-store or CDN implementation.
    """

    def __init__(self, path):
        if (
            not isinstance(path, (str, os.PathLike))
            or not str(path)
            or str(path) == ":memory:"
        ):
            raise ValueError("tile archive requires a persistent database file")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            deadline = time.monotonic() + 15
            while True:
                try:
                    db.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as error:
                    if (
                        "locked" not in str(error).lower()
                        or time.monotonic() >= deadline
                    ):
                        raise
                    time.sleep(0.01)
            db.execute("""CREATE TABLE IF NOT EXISTS tile_graphs(
                graph TEXT PRIMARY KEY,hash TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS tile_objects(
                graph TEXT,z INTEGER,x INTEGER,y INTEGER,format TEXT,
                payload BLOB NOT NULL,hash TEXT NOT NULL,
                PRIMARY KEY(graph,z,x,y,format))""")
            db.commit()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.execute("PRAGMA busy_timeout=15000")
            return db
        except BaseException:
            db.close()
            raise

    @staticmethod
    def _object(db, key):
        # Do not fetch an oversized/non-BLOB payload just to discover corruption.
        row = db.execute(
            """SELECT length(payload),typeof(payload),hash FROM tile_objects
            WHERE graph=? AND z=? AND x=? AND y=? AND format=?""",
            key,
        ).fetchone()
        if row is None:
            return None
        if row[1] != "blob" or not 0 <= row[0] <= _MAX_OBJECT:
            raise Error(503, "precomputed tile type or size mismatch")
        payload = db.execute(
            "SELECT payload FROM tile_objects WHERE graph=? AND z=? AND x=? AND y=? AND format=?",
            key,
        ).fetchone()[0]
        if hashlib.sha256(payload).hexdigest() != row[2]:
            raise Error(503, "precomputed tile checksum mismatch")
        return payload, row[2]

    def get(self, graph, graph_hash, z, x, y, format):
        _identity(graph, graph_hash)
        z, x, y = tile(z, x, y)
        _format(format)
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            head = db.execute(
                "SELECT hash FROM tile_graphs WHERE graph=?", (graph,)
            ).fetchone()
            if head is None:
                return None
            if head[0] != graph_hash:
                raise Error(503, "precomputed graph identity mismatch")
            record = self._object(db, (graph, z, x, y, format))
        return (record[0], _TYPES[format]) if record is not None else None

    def publish(self, graph, graph_hash, records):
        """Publish at most 512 objects/64MiB atomically, after offline capture."""
        _identity(graph, graph_hash)
        prepared = _records(records)
        with closing(self.connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                head = db.execute(
                    "SELECT hash FROM tile_graphs WHERE graph=?", (graph,)
                ).fetchone()
                if head is not None and head[0] != graph_hash:
                    raise Error(409, "graph UUID conflicts with precomputed catalog")
                db.execute(
                    "INSERT OR IGNORE INTO tile_graphs VALUES(?,?)", (graph, graph_hash)
                )
                for z, x, y, format, payload, digest in prepared:
                    old = self._object(db, (graph, z, x, y, format))
                    if old is not None:
                        if old != (payload, digest):
                            raise Error(
                                409, "immutable tile already has different content"
                            )
                        continue
                    db.execute(
                        "INSERT INTO tile_objects VALUES(?,?,?,?,?,?,?)",
                        (graph, z, x, y, format, payload, digest),
                    )
                db.commit()
            except BaseException:
                db.rollback()
                raise


def precompute(store, archive, tiles, *, max_tiles=256, max_bytes=_MAX_BATCH_BYTES):
    """Generate explicit tiles without consuming shared render/archive caches.

    Each render is pinned to one graph UUID. A generation switch before final
    verification fails the batch. Two separate databases cannot atomically pin
    the active head: a later switch may still leave a valid complete old batch.
    """
    from .render import render_tile
    from .store import Store

    if type(max_tiles) is not int or not 1 <= max_tiles <= 256:
        raise ValueError("max_tiles must be 1..256")
    if type(max_bytes) is not int or not 1 <= max_bytes <= _MAX_BATCH_BYTES:
        raise ValueError("invalid precompute byte budget")
    if not isinstance(tiles, list) or not 1 <= len(tiles) <= max_tiles:
        raise ValueError("precompute requires a bounded nonempty XYZ list")
    if os.path.samefile(archive.path, store.path):
        raise ValueError("archive and private catalog must be separate databases")
    canonical = set()
    for entry in tiles:
        if not isinstance(entry, (list, tuple)) or len(entry) != 3:
            raise ValueError("invalid precompute XYZ entry")
        canonical.add(tile(*entry))
    # A separate facade has fresh caches and no tile_archive attribute. Never
    # mutate the caller's shared store to temporarily disable its archive.
    rendering = Store(store.path, readonly=store.readonly, clock=store.clock)
    with rendering.transaction() as db:
        meta = rendering.ready(db)
    prepared, size = [], 0
    for z, x, y in sorted(canonical):
        for format in _TYPES:
            payload, _content_type = render_tile(
                rendering, meta["graph"], z, x, y, format
            )
            size += len(payload)
            if len(payload) > _MAX_OBJECT or size > max_bytes:
                raise Error(413, "precompute byte budget exceeded")
            prepared.append((z, x, y, format, payload))
    with rendering.transaction() as db:
        current = rendering.ready(db)
        if (current["graph"], current["graph_hash"]) != (
            meta["graph"],
            meta["graph_hash"],
        ):
            raise Error(409, "graph changed during precomputation; retry")
    archive.publish(meta["graph"], meta["graph_hash"], prepared)
    return {
        "graph": meta["graph"],
        "tiles": len(canonical),
        "objects": len(prepared),
        "bytes": size,
    }
