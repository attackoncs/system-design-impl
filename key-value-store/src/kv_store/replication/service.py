"""Network-backed coordinator with explicit replica RPCs and owned quorum tasks."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import logging
import time

from .coordinator import (
    PutResult,
    GetResult,
    DeleteResult,
    QuorumNotMetError,
    RequestCoordinator,
)
from .records import Version, reconcile
from .vector_clock import VectorClock

logger = logging.getLogger(__name__)


class ReplicaCoordinator:
    def __init__(
        self,
        node_id,
        ring,
        quorum,
        records,
        transport,
        membership,
        hints,
        max_clock_entries=10,
    ):
        self.node_id, self.ring, self.quorum = node_id, ring, quorum
        self.records, self.transport, self.membership, self.hints = (
            records,
            transport,
            membership,
            hints,
        )
        self._tasks = set()
        self._requests = set()
        self._write_locks = [asyncio.Lock() for _ in range(64)]
        self._closed = False
        self._reserved = 0
        self._max_clock_entries = max_clock_entries

    def _targets(self, key):
        order = self.ring.get_nodes(key, len(self.ring.nodes))
        primary = order[: self.quorum.get_quorum_config().n]
        selected = list(primary)
        replacements = {}
        for index, node in enumerate(primary):
            if not self.membership.is_node_alive(node):
                substitute = next(
                    (
                        candidate
                        for candidate in order
                        if candidate not in selected
                        and self.membership.is_node_alive(candidate)
                    ),
                    None,
                )
                if substitute is not None:
                    selected[index] = substitute
                    replacements[substitute] = node
        return selected, replacements

    def _spawn(self, operation):
        task = asyncio.create_task(operation)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _collect(self, tasks, required):
        pending, responses = set(tasks), []
        while pending and len(responses) < required:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                result = task.result()
                if result is not None:
                    responses.append(result)
        if len(responses) < required:
            raise QuorumNotMetError(required, len(responses))
        # Remaining bounded RPCs continue and are owned until close().
        return responses

    async def _write_replica(self, node, key, version, original=None):
        try:
            if original is not None:
                await self.hints.save(original, key, [version])
            if node == self.node_id:
                await self.records.merge(key, [version])
                success = True
            else:
                address = self.membership.get_node_address(node)
                success = await self.transport.merge_replica(address, key, [version])
            if success:
                return node
        except Exception:
            logger.warning("Replica write failed: %s", node, exc_info=True)
        try:
            await self.hints.save(original or node, key, [version])
        except Exception:
            logger.exception("Unable to persist hint")
        return None

    @contextmanager
    def _admit(self):
        if self._closed:
            raise RuntimeError("coordinator is closed")
        reservation = self.quorum.get_quorum_config().n
        if self._reserved + len(self._tasks) + reservation > 256:
            raise RuntimeError("replica task capacity reached; retry later")
        # Reserve before the first await, including requests waiting on a key lock.
        self._reserved += reservation
        try:
            request = asyncio.current_task()
        except RuntimeError:  # synchronous admission inspection without a loop
            request = None
        if request is not None:
            self._requests.add(request)
        try:
            yield
        finally:
            self._reserved -= reservation
            self._requests.discard(request)

    async def _write(self, key, value, client_clock, consistency):
        with self._admit():
            return await self._write_admitted(key, value, client_clock, consistency)

    async def _write_admitted(self, key, value, client_clock, consistency):
        RequestCoordinator._validate_key_value(key, value or b"")
        if not isinstance(key, str) or not key:
            raise ValueError("nonempty string key required")
        # Persist a local copy before fanout, including when coordinating outside
        # the preference list. This also preserves coordinator counters on restart.

        index = int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], "big") % 64
        async with self._write_locks[index]:
            old = await self.records.read(key)
            entries = (client_clock or VectorClock()).to_dict()
            entries[self.node_id] = max(
                entries.get(self.node_id, 0),
                max((v.clock.to_dict().get(self.node_id, 0) for v in old), default=0),
            )
            # Never prune the coordinator's own counter: it prevents version
            # reuse after restart. Dropping other entries still loses causality.
            while len(entries) > self._max_clock_entries:
                victim = min(
                    (node for node in entries if node != self.node_id), key=entries.get
                )
                del entries[victim]
            clock = VectorClock(entries, self._max_clock_entries).increment(
                self.node_id
            )
            version = Version(value, clock, time.time())
            await self.records.merge(key, [version])
            targets, replacements = self._targets(key)
            tasks = [
                self._spawn(
                    self._write_replica(node, key, version, replacements.get(node))
                )
                for node in targets
            ]
            responses = await self._collect(
                tasks, self.quorum.get_quorum_config(consistency).w
            )
        return clock, len(responses)

    async def put(self, key, value, client_clock=None, consistency=None):
        if not isinstance(value, bytes):
            raise ValueError("value must be bytes")
        clock, count = await self._write(key, value, client_clock, consistency)
        return PutResult(True, clock, count)

    async def delete(self, key, client_clock=None, consistency=None):
        clock, _ = await self._write(key, None, client_clock, consistency)
        return DeleteResult(True, clock)

    async def _read_replica(self, node, key):
        try:
            versions = (
                await self.records.read(key)
                if node == self.node_id
                else await self.transport.read_replica(
                    self.membership.get_node_address(node), key
                )
            )
            return versions  # [] is a successful absent read, None is a failed RPC.
        except Exception:
            logger.warning("Replica read failed: %s", node, exc_info=True)
            return None

    async def get(self, key, consistency=None):
        with self._admit():
            return await self._get_admitted(key, consistency)

    async def _get_admitted(self, key, consistency):
        RequestCoordinator._validate_key_value(key, b"")
        targets, _ = self._targets(key)
        responses = await self._collect(
            [self._spawn(self._read_replica(node, key)) for node in targets],
            self.quorum.get_quorum_config(consistency).r,
        )
        versions = reconcile(
            [version for response in responses for version in response]
        )
        if not versions:
            return GetResult(False)
        if len(versions) == 1:
            version = versions[0]
            return GetResult(version.value is not None, version.value, version.clock)
        return GetResult(
            True,
            has_conflict=True,
            conflicting_values=[(v.value, v.clock) for v in versions],
        )

    def reopen(self):
        if self._requests or self._tasks:
            raise RuntimeError("previous requests have not drained")
        self._closed = False

    async def close(self):
        self._closed = True
        requests = tuple(
            task for task in self._requests if task is not asyncio.current_task()
        )
        for task in requests:
            task.cancel()
        await asyncio.gather(*requests, return_exceptions=True)
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
