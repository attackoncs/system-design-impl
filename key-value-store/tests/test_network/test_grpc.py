"""Tests for the gRPC network layer.

Tests cover:
- gRPC server starts and stops cleanly
- Put/Get/Delete RPCs work end-to-end (in-process)
- Client handles unavailable node gracefully
- Connection pooling reuses channels
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import grpc
import grpc.aio
import pytest

from kv_store.network.grpc_server import GRPCServer, KVStoreServicer
from kv_store.network.grpc_client import GRPCClient, NodeUnavailableError
from kv_store.network import kvstore_pb2, kvstore_pb2_grpc
from kv_store.replication.vector_clock import VectorClock


# --- Mock Components ---


class MockStorageEngine:
    """Mock storage engine for testing."""

    def __init__(self):
        self._data: dict[str, tuple[bytes, float, bool]] = {}

    async def put(self, key: str, value: bytes, timestamp: float) -> None:
        self._data[key] = (value, timestamp, False)

    async def get(self, key: str) -> Optional[object]:
        if key not in self._data:
            return None
        value, timestamp, is_tombstone = self._data[key]
        return MockStorageResult(
            key=key,
            value=value,
            timestamp=timestamp,
            is_tombstone=is_tombstone,
            found=True,
        )

    async def delete(self, key: str, timestamp: float) -> None:
        self._data[key] = (b"", timestamp, True)


@dataclass
class MockStorageResult:
    key: str
    value: Optional[bytes]
    timestamp: float
    is_tombstone: bool
    found: bool


class MockCoordinator:
    """Mock request coordinator for testing."""

    def __init__(self):
        self._data: dict[str, tuple[bytes, VectorClock]] = {}

    async def put(
        self,
        key: str,
        value: bytes,
        client_clock=None,
        consistency=None,
    ):
        clock = client_clock if client_clock else VectorClock()
        new_clock = clock.increment("test-node")
        self._data[key] = (value, new_clock)
        return MockPutResult(success=True, vector_clock=new_clock, replicas_acknowledged=3)

    async def get(self, key: str, consistency=None):
        if key in self._data:
            value, clock = self._data[key]
            return MockGetResult(
                found=True,
                value=value,
                vector_clock=clock,
                has_conflict=False,
                conflicting_values=[],
            )
        return MockGetResult(
            found=False,
            value=None,
            vector_clock=None,
            has_conflict=False,
            conflicting_values=[],
        )

    async def delete(self, key: str, client_clock=None, consistency=None):
        clock = client_clock if client_clock else VectorClock()
        new_clock = clock.increment("test-node")
        if key in self._data:
            del self._data[key]
        return MockDeleteResult(success=True, vector_clock=new_clock)


@dataclass
class MockPutResult:
    success: bool
    vector_clock: VectorClock
    replicas_acknowledged: int


@dataclass
class MockGetResult:
    found: bool
    value: Optional[bytes]
    vector_clock: Optional[VectorClock]
    has_conflict: bool
    conflicting_values: list = field(default_factory=list)


@dataclass
class MockDeleteResult:
    success: bool
    vector_clock: VectorClock


class MockGossipProtocol:
    """Mock gossip protocol for testing."""

    def __init__(self):
        self._members = {}

    @property
    def members(self):
        return self._members

    def merge_membership(self, remote_members):
        for member in remote_members:
            self._members[member.node_id] = member


class MockMerkleTree:
    """Mock Merkle tree for testing."""

    def __init__(self):
        self._bucket_hashes = {0: "hash0", 1: "hash1"}

    def compare(self, other_bucket_hashes):
        diffs = []
        for bucket_id, local_hash in self._bucket_hashes.items():
            remote_hash = other_bucket_hashes.get(bucket_id, "")
            if local_hash != remote_hash:
                diffs.append(MockSyncDiff(
                    bucket_id=bucket_id,
                    local_hash=local_hash,
                    remote_hash=remote_hash,
                ))
        return diffs

    def get_keys_in_bucket(self, bucket_id):
        return [f"key-in-bucket-{bucket_id}"]


@dataclass
class MockSyncDiff:
    bucket_id: int
    local_hash: str
    remote_hash: str


# --- Fixtures ---


@pytest.fixture
def mock_coordinator():
    return MockCoordinator()


@pytest.fixture
def mock_storage():
    return MockStorageEngine()


@pytest.fixture
def mock_gossip():
    return MockGossipProtocol()


@pytest.fixture
def mock_merkle():
    return MockMerkleTree()


@pytest.fixture
async def grpc_server(mock_coordinator, mock_storage, mock_gossip, mock_merkle):
    """Create and start a gRPC server on a random port for testing."""
    server = GRPCServer(
        coordinator=mock_coordinator,
        storage_engine=mock_storage,
        gossip_protocol=mock_gossip,
        merkle_tree=mock_merkle,
        host="127.0.0.1",
        port=0,  # Use port 0 to let OS assign a free port
    )
    # We need to manually set up the server to get the actual port
    options = [
        ("grpc.max_send_message_length", server._max_message_size),
        ("grpc.max_receive_message_length", server._max_message_size),
    ]
    aio_server = grpc.aio.server(options=options)
    servicer = KVStoreServicer(
        coordinator=mock_coordinator,
        storage_engine=mock_storage,
        gossip_protocol=mock_gossip,
        merkle_tree=mock_merkle,
    )
    kvstore_pb2_grpc.add_KVStoreServiceServicer_to_server(servicer, aio_server)
    port = aio_server.add_insecure_port("127.0.0.1:0")
    await aio_server.start()

    yield aio_server, port

    await aio_server.stop(grace=0)


# --- Test: Server starts and stops cleanly ---


class TestGRPCServerLifecycle:
    """Tests for gRPC server start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_server_starts_and_stops(
        self, mock_coordinator, mock_storage, mock_gossip, mock_merkle
    ):
        """Server should start and stop without errors."""
        server = GRPCServer(
            coordinator=mock_coordinator,
            storage_engine=mock_storage,
            gossip_protocol=mock_gossip,
            merkle_tree=mock_merkle,
            host="127.0.0.1",
            port=0,
        )
        # Manually create server to test lifecycle
        server._server = grpc.aio.server()
        server._server.add_insecure_port("127.0.0.1:0")
        await server._server.start()
        await server.stop(grace=0)
        assert server._server is None

    @pytest.mark.asyncio
    async def test_server_stop_when_not_started(
        self, mock_coordinator, mock_storage, mock_gossip, mock_merkle
    ):
        """Stopping a server that hasn't started should not raise."""
        server = GRPCServer(
            coordinator=mock_coordinator,
            storage_engine=mock_storage,
            gossip_protocol=mock_gossip,
            merkle_tree=mock_merkle,
            host="127.0.0.1",
            port=50099,
        )
        # Should not raise
        await server.stop()


