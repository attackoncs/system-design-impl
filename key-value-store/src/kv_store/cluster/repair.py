"""Durable handoff journal and topology-aware bidirectional replica repair."""

from __future__ import annotations

import asyncio
from contextlib import closing
import sqlite3
from pathlib import Path

from kv_store.replication.records import encode, decode


class HintJournal:
    def __init__(self, path, membership, transport):
        self.path = Path(path)
        self.membership, self.transport = membership, transport
        self._lock = asyncio.Lock()
        self._cursor = 0

    def _execute(self, sql, args=()):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS hints(id INTEGER PRIMARY KEY, target TEXT, key TEXT, record BLOB)"
            )
            result = db.execute(sql, args).fetchall()
            db.commit()
            return result

    async def save(self, target, key, versions):
        async with self._lock:
            await asyncio.to_thread(
                self._execute,
                "INSERT INTO hints(target,key,record) VALUES(?,?,?)",
                (target, key, encode(versions)),
            )

    async def deliver(self):
        async with self._lock:
            rows = await asyncio.to_thread(
                self._execute,
                "SELECT id,target,key,record FROM hints WHERE id > ? ORDER BY id LIMIT 100",
                (self._cursor,),
            )
        self._cursor = rows[-1][0] if rows else 0
        for identity, target, key, raw in rows:
            if not self.membership.is_node_alive(target):
                continue
            try:
                success = await self.transport.merge_replica(
                    self.membership.get_node_address(target), key, decode(raw)
                )
            except Exception:
                continue
            if success:
                async with self._lock:
                    await asyncio.to_thread(
                        self._execute, "DELETE FROM hints WHERE id=?", (identity,)
                    )


class ReplicaRepair:
    def __init__(self, node_id, records, membership, ring, replicas, transport):
        self.node_id, self.records, self.membership = node_id, records, membership
        self.ring, self.replicas, self.transport = ring, replicas, transport

    async def sync(self, peer):
        address = self.membership.get_node_address(peer)
        buckets, rows = await self.transport.merkle_tree_sync(
            address, self.records.tree.get_bucket_hashes()
        )
        for row in rows:
            if self.node_id in self.ring.get_nodes(row["key"], self.replicas):
                await self.records.merge(row["key"], decode(row["value"]))
        for bucket in buckets:
            for key in self.records.tree.get_keys_in_bucket(bucket):
                if peer in self.ring.get_nodes(key, self.replicas):
                    await self.transport.merge_replica(
                        address, key, await self.records.read(key)
                    )
