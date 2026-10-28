"""Shard composition root; catalog, booking and saga APIs stay compatible."""

import math
import time

from .bookings import Bookings
from .cache import Cache
from .catalog import Catalog
from .common import Database, Error, identifier, integer, shard_for
from .saga import Saga
from .schema import initialize


class Store(Catalog, Bookings, Saga, Database):
    def __init__(self, path, shard=0, shards=1, hold_seconds=120, clock=time.time):
        if not callable(clock):
            raise ValueError("clock must be callable")
        super().__init__(path)
        self.shard, self.shards, self.hold_seconds, self.clock = (
            shard,
            shards,
            hold_seconds,
            clock,
        )
        integer(shards, "shards", 1, 32)
        integer(shard, "shard", 0, shards - 1)
        integer(hold_seconds, "hold_seconds", 1, 900)
        self.cache = Cache()
        initialize(self)
        with self.transaction(True) as db:
            db.execute(
                "INSERT OR IGNORE INTO metadata VALUES(1,?,?,0)", (shard, shards)
            )
            meta = db.execute("SELECT * FROM metadata").fetchone()
            if (meta["shard"], meta["shards"]) != (shard, shards):
                raise Error(409, "shard topology differs from stored metadata")

    def owner(self, hotel):
        identifier(hotel)
        if shard_for(hotel, self.shards) != self.shard:
            raise Error(409, "hotel belongs to another shard")

    @staticmethod
    def changed(db):
        db.execute("UPDATE metadata SET revision=revision+1")

    def revision(self):
        with self.transaction() as db:
            return db.execute("SELECT revision FROM metadata").fetchone()[0]

    def _now(self):
        value = self.clock()
        if (
            type(value) not in (int, float)
            or not 0 <= value <= 253402300799
            or not math.isfinite(value)
        ):
            raise ValueError("invalid shard clock")
        return value