# --- Test: Put/Get/Delete RPCs end-to-end ---


class TestGRPCEndToEnd:
    """Tests for Put/Get/Delete RPCs working end-to-end via in-process server."""

    @pytest.mark.asyncio
    async def test_put_and_get(self, grpc_server):
        """Put a value and then Get it back via gRPC."""
        server, port = grpc_server
        target = f"127.0.0.1:{port}"

        client = GRPCClient(timeout=5.0)
        try:
            clock = VectorClock()

            # Put
            success = await client.put(target, "test-key", b"test-value", clock)
            assert success is True

            # Get
            result = await client.get(target, "test-key")
            assert result is not None
            value, returned_clock = result
            assert value == b"test-value"
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_get_nonexistent_key(self, grpc_server):
        """Get a key that doesn't exist should return None."""
        server, port = grpc_server
        target = f"127.0.0.1:{port}"

        client = GRPCClient(timeout=5.0)
        try:
            result = await client.get(target, "nonexistent-key")
            assert result is None
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_put_get_delete(self, grpc_server):
        """Full lifecycle: put, get, delete via gRPC."""
        server, port = grpc_server
        target = f"127.0.0.1:{port}"

        client = GRPCClient(timeout=5.0)
        try:
            clock = VectorClock()

            # Put
            success = await client.put(target, "del-key", b"del-value", clock)
            assert success is True

            # Get - should find it
            result = await client.get(target, "del-key")
            assert result is not None

            # Delete
            # Use the stub directly for delete since our client doesn't expose delete
            stub = kvstore_pb2_grpc.KVStoreServiceStub(
                client._get_channel(target)
            )
            del_response = await stub.Delete(
                kvstore_pb2.DeleteRequest(key="del-key"),
                timeout=5.0,
            )
            assert del_response.success is True
        finally:
            await client.close()


