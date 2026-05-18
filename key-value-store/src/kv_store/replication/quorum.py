"""Quorum logic for managing read and write consistency in the distributed store.

Implements tunable consistency levels (ONE, QUORUM, ALL) and manages concurrent
replica operations. The QuorumManager sends requests to all N replicas concurrently
and waits for the required number of acknowledgments (W for writes, R for reads).

Strong consistency is guaranteed when W + R > N, ensuring read and write quorums
always overlap.

Covers: FR-4.1, FR-4.2, FR-4.3, FR-4.4, FR-4.5, FR-4.6
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Optional

from kv_store.config import ReplicationConfig
from kv_store.replication.vector_clock import VectorClock


class ConsistencyLevel(Enum):
    """Predefined consistency levels for read/write operations."""

    ONE = "one"  # W=1, R=1 (eventual consistency)
    QUORUM = "quorum"  # W=⌊N/2⌋+1, R=⌊N/2⌋+1 (strong consistency for N=3)
    ALL = "all"  # W=N, R=N (strongest, lowest availability)


@dataclass
class QuorumConfig:
    """Quorum parameters for a single operation.

    Attributes:
        n: Total number of replicas.
        w: Number of write acknowledgments required.
        r: Number of read responses required.
    """

    n: int
    w: int
    r: int

    def is_strongly_consistent(self) -> bool:
        """Check if W + R > N (guarantees read/write quorum overlap).

        Returns:
            True if the configuration guarantees strong consistency.
        """
        return self.w + self.r > self.n

    @classmethod
    def from_consistency_level(
        cls, level: ConsistencyLevel, n: int = 3
    ) -> "QuorumConfig":
        """Create QuorumConfig from a named consistency level.

        Args:
            level: The desired consistency level.
            n: Total replica count.

        Returns:
            QuorumConfig with appropriate W and R values.
        """
        if level == ConsistencyLevel.ONE:
            return cls(n=n, w=1, r=1)
        elif level == ConsistencyLevel.QUORUM:
            quorum = n // 2 + 1
            return cls(n=n, w=quorum, r=quorum)
        elif level == ConsistencyLevel.ALL:
            return cls(n=n, w=n, r=n)
        else:
            raise ValueError(f"Unknown consistency level: {level}")


@dataclass
class QuorumResult:
    """Result of a quorum operation.

    Attributes:
        success: Whether the quorum was satisfied.
        responses_received: Number of successful responses collected.
        responses_required: Number of responses needed for quorum.
        failed_nodes: List of node IDs that failed to respond.
        values: For reads, collected (value, vector_clock) pairs from responding nodes.
    """

    success: bool
    responses_received: int
    responses_required: int
    failed_nodes: list[str] = field(default_factory=list)
    values: list[tuple[bytes, VectorClock]] = field(default_factory=list)


# Type aliases for the write and read callables
WriteCallable = Callable[
    [str, str, bytes, VectorClock], Coroutine[Any, Any, bool]
]  # (node_id, key, value, vector_clock) -> success
ReadCallable = Callable[
    [str, str], Coroutine[Any, Any, Optional[tuple[bytes, VectorClock]]]
]  # (node_id, key) -> (value, vector_clock) or None


class QuorumManager:
    """Manages quorum logic for read and write operations.

    Determines how many acknowledgments are needed, tracks responses,
    and decides when a quorum is satisfied or has failed.

    The actual network calls are delegated to write/read callables passed
    during construction, allowing the gRPC client to be injected later.
    """

    def __init__(
        self,
        config: ReplicationConfig,
        write_func: Optional[WriteCallable] = None,
        read_func: Optional[ReadCallable] = None,
    ) -> None:
        """Initialize with replication configuration and operation callbacks.

        Args:
            config: Replication config with N, W, R defaults.
            write_func: Async callable for writing to a replica node.
                        Signature: (node_id, key, value, vector_clock) -> bool
            read_func: Async callable for reading from a replica node.
                       Signature: (node_id, key) -> Optional[(value, vector_clock)]
        """
        self._config = config
        self._write_func = write_func
        self._read_func = read_func

    def get_quorum_config(
        self, consistency: Optional[ConsistencyLevel] = None
    ) -> QuorumConfig:
        """Get the quorum config, optionally overriding with a consistency level.

        If no consistency level is provided, uses the defaults from the
        ReplicationConfig.

        Args:
            consistency: Optional override for the default consistency.

        Returns:
            QuorumConfig to use for the operation.
        """
        if consistency is not None:
            return QuorumConfig.from_consistency_level(
                consistency, n=self._config.n_replicas
            )
        return QuorumConfig(
            n=self._config.n_replicas,
            w=self._config.w_quorum,
            r=self._config.r_quorum,
        )

    async def write_quorum(
        self,
        key: str,
        value: bytes,
        vector_clock: VectorClock,
        replica_nodes: list[str],
        quorum_config: QuorumConfig,
    ) -> QuorumResult:
        """Execute a write with quorum consensus.

        Sends write requests to all replica nodes concurrently using
        asyncio.gather with return_exceptions=True. Returns success once
        W acknowledgments are received.

        Args:
            key: The key being written.
            value: The value bytes.
            vector_clock: The vector clock for this write.
            replica_nodes: List of node IDs to replicate to.
            quorum_config: Quorum parameters for this operation.

        Returns:
            QuorumResult indicating success/failure and which nodes failed.

        Raises:
            RuntimeError: If no write function has been configured.
        """
        if self._write_func is None:
            raise RuntimeError("No write function configured for QuorumManager")

        # Send write requests to all replicas concurrently
        tasks = [
            self._write_func(node_id, key, value, vector_clock)
            for node_id in replica_nodes
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Count successes and track failures
        success_count = 0
        failed_nodes: list[str] = []

        for node_id, result in zip(replica_nodes, results):
            if isinstance(result, Exception):
                failed_nodes.append(node_id)
            elif result is True:
                success_count += 1
            else:
                failed_nodes.append(node_id)

        return QuorumResult(
            success=success_count >= quorum_config.w,
            responses_received=success_count,
            responses_required=quorum_config.w,
            failed_nodes=failed_nodes,
        )

    async def read_quorum(
        self,
        key: str,
        replica_nodes: list[str],
        quorum_config: QuorumConfig,
    ) -> QuorumResult:
        """Execute a read with quorum consensus.

        Queries all replica nodes concurrently and collects responses.
        Returns success once R responses are received, along with the
        collected values for conflict resolution upstream.

        Args:
            key: The key to read.
            replica_nodes: List of node IDs to query.
            quorum_config: Quorum parameters for this operation.

        Returns:
            QuorumResult with collected values for conflict resolution.

        Raises:
            RuntimeError: If no read function has been configured.
        """
        if self._read_func is None:
            raise RuntimeError("No read function configured for QuorumManager")

        # Send read requests to all replicas concurrently
        tasks = [self._read_func(node_id, key) for node_id in replica_nodes]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect successful responses and track failures
        success_count = 0
        failed_nodes: list[str] = []
        values: list[tuple[bytes, VectorClock]] = []

        for node_id, result in zip(replica_nodes, results):
            if isinstance(result, Exception):
                failed_nodes.append(node_id)
            elif result is not None:
                success_count += 1
                values.append(result)
            else:
                # None means key not found on this node, still counts as a response
                success_count += 1

        return QuorumResult(
            success=success_count >= quorum_config.r,
            responses_received=success_count,
            responses_required=quorum_config.r,
            failed_nodes=failed_nodes,
            values=values,
        )
