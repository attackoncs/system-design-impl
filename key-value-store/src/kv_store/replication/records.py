"""Durable sibling records: causal metadata travels with values through the LSM."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import hashlib
import json
import math
import time

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


def decode(raw: bytes) -> list[Version]:
    if len(raw) > 1024 * 1024:
        raise ValueError("replica record exceeds 1 MiB")
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("format") != 1:
        raise ValueError("unsupported replica record format")
    rows = data.get("versions")
    if not isinstance(rows, list) or len(rows) > 256:
        raise ValueError("expected at most 256 sibling versions")
    versions = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid version")
        clock, timestamp = row.get("clock"), row.get("timestamp")
        if (
            not isinstance(clock, dict)
            or len(clock) > 256
            or any(
                not isinstance(node, str)
                or not node
                or type(count) is not int
                or count < 0
                for node, count in clock.items()
            )
        ):
            raise ValueError("invalid vector clock")
        if type(timestamp) not in (int, float) or not math.isfinite(timestamp):
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
        async with self.lock(key):
            existing = await self.read(key)
            merged = reconcile([*existing, *versions])
            raw = encode(merged)
            decode(raw)  # apply the same limits to local and remote merges
            if raw == encode(existing):
                return True
            # Metadata and values are one WAL/SSTable record, never separate commits.
            await self.engine.put(key, raw, time.time())
            self.tree.update(key, hashlib.sha256(raw).hexdigest())
        return True

    async def rebuild(self):
        for key, result in (await self.engine.scan()).items():
            if result.found:
                decode(result.value)  # fail explicitly on incompatible legacy data
                self.tree.update(key, hashlib.sha256(result.value).hexdigest())