# --- Test: Client handles unavailable node ---


class TestGRPCClientUnavailable:
    """Tests for client handling of unavailable nodes."""

    @pytest.mark.asyncio
    async def test_put_to_unavailable_node(self):
        """Put to an unavailable node should raise NodeUnavailableError."""
        client = GRPCClient(timeout=1.0)
        try:
            with pytest.raises(NodeUnavailableError) as exc_info:
                await client.put(
                    "127.0.0.1:59999",  # No server running here
                    "key",
                    b"value",
                    VectorClock(),
                )
            assert "127.0.0.1:59999" in str(exc_info.value)
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_get_from_unavailable_node(self):
        """Get from an unavailable node should raise NodeUnavailableError."""
        client = GRPCClient(timeout=1.0)
        try:
            with pytest.raises(NodeUnavailableError):
                await client.get("127.0.0.1:59999", "key")
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_replicate_to_unavailable_node(self):
        """Replicate to an unavailable node should raise NodeUnavailableError."""
        client = GRPCClient(timeout=1.0)
        try:
            with pytest.raises(NodeUnavailableError):
                await client.replicate(
                    "127.0.0.1:59999",
                    "key",
                    b"value",
                    VectorClock(),
                    timestamp=time.time(),
                )
        finally:
            await client.close()


# --- Test: Connection pooling ---


class TestConnectionPooling:
    """Tests for connection pooling behavior."""

    @pytest.mark.asyncio
    async def test_reuses_channel_for_same_target(self):
        """Multiple calls to the same target should reuse the same channel."""
        client = GRPCClient()
        try:
            target = "127.0.0.1:50051"

            # Get channel twice
            channel1 = client._get_channel(target)
            channel2 = client._get_channel(target)

            assert channel1 is channel2
            assert client.active_channels == 1
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_different_targets_get_different_channels(self):
        """Different targets should get different channels."""
        client = GRPCClient()
        try:
            channel1 = client._get_channel("127.0.0.1:50051")
            channel2 = client._get_channel("127.0.0.1:50052")

            assert channel1 is not channel2
            assert client.active_channels == 2
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_close_clears_all_channels(self):
        """Closing the client should clear all channels."""
        client = GRPCClient()

        # Create some channels
        client._get_channel("127.0.0.1:50051")
        client._get_channel("127.0.0.1:50052")
        assert client.active_channels == 2

        await client.close()
        assert client.active_channels == 0


# --- Test: Replicate RPC ---


class TestReplicateRPC:
    """Tests for the Replicate RPC."""

    @pytest.mark.asyncio
    async def test_replicate_writes_to_storage(self, grpc_server, mock_storage):
        """Replicate RPC should write directly to local storage."""
        server, port = grpc_server
        target = f"127.0.0.1:{port}"

        client = GRPCClient(timeout=5.0)
        try:
            clock = VectorClock({"node-1": 1})
            success = await client.replicate(
                target,
                key="replicated-key",
                value=b"replicated-value",
                vector_clock=clock,
                timestamp=1000.0,
                is_tombstone=False,
            )
            assert success is True
            # Verify it was written to storage
            assert "replicated-key" in mock_storage._data
            value, ts, is_tomb = mock_storage._data["replicated-key"]
            assert value == b"replicated-value"
            assert ts == 1000.0
            assert is_tomb is False
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_replicate_tombstone(self, grpc_server, mock_storage):
        """Replicate RPC with is_tombstone=True should delete from storage."""
        server, port = grpc_server
        target = f"127.0.0.1:{port}"

        client = GRPCClient(timeout=5.0)
        try:
            clock = VectorClock({"node-1": 1})
            success = await client.replicate(
                target,
                key="tomb-key",
                value=b"",
                vector_clock=clock,
                timestamp=2000.0,
                is_tombstone=True,
            )
            assert success is True
            # Verify tombstone was written
            assert "tomb-key" in mock_storage._data
            _, ts, is_tomb = mock_storage._data["tomb-key"]
            assert ts == 2000.0
            assert is_tomb is True
        finally:
            await client.close()
