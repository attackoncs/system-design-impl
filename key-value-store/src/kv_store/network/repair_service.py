"""Bounded Merkle metadata and record pages, separate from CRUD handlers."""

from __future__ import annotations

import hashlib

import grpc

from kv_store.network import kvstore_pb2 as proto
from kv_store.replication.records import encode


class RepairService:
    def __init__(self, records, tree, max_message_size):
        self.records, self.tree = records, tree
        self.max_bytes = min(2 * 1024 * 1024, max_message_size - 1024)

    async def _check(self, request, context, *, require_root=False):
        if request.bucket_count != self.tree.bucket_count:
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION, "Merkle bucket counts differ"
            )
        root = self.tree.get_root_hash()
        if require_root and not request.expected_root:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "expected_root is required"
            )
        if request.expected_root and request.expected_root != root:
            await context.abort(
                grpc.StatusCode.ABORTED, "Merkle view changed; retry repair"
            )
        return root

    async def inspect(self, request, context):
        root = await self._check(request, context)
        if not 1 <= len(request.indices) <= 64 or len(set(request.indices)) != len(
            request.indices
        ):
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "expected1..64 unique indices"
            )
        try:
            rows = [
                proto.MerkleHashNode(index=index, hash=self.tree.node_hash(index))
                for index in request.indices
            ]
        except ValueError as error:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(error))
        return proto.MerkleInspectResponse(
            bucket_count=self.tree.bucket_count, root_hash=root, nodes=rows
        )

    async def page(self, request, context):
        root = await self._check(request, context, require_root=True)
        if len(request.after_key.encode("utf-8")) > 256:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "repair cursor exceeds key limit"
            )
        limit = request.limit or 64
        budget = min(request.max_bytes or self.max_bytes, self.max_bytes)
        if budget < 1024:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "repair byte budget too small"
            )
        try:
            keys, more = self.tree.key_page(request.bucket_id, request.after_key, limit)
        except ValueError as error:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(error))
        if self.records is None:
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION, "replica records unavailable"
            )
        response = proto.RepairBucketPageResponse(
            root_hash=root, next_after=request.after_key, done=not more
        )
        size = 512  # root, cursor and protobuf framing allowance
        for key in keys:
            expected_hash = self.tree.value_hash(key)
            versions = await self.records.read(key)
            await self._check(request, context, require_root=True)
            if not versions:
                await context.abort(
                    grpc.StatusCode.FAILED_PRECONDITION,
                    "Merkle index references a missing record",
                )
            raw = encode(versions)
            if hashlib.sha256(raw).hexdigest() != expected_hash:
                # The storage write can be visible while its committing task
                # still awaits flush before publishing the index update.
                await context.abort(
                    grpc.StatusCode.ABORTED,
                    "record commit has not published its Merkle digest",
                )
            row = proto.ReplicaRecord(key=key, record=raw)
            row_size = row.ByteSize() + 5
            if size + row_size > budget:
                if not response.records:
                    await context.abort(
                        grpc.StatusCode.RESOURCE_EXHAUSTED,
                        "one replica record exceeds repair page budget",
                    )
                response.done = False
                break
            response.records.append(row)
            response.next_after = key
            size += row_size
        return response
