"""Tests for the KVClient API.

Tests cover:
- put/get/delete roundtrip via gRPC (using in-process mock server)
- Retry on transient failure (UNAVAILABLE status)
- Failover to next seed node
- Context manager opens and closes connection
- Timeout handling
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import grpc
import grpc.aio
import pytest

from kv_store.client import ClientConfig, KVClient, KVResponse
from kv_store.network import kvstore_pb2, kvstore_pb2_grpc
from kv_store.replication.vector_clock import VectorClock


# ---------------------------------------------------------------------------
# Helpers: In-process gRPC server with a mock servicer
# ---------------------------------------------------------------------------


class MockKVStoreServicer(kvstore_pb2_grpc.KVStoreServiceServicer):
    """A simple in-process mock servicer for testing the client."""

    def __init__(self):
        self.store: dict[str, tuple[bytes, kvstore_pb2.VectorClockProto]] = {}
        self.put_count = 0
        self.get_count = 0
        self.delete_count = 0

    async def Put(self, request, context):
        self.put_count += 1
        clock = kvstore_pb2.VectorClockProto()
        clock.entries.append(
            kvstore_pb2.VectorClockEntry(node_id="node-1", counter=1)
        )
        self.store[request.key] = (request.value, clock)
        return kvstore_pb2.PutResponse(success=True, vector_clock=clock)

    async def Get(self, request, context):
        self.get_count += 1
        if request.key in self.store:
            value, clock = self.store[request.key]
            return kvstore_pb2.GetResponse(
                found=True,
                value=value,
                vector_clock=clock,
                has_conflict=False,
            )
        return kvstore_pb2.GetResponse(found=False)

    async def Delete(self, request, context):
        self.delete_count += 1
        clock = kvstore_pb2.VectorClockProto()
        clock.entries.append(
            kvstore_pb2.VectorClockEntry(node_id="node-1", counter=2)
        )
        if request.key in self.store:
            del self.store[request.key]
        return kvstore_pb2.DeleteResponse(success=True, vector_clock=clock)


class ConflictingServicer(kvstore_pb2_grpc.KVStoreServiceServicer):
    """Servicer that returns conflicting values on Get."""

    async def Put(self, request, context):
        clock = kvstore_pb2.VectorClockProto()
        clock.entries.append(
            kvstore_pb2.VectorClockEntry(node_id="node-1", counter=1)
        )
        return kvstore_pb2.PutResponse(success=True, vector_clock=clock)

    async def Get(self, request, context):
        clock1 = kvstore_pb2.VectorClockProto()
        clock1.entries.append(
            kvstore_pb2.VectorClockEntry(node_id="node-1", counter=1)
        )
        clock2 = kvstore_pb2.VectorClockProto()
        clock2.entries.append(
            kvstore_pb2.VectorClockEntry(node_id="node-2", counter=1)
        )
        return kvstore_pb2.GetResponse(
            found=True,
            value=b"value1",
            vector_clock=clock1,
            has_conflict=True,
            conflicting_values=[
                kvstore_pb2.ConflictingValue(value=b"value1", vector_clock=clock1),
                kvstore_pb2.ConflictingValue(value=b"value2", vector_clock=clock2),
            ],
        )


@pytest.fixture
async def grpc_server():
    """Start an in-process async gRPC server with the mock servicer."""
    servicer = MockKVStoreServicer()
    server = grpc.aio.server()
    kvstore_pb2_grpc.add_KVStoreServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("[::]:0")
    await server.start()
    yield server, port, servicer
    await server.stop(grace=0)


@pytest.fixture
async def conflict_server():
    """Start an in-process async gRPC server that returns conflicts."""
    servicer = ConflictingServicer()
    server = grpc.aio.server()
    kvstore_pb2_grpc.add_KVStoreServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("[::]:0")
    await server.start()
    yield server, port, servicer
    await server.stop(grace=0)


# ---------------------------------------------------------------------------
# Test: put/get/delete roundtrip via gRPC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_get_delete_roundtrip(grpc_server):
    """Test basic put, get, and delete operations via an in-process gRPC server."""
    server, port, servicer = grpc_server
    config = ClientConfig(
        seed_nodes=[f"localhost:{port}"],
        timeout=5.0,
        retry_count=1,
        retry_delay=0.1,
    )

    async with KVClient(config) as client:
        # Put
        put_resp = await client.put("test-key", b"test-value")
        assert put_resp.success is True
        assert put_resp.vector_clock is not None

        # Get
        get_resp = await client.get("test-key")
        assert get_resp.success is True
        assert get_resp.value == b"test-value"
        assert get_resp.vector_clock is not None
        assert get_resp.has_conflict is False

        # Delete
        del_resp = await client.delete("test-key")
        assert del_resp.success is True
        assert del_resp.vector_clock is not None

        # Get after delete
        get_resp2 = await client.get("test-key")
        assert get_resp2.value is None


@pytest.mark.asyncio
async def test_get_nonexistent_key(grpc_server):
    """Test that getting a non-existent key returns None value."""
    server, port, servicer = grpc_server
    config = ClientConfig(
        seed_nodes=[f"localhost:{port}"],
        timeout=5.0,
        retry_count=1,
        retry_delay=0.1,
    )

    async with KVClient(config) as client:
        resp = await client.get("nonexistent")
        assert resp.success is True
        assert resp.value is None
        assert resp.vector_clock is None


@pytest.mark.asyncio
async def test_get_with_conflicts(conflict_server):
    """Test that get correctly handles conflicting values."""
    server, port, servicer = conflict_server
    config = ClientConfig(
        seed_nodes=[f"localhost:{port}"],
        timeout=5.0,
        retry_count=1,
        retry_delay=0.1,
    )

    async with KVClient(config) as client:
        resp = await client.get("conflict-key")
        assert resp.success is True
        assert resp.has_conflict is True
        assert len(resp.conflicting_values) == 2
        values = [cv[0] for cv in resp.conflicting_values]
        assert b"value1" in values
        assert b"value2" in values


# ---------------------------------------------------------------------------
# Test: retry on transient failure
# ---------------------------------------------------------------------------


class FailThenSucceedServicer(kvstore_pb2_grpc.KVStoreServiceServicer):
    """Servicer that fails N times then succeeds."""

    def __init__(self, fail_count: int = 2):
        self.fail_count = fail_count
        self.attempt = 0

    async def Put(self, request, context):
        self.attempt += 1
        if self.attempt <= self.fail_count:
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details("Transient failure")
            return kvstore_pb2.PutResponse(success=False)
        clock = kvstore_pb2.VectorClockProto()
        clock.entries.append(
            kvstore_pb2.VectorClockEntry(node_id="node-1", counter=1)
        )
        return kvstore_pb2.PutResponse(success=True, vector_clock=clock)


@pytest.fixture
async def retry_server():
    """Server that fails twice then succeeds."""
    servicer = FailThenSucceedServicer(fail_count=2)
    server = grpc.aio.server()
    kvstore_pb2_grpc.add_KVStoreServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("[::]:0")
    await server.start()
    yield server, port, servicer
    await server.stop(grace=0)


@pytest.mark.asyncio
async def test_retry_on_transient_failure(retry_server):
    """Test that the client retries on UNAVAILABLE and eventually succeeds."""
    server, port, servicer = retry_server
    config = ClientConfig(
        seed_nodes=[f"localhost:{port}"],
        timeout=5.0,
        retry_count=3,
        retry_delay=0.05,
    )

    async with KVClient(config) as client:
        resp = await client.put("retry-key", b"retry-value")
        assert resp.success is True
        # Should have taken 3 attempts (2 failures + 1 success)
        assert servicer.attempt == 3


@pytest.mark.asyncio
async def test_retry_exhausted_raises_connection_error(retry_server):
    """Test that exhausting retries on a single node raises ConnectionError."""
    server, port, servicer = retry_server
    # Only 1 retry allowed, but server fails 2 times
    config = ClientConfig(
        seed_nodes=[f"localhost:{port}"],
        timeout=5.0,
        retry_count=1,
        retry_delay=0.05,
    )

    async with KVClient(config) as client:
        with pytest.raises(ConnectionError, match="All seed nodes exhausted"):
            await client.put("fail-key", b"fail-value")


# ---------------------------------------------------------------------------
# Test: failover to next seed node
# ---------------------------------------------------------------------------


class AlwaysUnavailableServicer(kvstore_pb2_grpc.KVStoreServiceServicer):
    """Servicer that always returns UNAVAILABLE."""

    async def Put(self, request, context):
        context.set_code(grpc.StatusCode.UNAVAILABLE)
        context.set_details("Node down")
        return kvstore_pb2.PutResponse(success=False)

    async def Get(self, request, context):
        context.set_code(grpc.StatusCode.UNAVAILABLE)
        context.set_details("Node down")
        return kvstore_pb2.GetResponse(found=False)


@pytest.fixture
async def failover_servers():
    """Two servers: first always fails, second works normally."""
    # First server - always unavailable
    bad_servicer = AlwaysUnavailableServicer()
    bad_server = grpc.aio.server()
    kvstore_pb2_grpc.add_KVStoreServiceServicer_to_server(bad_servicer, bad_server)
    bad_port = bad_server.add_insecure_port("[::]:0")
    await bad_server.start()

    # Second server - works normally
    good_servicer = MockKVStoreServicer()
    good_server = grpc.aio.server()
    kvstore_pb2_grpc.add_KVStoreServiceServicer_to_server(good_servicer, good_server)
    good_port = good_server.add_insecure_port("[::]:0")
    await good_server.start()

    yield bad_port, good_port, good_servicer
    await bad_server.stop(grace=0)
    await good_server.stop(grace=0)


@pytest.mark.asyncio
async def test_failover_to_next_seed_node(failover_servers):
    """Test that client fails over to the next seed node when current is unavailable."""
    bad_port, good_port, good_servicer = failover_servers
    config = ClientConfig(
        seed_nodes=[f"localhost:{bad_port}", f"localhost:{good_port}"],
        timeout=2.0,
        retry_count=2,
        retry_delay=0.05,
    )

    # Connect to the first (bad) node initially
    client = KVClient(config)
    # Manually connect to the bad node (skip channel_ready check for failover test)
    channel = grpc.aio.insecure_channel(f"localhost:{bad_port}")
    client._channel = channel
    client._stub = kvstore_pb2_grpc.KVStoreServiceStub(channel)
    client._connected = True
    client._current_node_index = 0

    try:
        # This should fail on bad node, then failover to good node
        resp = await client.put("failover-key", b"failover-value")
        assert resp.success is True
        assert good_servicer.put_count == 1
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Test: context manager opens and closes connection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_manager_opens_and_closes(grpc_server):
    """Test that the context manager properly opens and closes the connection."""
    server, port, servicer = grpc_server
    config = ClientConfig(
        seed_nodes=[f"localhost:{port}"],
        timeout=5.0,
        retry_count=1,
        retry_delay=0.1,
    )

    client = KVClient(config)
    assert client._connected is False

    async with client:
        assert client._connected is True
        assert client._channel is not None
        assert client._stub is not None

    # After exiting context, connection should be closed
    assert client._connected is False
    assert client._channel is None
    assert client._stub is None


@pytest.mark.asyncio
async def test_context_manager_with_no_seed_nodes():
    """Test that context manager raises when no seed nodes are configured."""
    config = ClientConfig(seed_nodes=[], timeout=1.0)
    with pytest.raises(ConnectionError, match="No seed nodes configured"):
        async with KVClient(config):
            pass


# ---------------------------------------------------------------------------
# Test: timeout handling
# ---------------------------------------------------------------------------


class SlowServicer(kvstore_pb2_grpc.KVStoreServiceServicer):
    """Servicer that takes too long to respond."""

    async def Get(self, request, context):
        await asyncio.sleep(10)  # Sleep longer than any reasonable timeout
        return kvstore_pb2.GetResponse(found=False)


@pytest.fixture
async def slow_server():
    """Server that responds very slowly."""
    servicer = SlowServicer()
    server = grpc.aio.server()
    kvstore_pb2_grpc.add_KVStoreServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("[::]:0")
    await server.start()
    yield server, port, servicer
    await server.stop(grace=0)


@pytest.mark.asyncio
async def test_timeout_handling(slow_server):
    """Test that operations time out when the server is too slow."""
    server, port, servicer = slow_server
    config = ClientConfig(
        seed_nodes=[f"localhost:{port}"],
        timeout=0.5,
        retry_count=1,
        retry_delay=0.05,
    )

    async with KVClient(config) as client:
        # The timeout should cause a DEADLINE_EXCEEDED error which is not
        # UNAVAILABLE, so it should raise grpc.RpcError directly
        with pytest.raises(grpc.RpcError):
            await client.get("slow-key")


# ---------------------------------------------------------------------------
# Test: put with vector clock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_with_vector_clock(grpc_server):
    """Test that put correctly sends a vector clock for read-modify-write."""
    server, port, servicer = grpc_server
    config = ClientConfig(
        seed_nodes=[f"localhost:{port}"],
        timeout=5.0,
        retry_count=1,
        retry_delay=0.1,
    )

    clock = VectorClock(entries={"node-1": 1})

    async with KVClient(config) as client:
        resp = await client.put("key", b"value", vector_clock=clock)
        assert resp.success is True
        assert resp.vector_clock is not None


# ---------------------------------------------------------------------------
# Test: not connected raises error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operation_without_connect_raises():
    """Test that calling operations without connecting raises ConnectionError."""
    config = ClientConfig(
        seed_nodes=["localhost:50051"],
        timeout=1.0,
        retry_count=1,
        retry_delay=0.1,
    )
    client = KVClient(config)
    with pytest.raises(ConnectionError, match="not connected"):
        await client.put("key", b"value")


# ---------------------------------------------------------------------------
# Test: delete with consistency level
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_with_consistency(grpc_server):
    """Test delete operation with a specified consistency level."""
    server, port, servicer = grpc_server
    config = ClientConfig(
        seed_nodes=[f"localhost:{port}"],
        timeout=5.0,
        retry_count=1,
        retry_delay=0.1,
    )

    async with KVClient(config) as client:
        await client.put("del-key", b"del-value")
        resp = await client.delete("del-key", consistency="quorum")
        assert resp.success is True
        assert servicer.delete_count == 1
