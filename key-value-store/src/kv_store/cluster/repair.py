"""Durable handoff journal and topology-aware bidirectional replica repair."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

from kv_store.async_work import complete
from kv_store.replication.records import decode, encode


class HintJournal:
    def __init__(self, path, membership, transport):
        self.path = Path(path)
        self.membership, self.transport = membership, transport
        self._lock = asyncio.Lock()
        self._delivery_lock = asyncio.Lock()
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
        raw = encode(versions)
        decode(raw)  # freeze and validate before waiting on SQL ownership
        async with self._lock:
            await complete(
                asyncio.to_thread(
                    self._execute,
                    "INSERT INTO hints(target,key,record) VALUES(?,?,?)",
                    (target, key, raw),
                )
            )

    async def deliver(self):
        async with self._delivery_lock:
            await self._deliver_round()

    async def _deliver_round(self):
        async with self._lock:
            rows = await complete(
                asyncio.to_thread(
                    self._execute,
                    "SELECT id,target,key,record FROM hints WHERE id > ? ORDER BY id LIMIT 100",
                    (self._cursor,),
                )
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
                    await complete(
                        asyncio.to_thread(
                            self._execute,
                            "DELETE FROM hints WHERE id=? AND target=? AND key=? AND record=?",
                            (identity, target, key, raw),
                        )
                    )


# Keep the historical import while separating wire repair from SQL handoff.
from kv_store.cluster.anti_entropy import ReplicaRepair as ReplicaRepair
