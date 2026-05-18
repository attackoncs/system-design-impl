"""gRPC client for inter-node communication in the distributed key-value store.

Provides connection pooling (one channel per target address) and methods for
all inter-node RPCs: Put, Get, Delete, Replicate, GossipExchange,
HintedHandoff, and MerkleTreeSync.

Handles connection failures gracefully by raising NodeUnavailableError.
"""

from __future__ import annotations

import logging
from typing import Optional

import grpc
import grpc.aio

from kv_store.network import kvstore_pb2
from kv_store.network import kvstore_pb2_grpc
from kv_store.replication.vector_clock import VectorClock

logger = logging.getLogger(__name__)


class NodeUnavailableError(Exception):
    """Raised when a target node cannot be reached.

    Attributes:
        target: The address of the unavailable node.
        cause: The underlying exception that caused the failure.
    """

    def __init__(self, target: str, cause: Optional[Exception] = None):
        self.target = target
        self.cause = cause
        msg = f"Node unavailable: {target}"
        if cause:
            msg += f" (cause: {cause})"
        super().__init__(msg)


def _vector_clock_to_proto(clock: VectorClock) -> kvstore_pb2.VectorClockProto:
    """Convert a VectorClock to its protobuf representation."""
    proto = kvstore_pb2.VectorClockProto()
    for node_id, counter in clock.to_dict().items():
        entry = kvstore_pb2.VectorClockEntry(node_id=node_id, counter=counter)
        proto.entries.append(entry)
    return proto


def _proto_to_vector_clock(proto: kvstore_pb2.VectorClockProto) -> VectorClock:
    """Convert a VectorClockProto to a VectorClock instance."""
    entries = {entry.node_id: entry.counter for entry in proto.entries}
    return VectorClock.from_dict(entries)


