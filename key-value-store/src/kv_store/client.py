"""Client API for the distributed key-value store.

Provides a high-level async client (KVClient) that connects to the cluster
via gRPC, supports put/get/delete operations with tunable consistency,
automatic retry on transient failures, and failover to alternate seed nodes.

Covers: FR-1.1, FR-1.2, FR-1.3, FR-1.5
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

import grpc
import grpc.aio

from kv_store.network import kvstore_pb2
from kv_store.network import kvstore_pb2_grpc
from kv_store.replication.vector_clock import VectorClock

logger = logging.getLogger(__name__)


@dataclass
class ClientConfig:
    """Configuration for the KVClient.

    Attributes:
        seed_nodes: List of node addresses (host:port) to connect to.
        timeout: Timeout in seconds for each RPC call.
        retry_count: Number of retries on transient failures per node.
        retry_delay: Delay in seconds between retries.
    """

    seed_nodes: list[str] = field(default_factory=list)
    timeout: float = 5.0
    retry_count: int = 3
    retry_delay: float = 0.5


@dataclass
class KVResponse:
    """Response from a KV operation.

    Attributes:
        success: Whether the operation succeeded.
        value: The value bytes (for get operations).
        vector_clock: The vector clock associated with the response.
        has_conflict: Whether conflicting versions were detected.
        conflicting_values: List of (value, vector_clock) tuples for conflicts.
    """

    success: bool
    value: Optional[bytes] = None
    vector_clock: Optional[VectorClock] = None
    has_conflict: bool = False
    conflicting_values: list[tuple[bytes, VectorClock]] = field(default_factory=list)


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


class KVClient:
    """Async client for the distributed key-value store.

    Connects to the cluster via gRPC seed nodes, with automatic retry
    on transient failures and failover to the next seed node when all
    retries are exhausted for the current node.

    Usage:
        async with KVClient(config) as client:
            await client.put("key", b"value")
            response = await client.get("key")
    """

    def __init__(self, config: ClientConfig) -> None:
        """Initialize the KVClient.

        Args:
            config: Client configuration with seed nodes, timeout, and retry settings.
        """
        self._config = config
        self._channel: Optional[grpc.aio.Channel] = None
        self._stub: Optional[kvstore_pb2_grpc.KVStoreServiceStub] = None
        self._current_node_index: int = 0
        self._connected: bool = False

    async def connect(self) -> None:
        """Establish a gRPC channel to a seed node.

        Tries each seed node in order until one connects successfully.

        Raises:
            ConnectionError: If no seed node can be reached.
        """
        if not self._config.seed_nodes:
            raise ConnectionError("No seed nodes configured")

        last_error: Optional[Exception] = None
        for i in range(len(self._config.seed_nodes)):
            node_index = (self._current_node_index + i) % len(self._config.seed_nodes)
            target = self._config.seed_nodes[node_index]
            try:
                channel = grpc.aio.insecure_channel(target)
                # Verify connectivity by waiting for the channel to be ready
                await channel.channel_ready()
                self._channel = channel
                self._stub = kvstore_pb2_grpc.KVStoreServiceStub(channel)
                self._current_node_index = node_index
                self._connected = True
                logger.info("Connected to seed node: %s", target)
                return
            except Exception as e:
                logger.warning("Failed to connect to %s: %s", target, e)
                last_error = e
                try:
                    await channel.close()
                except Exception:
                    pass

        raise ConnectionError(
            f"Could not connect to any seed node. Last error: {last_error}"
        )

    async def close(self) -> None:
        """Close the gRPC channel and release resources."""
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None
            self._connected = False
            logger.info("Client connection closed")

    async def __aenter__(self) -> "KVClient":
        """Enter the async context manager, establishing a connection."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit the async context manager, closing the connection."""
        await self.close()

    async def put(
        self,
        key: str,
        value: bytes,
        consistency: Optional[str] = None,
        vector_clock: Optional[VectorClock] = None,
    ) -> KVResponse:
        """Store a key-value pair in the cluster.

        Args:
            key: The key string (max 256 bytes).
            value: The value bytes (max 10 KB).
            consistency: Optional consistency level ("one", "quorum", "all").
            vector_clock: Optional vector clock for read-modify-write.

        Returns:
            KVResponse with success status and updated vector clock.

        Raises:
            ConnectionError: If not connected and failover exhausted.
        """
        request = kvstore_pb2.PutRequest(
            key=key,
            value=value,
            consistency_level=consistency or "",
        )
        if vector_clock is not None:
            request.vector_clock.CopyFrom(_vector_clock_to_proto(vector_clock))

        response = await self._execute_with_retry(
            lambda stub: stub.Put(request, timeout=self._config.timeout)
        )

        result_clock = _proto_to_vector_clock(response.vector_clock)
        return KVResponse(
            success=response.success,
            vector_clock=result_clock,
        )

    async def get(
        self,
        key: str,
        consistency: Optional[str] = None,
    ) -> KVResponse:
        """Retrieve the value for a key from the cluster.

        Args:
            key: The key to look up.
            consistency: Optional consistency level ("one", "quorum", "all").

        Returns:
            KVResponse with value, vector clock, and conflict information.

        Raises:
            ConnectionError: If not connected and failover exhausted.
        """
        request = kvstore_pb2.GetRequest(
            key=key,
            consistency_level=consistency or "",
        )

        response = await self._execute_with_retry(
            lambda stub: stub.Get(request, timeout=self._config.timeout)
        )

        if not response.found:
            return KVResponse(success=True, value=None, vector_clock=None)

        result_clock = _proto_to_vector_clock(response.vector_clock)

        conflicting_values: list[tuple[bytes, VectorClock]] = []
        if response.has_conflict:
            for cv in response.conflicting_values:
                cv_clock = _proto_to_vector_clock(cv.vector_clock)
                conflicting_values.append((cv.value, cv_clock))

        return KVResponse(
            success=True,
            value=response.value,
            vector_clock=result_clock,
            has_conflict=response.has_conflict,
            conflicting_values=conflicting_values,
        )

    async def delete(
        self,
        key: str,
        consistency: Optional[str] = None,
        vector_clock: Optional[VectorClock] = None,
    ) -> KVResponse:
        """Delete a key from the cluster using a tombstone marker.

        Args:
            key: The key to delete.
            consistency: Optional consistency level ("one", "quorum", "all").
            vector_clock: Optional vector clock for the delete operation.

        Returns:
            KVResponse with success status and updated vector clock.

        Raises:
            ConnectionError: If not connected and failover exhausted.
        """
        request = kvstore_pb2.DeleteRequest(
            key=key,
            consistency_level=consistency or "",
        )
        if vector_clock is not None:
            request.vector_clock.CopyFrom(_vector_clock_to_proto(vector_clock))

        response = await self._execute_with_retry(
            lambda stub: stub.Delete(request, timeout=self._config.timeout)
        )

        result_clock = _proto_to_vector_clock(response.vector_clock)
        return KVResponse(
            success=response.success,
            vector_clock=result_clock,
        )

    async def _execute_with_retry(self, rpc_call):
        """Execute an RPC call with retry and failover logic.

        Retries on UNAVAILABLE status up to retry_count times with retry_delay
        between attempts. If all retries fail for the current node, fails over
        to the next seed node.

        Args:
            rpc_call: A callable that takes a stub and returns a coroutine.

        Returns:
            The RPC response.

        Raises:
            ConnectionError: If all seed nodes are exhausted.
            grpc.RpcError: If a non-retryable error occurs.
        """
        if not self._connected or self._stub is None:
            raise ConnectionError("Client is not connected. Call connect() first.")

        nodes_tried = 0
        total_nodes = len(self._config.seed_nodes)

        while nodes_tried < total_nodes:
            # Retry loop for the current node
            for attempt in range(self._config.retry_count):
                try:
                    return await rpc_call(self._stub)
                except grpc.RpcError as e:
                    if hasattr(e, "code") and e.code() == grpc.StatusCode.UNAVAILABLE:
                        logger.warning(
                            "RPC unavailable (attempt %d/%d) on node %s: %s",
                            attempt + 1,
                            self._config.retry_count,
                            self._config.seed_nodes[self._current_node_index],
                            e,
                        )
                        if attempt < self._config.retry_count - 1:
                            await asyncio.sleep(self._config.retry_delay)
                    else:
                        # Non-retryable error, raise immediately
                        raise

            # All retries exhausted for current node, try failover
            nodes_tried += 1
            if nodes_tried < total_nodes:
                logger.info(
                    "Failing over from node %s to next seed node",
                    self._config.seed_nodes[self._current_node_index],
                )
                await self._failover_to_next_node()

        raise ConnectionError(
            "All seed nodes exhausted after retries. "
            f"Tried {total_nodes} node(s) with {self._config.retry_count} retries each."
        )

    async def _failover_to_next_node(self) -> None:
        """Failover to the next seed node in the list.

        Closes the current channel and connects to the next available seed node.
        """
        # Close current channel
        if self._channel is not None:
            try:
                await self._channel.close()
            except Exception:
                pass

        # Move to next node
        self._current_node_index = (
            (self._current_node_index + 1) % len(self._config.seed_nodes)
        )
        target = self._config.seed_nodes[self._current_node_index]

        try:
            channel = grpc.aio.insecure_channel(target)
            self._channel = channel
            self._stub = kvstore_pb2_grpc.KVStoreServiceStub(channel)
            logger.info("Failed over to node: %s", target)
        except Exception as e:
            logger.error("Failed to connect to failover node %s: %s", target, e)
            self._channel = None
            self._stub = None
            raise ConnectionError(f"Failover to {target} failed: {e}") from e
