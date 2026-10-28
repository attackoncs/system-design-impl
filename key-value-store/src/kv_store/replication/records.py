"""Durable sibling records: causal metadata travels with values through the LSM."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import time
from dataclasses import dataclass

from kv_store.async_work import complete

from .vector_clock import VectorClock


@dataclass(frozen=True)
class Version:
    value: bytes | None
    clock: VectorClock
    timestamp: float


def encode(versions: list[Version]) -> bytes:
    rows = [
        {
            "value": None if v.value is None else base64.b64encode(v.value).decode(),
            "clock": v.clock.to_dict(),
            "timestamp": v.timestamp,
        }
        for v in versions
    ]
    rows.sort(key=lambda row: json.dumps(row, sort_keys=True))
    return json.dumps(
        {"format": 1, "versions": rows},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate record field")
        result[key] = value
    return result


def decode(raw: bytes) -> list[Version]:
    if len(raw) > 1024 * 1024:
        raise ValueError("replica record exceeds 1 MiB")
    data = json.loads(raw, object_pairs_hook=_unique_fields)
    if (
        not isinstance(data, dict)
        or set(data) != {"format", "versions"}
        or type(data["format"]) is not int
        or data["format"] != 1
    ):
        raise ValueError("unsupported replica record format")
    rows = data.get("versions")
    if not isinstance(rows, list) or len(rows) > 256:
        raise ValueError("expected at most 256 sibling versions")
    versions = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"clock", "timestamp", "value"}:
            raise ValueError("invalid version")
        clock, timestamp = row.get("clock"), row.get("timestamp")
        if (
            not isinstance(clock, dict)
            or len(clock) > 256
            or any(
                not isinstance(node, str)
                or not node
                or len(node.encode("utf-8")) > 256
                or type(count) is not int
                or not 0 <= count <= (1 << 63) - 1
                for node, count in clock.items()
            )
        ):
            raise ValueError("invalid vector clock")
        if (
            type(timestamp) not in (int, float)
            or not math.isfinite(timestamp)
            or not 0 <= timestamp <= 1e12
        ):
            raise ValueError("invalid timestamp")
        if "value" not in row or (
            row["value"] is not None and not isinstance(row["value"], str)
        ):
            raise ValueError("invalid value encoding")
        value = (
            None
            if row["value"] is None
            else base64.b64decode(row["value"], validate=True)
        )
        if value is not None and len(value) > 10 * 1024:
            raise ValueError("value exceeds 10 KiB")
        versions.append(Version(value, VectorClock.from_dict(clock), timestamp))
    return versions


def reconcile(versions: list[Version]) -> list[Version]:
    """Keep causal siblings; equal clocks with different values remain conflicts."""
    unique = {}
    for version in versions:
        identity = (version.value, tuple(sorted(version.clock.to_dict().items())))
        previous = unique.get(identity)
        if previous is None or version.timestamp > previous.timestamp:
            unique[identity] = version
    candidates = list(unique.values())
    return [
        v
        for v in candidates
        if not any(other.clock.dominates(v.clock) for other in candidates)
    ]


class ReplicaRecords:
    """Per-key merge serialization using a bounded array of asyncio locks."""

    def __init__(self, engine, tree):
        self.engine, self.tree = engine, tree
        self._locks = [asyncio.Lock() for _ in range(64)]

    def lock(self, key):
        index = int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], "big") % len(
            self._locks
        )
        return self._locks[index]

    async def read(self, key):
        result = await self.engine.get(key)
        if result is None or not result.found:
            return []
        return decode(result.value)

    async def merge(self, key, versions):
        # Freeze and validate mutable input before waiting for a striped lock.
        frozen = decode(encode(versions))
        return await complete(self._merge(key, frozen))

    async def _merge(self, key, versions):
        async with self.lock(key):
            stored = await self.engine.get(key)
            existing = (
                decode(stored.value) if stored is not None and stored.found else []
            )
            merged = reconcile([*existing, *versions])
            raw = encode(merged)
            decode(raw)  # apply the same limits to local and remote merges
            if raw == encode(existing):
                return True
            # Metadata and values are one WAL/SSTable record, never separate commits.
            timestamp = time.time()
            if stored is not None:
                timestamp = max(timestamp, math.nextafter(stored.timestamp, math.inf))
            try:
                await self.engine.put(key, raw, timestamp)
            except BaseException:
                # A flush error can follow a durable WAL/memtable update. Keep
                # the index consistent with what reads actually observe while
                # still reporting the failed/uncertain write to the caller.
                observed = await self.engine.get(key)
                if observed is not None and observed.found:
                    decode(observed.value)
                    self.tree.update(key, hashlib.sha256(observed.value).hexdigest())
                raise
            self.tree.update(key, hashlib.sha256(raw).hexdigest())
        return True

    async def rebuild(self):
        for key, result in (await self.engine.scan()).items():
            if result.found:
                decode(result.value)  # fail explicitly on incompatible legacy data
                self.tree.update(key, hashlib.sha256(result.value).hexdigest())