class GRPCClient:
    """gRPC client with connection pooling for inter-node communication.

    Maintains a pool of async channels keyed by target address.
    All methods raise NodeUnavailableError on connection failures.
    """

    def __init__(
        self,
        timeout: float = 5.0,
        max_message_size: int = 16 * 1024 * 1024,
    ):
        """Initialize the gRPC client.

        Args:
            timeout: Default timeout in seconds for RPC calls.
            max_message_size: Maximum message size in bytes.
        """
        self._timeout = timeout
        self._max_message_size = max_message_size
        self._channels: dict[str, grpc.aio.Channel] = {}

    def _get_channel(self, target: str) -> grpc.aio.Channel:
        """Get or create a channel for the target address.

        Implements connection pooling by reusing channels per target.

        Args:
            target: The target address (host:port).

        Returns:
            An async gRPC channel.
        """
        if target not in self._channels:
            options = [
                ("grpc.max_send_message_length", self._max_message_size),
                ("grpc.max_receive_message_length", self._max_message_size),
            ]
            self._channels[target] = grpc.aio.insecure_channel(
                target, options=options
            )
        return self._channels[target]

    def _get_stub(self, target: str) -> kvstore_pb2_grpc.KVStoreServiceStub:
        """Get a service stub for the target address.

        Args:
            target: The target address (host:port).

        Returns:
            A KVStoreService stub.
        """
        channel = self._get_channel(target)
        return kvstore_pb2_grpc.KVStoreServiceStub(channel)

    async def put(
        self,
        target: str,
        key: str,
        value: bytes,
        vector_clock: VectorClock,
    ) -> bool:
        """Send a Put request to a target node.

        Args:
            target: Target node address (host:port).
            key: The key to write.
            value: The value bytes.
            vector_clock: The vector clock for this write.

        Returns:
            True if the put was successful.

        Raises:
            NodeUnavailableError: If the target node cannot be reached.
        """
        try:
            stub = self._get_stub(target)
            request = kvstore_pb2.PutRequest(
                key=key,
                value=value,
                vector_clock=_vector_clock_to_proto(vector_clock),
            )
            response = await stub.Put(request, timeout=self._timeout)
            return response.success
        except grpc.RpcError as e:
            raise NodeUnavailableError(target, e) from e

    async def get(
        self,
        target: str,
        key: str,
    ) -> Optional[tuple[bytes, VectorClock]]:
        """Send a Get request to a target node.

        Args:
            target: Target node address (host:port).
            key: The key to read.

        Returns:
            Tuple of (value, vector_clock) if found, None if not found.

        Raises:
            NodeUnavailableError: If the target node cannot be reached.
        """
        try:
            stub = self._get_stub(target)
            request = kvstore_pb2.GetRequest(key=key)
            response = await stub.Get(request, timeout=self._timeout)

            if not response.found:
                return None

            clock = _proto_to_vector_clock(response.vector_clock)
            return (response.value, clock)
        except grpc.RpcError as e:
            raise NodeUnavailableError(target, e) from e

    async def replicate(
        self,
        target: str,
        key: str,
        value: bytes,
        vector_clock: VectorClock,
        timestamp: float,
        is_tombstone: bool = False,
    ) -> bool:
        """Send a Replicate request to a target node.

        Used for inter-node replication of writes.

        Args:
            target: Target node address (host:port).
            key: The key being replicated.
            value: The value bytes.
            vector_clock: The vector clock for this write.
            timestamp: The write timestamp.
            is_tombstone: Whether this is a delete tombstone.

        Returns:
            True if replication was successful.

        Raises:
            NodeUnavailableError: If the target node cannot be reached.
        """
        try:
            stub = self._get_stub(target)
            request = kvstore_pb2.ReplicateRequest(
                key=key,
                value=value,
                vector_clock=_vector_clock_to_proto(vector_clock),
                timestamp=timestamp,
                is_tombstone=is_tombstone,
            )
            response = await stub.Replicate(request, timeout=self._timeout)
            return response.success
        except grpc.RpcError as e:
            raise NodeUnavailableError(target, e) from e

    async def gossip_exchange(
        self,
        target: str,
        members: list,
    ) -> list:
        """Send a GossipExchange request to a target node.

        Sends local membership list and receives the remote membership list.

        Args:
            target: Target node address (host:port).
            members: List of MemberInfo objects to send.

        Returns:
            List of MemberInfo-like dicts from the remote node.

        Raises:
            NodeUnavailableError: If the target node cannot be reached.
        """
        try:
            stub = self._get_stub(target)
            request = kvstore_pb2.GossipMessage()

            for member in members:
                member_proto = kvstore_pb2.MemberInfoProto(
                    node_id=member.node_id,
                    address=member.address,
                    heartbeat_counter=member.heartbeat_counter,
                    status=member.status.value if hasattr(member.status, "value") else str(member.status),
                )
                request.members.append(member_proto)

            response = await stub.GossipExchange(request, timeout=self._timeout)

            # Convert response members back to a simple list of dicts
            remote_members = []
            for member_proto in response.members:
                remote_members.append({
                    "node_id": member_proto.node_id,
                    "address": member_proto.address,
                    "heartbeat_counter": member_proto.heartbeat_counter,
                    "status": member_proto.status,
                })

            return remote_members
        except grpc.RpcError as e:
            raise NodeUnavailableError(target, e) from e

    async def send_hinted_handoff(
        self,
        target: str,
        hint,
    ) -> bool:
        """Send a HintedHandoff request to a target node.

        Delivers hinted data to a recovered node.

        Args:
            target: Target node address (host:port).
            hint: HintedData object with key, value, vector_clock, timestamp, is_tombstone.

        Returns:
            True if delivery was successful.

        Raises:
            NodeUnavailableError: If the target node cannot be reached.
        """
        try:
            stub = self._get_stub(target)

            # Convert vector clock
            vc_proto = kvstore_pb2.VectorClockProto()
            if hasattr(hint, "vector_clock") and hint.vector_clock is not None:
                if isinstance(hint.vector_clock, VectorClock):
                    vc_proto = _vector_clock_to_proto(hint.vector_clock)

            request = kvstore_pb2.HintedHandoffRequest(
                target_node_id=hint.target_node_id if hasattr(hint, "target_node_id") else "",
                key=hint.key,
                value=hint.value if hint.value else b"",
                vector_clock=vc_proto,
                timestamp=hint.timestamp if hasattr(hint, "timestamp") else 0.0,
                is_tombstone=hint.is_tombstone if hasattr(hint, "is_tombstone") else False,
            )
            response = await stub.HintedHandoff(request, timeout=self._timeout)
            return response.success
        except grpc.RpcError as e:
            raise NodeUnavailableError(target, e) from e

    async def merkle_tree_sync(
        self,
        target: str,
        bucket_hashes: dict[int, str],
    ) -> tuple[list[int], list[dict]]:
        """Send a MerkleTreeSync request to a target node.

        Sends local bucket hashes and receives differing buckets
        along with the remote node's key-value pairs for those buckets.

        Args:
            target: Target node address (host:port).
            bucket_hashes: Dict mapping bucket_id to hash string.

        Returns:
            Tuple of (differing_bucket_ids, key_value_pairs).

        Raises:
            NodeUnavailableError: If the target node cannot be reached.
        """
        try:
            stub = self._get_stub(target)
            request = kvstore_pb2.MerkleTreeSyncRequest(
                bucket_hashes={k: v for k, v in bucket_hashes.items()},
            )
            response = await stub.MerkleTreeSync(request, timeout=self._timeout)

            differing_buckets = list(response.differing_buckets)
            key_value_pairs = []
            for kv in response.key_value_pairs:
                key_value_pairs.append({
                    "key": kv.key,
                    "value": kv.value,
                    "timestamp": kv.timestamp,
                    "is_tombstone": kv.is_tombstone,
                })

            return (differing_buckets, key_value_pairs)
        except grpc.RpcError as e:
            raise NodeUnavailableError(target, e) from e

    async def close(self) -> None:
        """Close all open channels and clean up resources."""
        for target, channel in self._channels.items():
            await channel.close()
            logger.debug("Closed channel to %s", target)
        self._channels.clear()
        logger.info("All gRPC client channels closed")

    @property
    def active_channels(self) -> int:
        """Number of active channels in the pool."""
        return len(self._channels)
