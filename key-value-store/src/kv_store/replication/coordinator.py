"""Request Coordinator for coordinating client requests across replica nodes.

The coordinator is responsible for:
1. Determining replica nodes via consistent hash ring
2. Forwarding requests to replicas via gRPC
3. Collecting responses and applying quorum logic
4. Resolving conflicts using vector clocks
5. Falling back to sloppy quorum if nodes are unavailable
6. Triggering hinted handoff for failed nodes

Any node can act as coordinator for any client request (decentralized).

Covers: FR-1, FR-2, FR-3, FR-4, FR-5, FR-7, FR-12.2
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

from kv_store.replication.vector_clock import VectorClock
from kv_store.replication.quorum import (
    ConsistencyLevel,
    QuorumConfig,
    QuorumManager,
    QuorumResult,
)


# --- Custom Exceptions ---


class QuorumNotMetError(Exception):
    """Raised when a quorum cannot be satisfied for an operation.

    Attributes:
        required: Number of acknowledgments required.
        received: Number of acknowledgments actually received.
        failed_nodes: List of node IDs that failed to respond.
    """

    def __init__(
        self,
        required: int,
        received: int,
        failed_nodes: Optional[list[str]] = None,
    ) -> None:
        self.required = required
        self.received = received
        self.failed_nodes = failed_nodes or []
        super().__init__(
            f"Quorum not met: required {required}, received {received}. "
            f"Failed nodes: {self.failed_nodes}"
        )


# --- Result Dataclasses ---


@dataclass
class PutResult:
    """Result of a put operation.

    Attributes:
        success: Whether the write quorum was satisfied.
        vector_clock: The updated vector clock after the write.
        replicas_acknowledged: Number of replicas that acknowledged the write.
    """

    success: bool
    vector_clock: VectorClock
    replicas_acknowledged: int


@dataclass
class GetResult:
    """Result of a get operation.

    Attributes:
        found: Whether the key was found on any replica.
        value: The resolved value (if no conflict and found).
        vector_clock: The vector clock of the resolved value.
        has_conflict: Whether concurrent versions were detected.
        conflicting_values: All conflicting (value, vector_clock) pairs if conflict.
    """

    found: bool
    value: Optional[bytes] = None
    vector_clock: Optional[VectorClock] = None
    has_conflict: bool = False
    conflicting_values: list[tuple[bytes, VectorClock]] = field(default_factory=list)


@dataclass
class DeleteResult:
    """Result of a delete operation.

    Attributes:
        success: Whether the delete quorum was satisfied.
        vector_clock: The updated vector clock after the tombstone write.
    """

    success: bool
    vector_clock: VectorClock


# --- Protocol Interfaces ---


@runtime_checkable
class HashRingProtocol(Protocol):
    """Protocol for the consistent hash ring."""

    def get_nodes(self, key: str, count: int) -> list[str]:
        """Get the N replica nodes for a key."""
        ...


@runtime_checkable
class StorageEngineProtocol(Protocol):
    """Protocol for the local storage engine."""

    async def put(self, key: str, value: bytes, timestamp: float) -> None:
        """Write a key-value pair locally."""
        ...

    async def get(self, key: str) -> Optional[object]:
        """Read a key from local storage."""
        ...

    async def delete(self, key: str, timestamp: float) -> None:
        """Delete a key locally (tombstone)."""
        ...


@runtime_checkable
class MembershipProtocol(Protocol):
    """Protocol for cluster membership."""

    def is_node_alive(self, node_id: str) -> bool:
        """Check if a node is alive."""
        ...

    def get_alive_members(self) -> list[str]:
        """Get all alive member node IDs."""
        ...


@runtime_checkable
class GRPCClientProtocol(Protocol):
    """Protocol for the gRPC client used for inter-node communication."""

    async def put(
        self, target: str, key: str, value: bytes, vector_clock: VectorClock
    ) -> bool:
        """Send a put request to a target node. Returns True on success."""
        ...

    async def get(
        self, target: str, key: str
    ) -> Optional[tuple[bytes, VectorClock]]:
        """Send a get request to a target node. Returns (value, clock) or None."""
        ...


@runtime_checkable
class HintedHandoffProtocol(Protocol):
    """Protocol for the hinted handoff manager."""

    def store_hint(
        self, target_node_id: str, key: str, value: bytes, vector_clock: VectorClock
    ) -> None:
        """Store a hint for a failed node."""
        ...


# --- Request Coordinator ---


class RequestCoordinator:
    """Coordinates client requests across replica nodes.

    Any node can act as coordinator. The coordinator:
    1. Determines replica nodes via consistent hash ring
    2. Forwards requests to replicas via gRPC
    3. Collects responses and applies quorum logic
    4. Resolves conflicts using vector clocks
    5. Falls back to sloppy quorum if nodes are unavailable

    Covers: FR-1, FR-2, FR-3, FR-4, FR-5, FR-7, FR-12.2
    """

    def __init__(
        self,
        node_id: str,
        hash_ring: HashRingProtocol,
        quorum_manager: QuorumManager,
        grpc_client: GRPCClientProtocol,
        storage_engine: StorageEngineProtocol,
        membership: MembershipProtocol,
        hinted_handoff: HintedHandoffProtocol,
    ) -> None:
        """Initialize the coordinator.

        Args:
            node_id: This node's identifier.
            hash_ring: The consistent hash ring for partitioning.
            quorum_manager: Quorum logic manager.
            grpc_client: gRPC client for inter-node communication.
            storage_engine: Local storage engine.
            membership: Cluster membership state.
            hinted_handoff: Hinted handoff manager for failed nodes.
        """
        self._node_id = node_id
        self._hash_ring = hash_ring
        self._quorum_manager = quorum_manager
        self._grpc_client = grpc_client
        self._storage_engine = storage_engine
        self._membership = membership
        self._hinted_handoff = hinted_handoff

    async def put(
        self,
        key: str,
        value: bytes,
        client_clock: Optional[VectorClock] = None,
        consistency: Optional[ConsistencyLevel] = None,
    ) -> PutResult:
        """Coordinate a put operation across replicas.

        1. Determine N replica nodes from hash ring (with sloppy quorum)
        2. Increment vector clock for this coordinator node
        3. Write locally if this node is a replica
        4. Forward to other replicas via gRPC
        5. If a replica write fails, store a hint for later delivery
        6. Wait for W acknowledgments

        Args:
            key: Key to write (max 256 bytes).
            value: Value to write (max 10 KB).
            client_clock: Vector clock from client (for read-modify-write).
            consistency: Optional consistency level override.

        Returns:
            PutResult with success status and updated vector clock.

        Raises:
            QuorumNotMetError: If fewer than W nodes acknowledge.
            ValueError: If key/value exceeds size limits.
        """
        # Validate key/value sizes
        self._validate_key_value(key, value)

        # Get quorum configuration
        quorum_config = self._quorum_manager.get_quorum_config(consistency)

        # Determine replica nodes (with sloppy quorum substitution)
        replica_nodes = self._get_replica_nodes(key)

        # Increment vector clock
        clock = client_clock if client_clock is not None else VectorClock()
        new_clock = clock.increment(self._node_id)

        # Write to replicas concurrently
        timestamp = time.time()
        ack_count = 0
        failed_nodes: list[str] = []

        async def write_to_node(target_node_id: str) -> bool:
            """Write to a single replica node."""
            if target_node_id == self._node_id:
                # Write locally
                try:
                    await self._storage_engine.put(key, value, timestamp)
                    return True
                except Exception:
                    return False
            else:
                # Forward via gRPC
                try:
                    result = await self._grpc_client.put(
                        target_node_id, key, value, new_clock
                    )
                    return result
                except Exception:
                    return False

        # Execute writes concurrently
        tasks = [write_to_node(node_id) for node_id in replica_nodes]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for node_id, result in zip(replica_nodes, results):
            if isinstance(result, Exception) or result is False:
                failed_nodes.append(node_id)
                # Store hint for failed node
                self._hinted_handoff.store_hint(node_id, key, value, new_clock)
            else:
                ack_count += 1

        # Check if quorum was met
        if ack_count < quorum_config.w:
            raise QuorumNotMetError(
                required=quorum_config.w,
                received=ack_count,
                failed_nodes=failed_nodes,
            )

        return PutResult(
            success=True,
            vector_clock=new_clock,
            replicas_acknowledged=ack_count,
        )

    async def get(
        self,
        key: str,
        consistency: Optional[ConsistencyLevel] = None,
    ) -> GetResult:
        """Coordinate a get operation across replicas.

        1. Determine N replica nodes from hash ring
        2. Query R replicas concurrently
        3. Compare vector clocks from responses
        4. If one version dominates, return it
        5. If conflict (concurrent versions), return all to client

        Args:
            key: Key to read.
            consistency: Optional consistency level override.

        Returns:
            GetResult with value(s) and conflict information.

        Raises:
            QuorumNotMetError: If fewer than R nodes respond.
        """
        # Get quorum configuration
        quorum_config = self._quorum_manager.get_quorum_config(consistency)

        # Determine replica nodes
        replica_nodes = self._get_replica_nodes(key)

        # Sentinel to distinguish "not found" from "error"
        _READ_ERROR = object()

        # Read from replicas concurrently
        async def read_from_node(
            target_node_id: str,
        ) -> object:
            """Read from a single replica node.

            Returns:
                (bytes, VectorClock) if found,
                None if key not found (valid response),
                _READ_ERROR sentinel if the read failed.
            """
            if target_node_id == self._node_id:
                # Read locally
                try:
                    result = await self._storage_engine.get(key)
                    if result is None:
                        return None
                    # StorageResult has .value, .found, .is_tombstone attributes
                    if not getattr(result, "found", True):
                        return None
                    if getattr(result, "is_tombstone", False):
                        return None
                    value = getattr(result, "value", None)
                    if value is None:
                        return None
                    # Local storage doesn't track vector clocks directly,
                    # return with an empty clock (coordinator manages clocks)
                    return (value, VectorClock())
                except Exception:
                    return _READ_ERROR
            else:
                # Query via gRPC
                try:
                    return await self._grpc_client.get(target_node_id, key)
                except Exception:
                    return _READ_ERROR

        tasks = [read_from_node(node_id) for node_id in replica_nodes]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect successful responses
        response_count = 0
        failed_nodes: list[str] = []
        values: list[tuple[bytes, VectorClock]] = []

        for node_id, result in zip(replica_nodes, results):
            if isinstance(result, Exception) or result is _READ_ERROR:
                failed_nodes.append(node_id)
            elif result is not None:
                response_count += 1
                values.append(result)  # type: ignore[arg-type]
            else:
                # None means key not found, still counts as a response
                response_count += 1

        # Check if read quorum was met
        if response_count < quorum_config.r:
            raise QuorumNotMetError(
                required=quorum_config.r,
                received=response_count,
                failed_nodes=failed_nodes,
            )

        # No values found
        if not values:
            return GetResult(found=False)

        # Resolve conflicts using vector clocks
        return self._resolve_read_conflicts(values)

    async def delete(
        self,
        key: str,
        client_clock: Optional[VectorClock] = None,
        consistency: Optional[ConsistencyLevel] = None,
    ) -> DeleteResult:
        """Coordinate a delete operation (tombstone write).

        Follows the same flow as put() but writes a tombstone marker
        (empty value indicating deletion).

        Args:
            key: Key to delete.
            client_clock: Vector clock from client.
            consistency: Optional consistency level override.

        Returns:
            DeleteResult with success status.

        Raises:
            QuorumNotMetError: If fewer than W nodes acknowledge.
        """
        # Get quorum configuration
        quorum_config = self._quorum_manager.get_quorum_config(consistency)

        # Determine replica nodes (with sloppy quorum substitution)
        replica_nodes = self._get_replica_nodes(key)

        # Increment vector clock
        clock = client_clock if client_clock is not None else VectorClock()
        new_clock = clock.increment(self._node_id)

        # Write tombstone to replicas concurrently
        timestamp = time.time()
        ack_count = 0
        failed_nodes: list[str] = []

        async def delete_on_node(target_node_id: str) -> bool:
            """Write tombstone to a single replica node."""
            if target_node_id == self._node_id:
                # Delete locally (writes tombstone)
                try:
                    await self._storage_engine.delete(key, timestamp)
                    return True
                except Exception:
                    return False
            else:
                # Forward tombstone via gRPC (put with empty value)
                try:
                    result = await self._grpc_client.put(
                        target_node_id, key, b"", new_clock
                    )
                    return result
                except Exception:
                    return False

        tasks = [delete_on_node(node_id) for node_id in replica_nodes]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for node_id, result in zip(replica_nodes, results):
            if isinstance(result, Exception) or result is False:
                failed_nodes.append(node_id)
                # Store hint for failed node
                self._hinted_handoff.store_hint(node_id, key, b"", new_clock)
            else:
                ack_count += 1

        # Check if quorum was met
        if ack_count < quorum_config.w:
            raise QuorumNotMetError(
                required=quorum_config.w,
                received=ack_count,
                failed_nodes=failed_nodes,
            )

        return DeleteResult(
            success=True,
            vector_clock=new_clock,
        )

    def _get_replica_nodes(self, key: str) -> list[str]:
        """Get N replica nodes for a key, substituting unavailable nodes.

        Uses hash_ring.get_nodes() for primary replicas, then applies
        sloppy quorum by finding next healthy nodes if any are down.

        Args:
            key: The key to partition.

        Returns:
            List of node IDs (may include substitute nodes for sloppy quorum).
        """
        quorum_config = self._quorum_manager.get_quorum_config()
        n = quorum_config.n

        # Get primary replica nodes from the hash ring
        primary_nodes = self._hash_ring.get_nodes(key, n)

        # Apply sloppy quorum: substitute unavailable nodes
        result_nodes: list[str] = []
        for node_id in primary_nodes:
            if self._membership.is_node_alive(node_id):
                result_nodes.append(node_id)
            else:
                # Find a substitute from alive members not already in the list
                substitute = self._find_substitute_node(
                    key, result_nodes + primary_nodes
                )
                if substitute:
                    result_nodes.append(substitute)
                else:
                    # No substitute available, still include the original
                    # (the write will fail for this node and trigger hinted handoff)
                    result_nodes.append(node_id)

        return result_nodes

    def _find_substitute_node(
        self, key: str, excluded_nodes: list[str]
    ) -> Optional[str]:
        """Find a healthy substitute node not in the excluded list.

        Tries to get additional nodes from the hash ring beyond N,
        or falls back to any alive member.

        Args:
            key: The key being operated on.
            excluded_nodes: Nodes to exclude from selection.

        Returns:
            A substitute node ID, or None if no substitute is available.
        """
        # Try to get more nodes from the ring
        alive_members = self._membership.get_alive_members()
        for member in alive_members:
            if member not in excluded_nodes:
                return member
        return None

    def _resolve_read_conflicts(
        self, values: list[tuple[bytes, VectorClock]]
    ) -> GetResult:
        """Resolve conflicts among collected read values using vector clocks.

        If one version dominates all others, return it as the resolved value.
        If there are concurrent versions (conflicts), return all of them.

        Args:
            values: List of (value, vector_clock) pairs from replicas.

        Returns:
            GetResult with resolved value or conflict information.
        """
        if len(values) == 1:
            value, clock = values[0]
            return GetResult(
                found=True,
                value=value,
                vector_clock=clock,
                has_conflict=False,
            )

        # Find the dominating version(s)
        # A version dominates if it dominates all other versions
        non_dominated: list[tuple[bytes, VectorClock]] = []

        for i, (val_i, clock_i) in enumerate(values):
            is_dominated = False
            for j, (val_j, clock_j) in enumerate(values):
                if i != j and clock_j.dominates(clock_i):
                    is_dominated = True
                    break
            if not is_dominated:
                non_dominated.append((val_i, clock_i))

        # Deduplicate: remove entries with identical clocks and values
        unique: list[tuple[bytes, VectorClock]] = []
        seen_clocks: list[VectorClock] = []
        for val, clock in non_dominated:
            is_dup = False
            for seen_clock in seen_clocks:
                if clock == seen_clock:
                    is_dup = True
                    break
            if not is_dup:
                unique.append((val, clock))
                seen_clocks.append(clock)

        if len(unique) == 1:
            value, clock = unique[0]
            return GetResult(
                found=True,
                value=value,
                vector_clock=clock,
                has_conflict=False,
            )

        # Multiple non-dominated versions = conflict
        return GetResult(
            found=True,
            value=None,
            vector_clock=None,
            has_conflict=True,
            conflicting_values=unique,
        )

    @staticmethod
    def _validate_key_value(key: str, value: bytes) -> None:
        """Validate key and value size constraints.

        Args:
            key: The key to validate.
            value: The value to validate.

        Raises:
            ValueError: If key exceeds 256 bytes or value exceeds 10 KB.
        """
        key_bytes = key.encode("utf-8") if isinstance(key, str) else key
        if len(key_bytes) > 256:
            raise ValueError(
                f"Key exceeds maximum size of 256 bytes: {len(key_bytes)} bytes"
            )
        if len(value) > 10 * 1024:
            raise ValueError(
                f"Value exceeds maximum size of 10 KB: {len(value)} bytes"
            )
