"""Top-down Merkle comparison and bounded, resumable-by-retry record pages."""

from __future__ import annotations

import asyncio
from collections import deque

from kv_store.replication.records import decode


class RepairChanged(RuntimeError):
    """A concurrent mutation invalidated a repair view; a later round retries."""


class ReplicaRepair:
    def __init__(self, node_id, records, membership, ring, replicas, transport):
        self.node_id, self.records, self.membership = node_id, records, membership
        self.ring, self.replicas, self.transport = ring, replicas, transport
        self._lock = asyncio.Lock()
        self.last_stats = {}

    @staticmethod
    def _check_reply(reply, local, expected, indices):
        if reply.bucket_count != local.bucket_count or (
            expected and reply.root_hash != expected
        ):
            raise RepairChanged("remote Merkle view changed")
        if len(reply.root_hash) != 64 or any(
            c not in "0123456789abcdef" for c in reply.root_hash
        ):
            raise ValueError("invalid Merkle root")
        if sorted(row.index for row in reply.nodes) != sorted(indices):
            raise ValueError("incomplete or duplicate Merkle nodes")
        if any(
            len(row.hash) != 64 or any(c not in "0123456789abcdef" for c in row.hash)
            for row in reply.nodes
        ):
            raise ValueError("invalid Merkle node digest")

    async def _differences(self, address, local, stats):
        pending, buckets, root = deque([1]), [], ""
        while pending:
            indices = [pending.popleft() for _ in range(min(64, len(pending)))]
            reply = await self.transport.merkle_inspect(
                address, local.bucket_count, indices, root
            )
            self._check_reply(reply, local, root, indices)
            root = reply.root_hash
            stats["metadata_rpcs"] += 1
            stats["compared_nodes"] += len(indices)
            for row in reply.nodes:
                if row.hash == local.node_hash(row.index):
                    continue
                if row.index >= local.bucket_count:
                    buckets.append(row.index - local.bucket_count)
                else:
                    pending.extend((row.index * 2, row.index * 2 + 1))
        return root, sorted(buckets)

    async def sync(self, peer):
        async with self._lock:
            return await self._sync(peer)

    async def _sync(self, peer):
        address = self.membership.get_node_address(peer)
        placement = self.ring.snapshot()
        local = self.records.tree.snapshot()
        stats = dict(
            metadata_rpcs=0,
            compared_nodes=0,
            differing_buckets=0,
            pages=0,
            received_records=0,
            pulled_records=0,
            pushed_records=0,
        )
        self.last_stats = stats
        root, buckets = await self._differences(address, local, stats)
        stats["differing_buckets"] = len(buckets)
        # Complete the pull before pushing anything: a push changes the remote
        # root and would invalidate the remaining pages of its captured view.
        for bucket in buckets:
            after = ""
            while True:
                page = await self.transport.repair_bucket_page(
                    address, local.bucket_count, bucket, root, after
                )
                stats["pages"] += 1
                if page.root_hash != root or len(page.records) > 128:
                    raise ValueError("invalid repair page")
                previous = after
                for row in page.records:
                    if (
                        not row.key
                        or row.key <= previous
                        or len(row.key.encode()) > 256
                        or self.records.tree._key_to_bucket(row.key) != bucket
                    ):
                        raise ValueError("unordered or misplaced repair row")
                    versions = decode(row.record)
                    if not versions:
                        raise ValueError("empty repair record")
                    previous = row.key
                    stats["received_records"] += 1
                    if self.node_id in placement.get_nodes(row.key, self.replicas):
                        await self.records.merge(row.key, versions)
                        stats["pulled_records"] += 1
                if page.next_after != previous or (not page.done and previous == after):
                    raise ValueError("repair cursor did not advance")
                after = previous
                if page.done:
                    break
        expected = self.records.tree.get_root_hash()
        for bucket in buckets:
            after = ""
            while True:
                if self.records.tree.get_root_hash() != expected:
                    raise RepairChanged("local Merkle view changed")
                keys, more = self.records.tree.key_page(bucket, after)
                for key in keys:
                    if peer in placement.get_nodes(key, self.replicas):
                        versions = await self.records.read(key)
                        if self.records.tree.get_root_hash() != expected:
                            raise RepairChanged("local Merkle view changed")
                        if not await self.transport.merge_replica(
                            address, key, versions
                        ):
                            raise RuntimeError("peer did not acknowledge repair")
                        stats["pushed_records"] += 1
                if not more:
                    break
                after = keys[-1]
        return dict(stats)
