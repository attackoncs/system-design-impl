"""Real loopback gRPC regressions; no coordinator/network mocks."""

import asyncio
from pathlib import Path

import pytest

from kv_store.node import KVNode
from kv_store.config import (
    NodeConfig,
    StorageConfig,
    NetworkConfig,
    ClusterConfig,
    ReplicationConfig,
)
from kv_store.client import KVClient, ClientConfig
from kv_store.replication.records import Version
from kv_store.replication.vector_clock import VectorClock
from kv_store.replication.quorum import ConsistencyLevel


def configuration(root, name, seeds=(), n=3):
    folder = Path(root) / name
    return NodeConfig(
        node_id=name,
        storage=StorageConfig(
            str(folder), str(folder / "wal"), str(folder / "sstables")
        ),
        network=NetworkConfig("127.0.0.1", 0),
        cluster=ClusterConfig(
            gossip_interval_seconds=0.05,
            failure_timeout_seconds=10,
            anti_entropy_interval_seconds=3600,
            hinted_handoff_interval_seconds=3600,
            merkle_tree_buckets=16,
            seed_nodes=list(seeds),
        ),
        replication=ReplicationConfig(n, min(2, n), min(2, n)),
        virtual_nodes=20,
    )


async def wait_until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), 5)


async def cluster(root):
    nodes = []
    try:
        for i in range(3):
            seeds = [] if not nodes else [f"127.0.0.1:{nodes[0]._grpc_server.port}"]
            node = KVNode(configuration(root, f"n{i}", seeds))
            await node.start()
            nodes.append(node)
        await wait_until(lambda: all(len(node.hash_ring.nodes) == 3 for node in nodes))
        return nodes
    except BaseException:
        for node in reversed(nodes):
            await node.stop()
        raise


@pytest.mark.asyncio
async def test_real_cluster_write_read_delete_and_empty_bytes(tmp_path):
    nodes = await cluster(tmp_path)
    try:
        async with KVClient(
            ClientConfig([f"127.0.0.1:{nodes[0]._grpc_server.port}"], retry_count=0)
        ) as client:
            result = await client.put("key", b"", consistency="all")
            read = await nodes[1].get("key", ConsistencyLevel.ALL)
            assert read.found and read.value == b""
            assert read.vector_clock == result.vector_clock
            await nodes[2].delete("key", read.vector_clock, ConsistencyLevel.ALL)
            deleted = await client.get("key", consistency="all")
            assert deleted.value is None
            assert deleted.vector_clock.dominates(read.vector_clock)
    finally:
        for node in reversed(nodes):
            await node.stop()
            assert node._grpc_client.active_channels == 0


@pytest.mark.asyncio
async def test_conflicts_and_tombstones_survive_restart(tmp_path):
    config = configuration(tmp_path, "single", n=1)
    node = KVNode(config)
    await node.start()
    try:
        left = Version(b"left", VectorClock({"left": 1}), 1)
        right = Version(None, VectorClock({"right": 1}), 2)
        await node._records.merge("conflict", [left, right])
    finally:
        await node.stop()
    node = KVNode(config)
    await node.start()
    try:
        result = await node.get("conflict")
        assert result.has_conflict
        assert {value for value, _ in result.conflicting_values} == {None, b"left"}
        merged = VectorClock({"left": 1, "right": 1})
        await node.put("conflict", b"resolved", merged)
        assert (await node.get("conflict")).value == b"resolved"
        assert node._merkle_tree.get_keys_in_bucket(
            node._merkle_tree._key_to_bucket("conflict")
        ) == ["conflict"]
    finally:
        await node.stop()


@pytest.mark.asyncio
async def test_bidirectional_anti_entropy_repairs_values_and_tombstones(tmp_path):
    nodes = await cluster(tmp_path)
    try:
        await nodes[0]._records.merge(
            "left", [Version(b"a", VectorClock({"n0": 1}), 1)]
        )
        await nodes[1]._records.merge(
            "right", [Version(None, VectorClock({"n1": 1}), 2)]
        )
        await nodes[0]._repair.sync("n1")
        assert (await nodes[1]._records.read("left"))[0].value == b"a"
        assert (await nodes[0]._records.read("right"))[0].value is None
        assert (
            nodes[0]._merkle_tree.get_root_hash()
            == nodes[1]._merkle_tree.get_root_hash()
        )
    finally:
        for node in reversed(nodes):
            await node.stop()


@pytest.mark.asyncio
async def test_hint_journal_survives_reopening(tmp_path):
    nodes = await cluster(tmp_path)
    try:
        await nodes[0]._hints.save(
            "n1", "hinted", [Version(b"kept", VectorClock({"n0": 2}), 1)]
        )
        from kv_store.cluster.repair import HintJournal

        reopened = HintJournal(
            nodes[0]._hints.path, nodes[0].membership, nodes[0]._grpc_client
        )
        await reopened.deliver()
        assert (await nodes[1]._records.read("hinted"))[0].value == b"kept"
        assert reopened._execute("SELECT count(*) FROM hints")[0][0] == 0
    finally:
        for node in reversed(nodes):
            await node.stop()


@pytest.mark.asyncio
async def test_quorum_returns_before_slow_replicas_and_shutdown_drains(
    tmp_path, monkeypatch
):
    nodes = await cluster(tmp_path)
    entered = asyncio.Event()
    gate = asyncio.Event()

    async def slow_replica(*args):
        entered.set()
        await gate.wait()
        return True

    try:
        monkeypatch.setattr(nodes[0]._grpc_client, "merge_replica", slow_replica)
        result = await asyncio.wait_for(
            nodes[0].put("slow", b"value", consistency=ConsistencyLevel.ONE), 2
        )
        assert result.success
        await entered.wait()
        runtime = nodes[0]._coordinator._runtime
        assert runtime._tasks  # slow RPCs are still owned after quorum response
        await nodes[0].stop()
        assert not runtime._tasks
    finally:
        for node in reversed(nodes):
            if node.is_running:
                await node.stop()


@pytest.mark.asyncio
async def test_restart_same_node_and_stale_replay_cannot_resurrect(tmp_path):
    node = KVNode(configuration(tmp_path, "restart", n=1))
    await node.start()
    first = await node.put("key", b"value")
    await node.delete("key", first.vector_clock)
    await node.stop()
    await node.start()
    try:
        await node._records.merge("key", [Version(b"value", first.vector_clock, 1)])
        assert not (await node.get("key")).found
        assert (await node.put("other", b"ok")).success
    finally:
        await node.stop()


@pytest.mark.asyncio
async def test_invalid_consistency_is_rejected_over_grpc(tmp_path):
    import grpc
    from kv_store.network import kvstore_pb2, kvstore_pb2_grpc

    node = KVNode(configuration(tmp_path, "validation", n=1))
    await node.start()
    try:
        async with grpc.aio.insecure_channel(
            f"127.0.0.1:{node._grpc_server.port}"
        ) as channel:
            stub = kvstore_pb2_grpc.KVStoreServiceStub(channel)
            with pytest.raises(grpc.aio.AioRpcError) as error:
                await stub.Put(
                    kvstore_pb2.PutRequest(
                        key="key", value=b"v", consistency_level="typo"
                    )
                )
            assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        assert not (await node.get("key")).found
    finally:
        await node.stop()


def test_admission_counts_requests_before_their_first_await(tmp_path):
    from contextlib import ExitStack

    node = KVNode(configuration(tmp_path, "bounded", n=3))
    runtime = node._coordinator._runtime
    with ExitStack() as stack:
        for _ in range(85):
            stack.enter_context(runtime._admit())
        with pytest.raises(RuntimeError, match="capacity"):
            stack.enter_context(runtime._admit())
    assert runtime._reserved == 0


@pytest.mark.asyncio
async def test_offline_hint_backlog_does_not_starve_later_live_target(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from kv_store.cluster.repair import HintJournal

    transport = SimpleNamespace(merge_replica=AsyncMock(return_value=True))
    membership = SimpleNamespace(
        is_node_alive=lambda n: n == "live", get_node_address=lambda n: n
    )
    journal = HintJournal(tmp_path / "hints.sqlite", membership, transport)
    versions = [Version(b"v", VectorClock({"writer": 1}), 1)]
    for _ in range(100):
        await journal.save("offline", "key", versions)
    await journal.save("live", "key", versions)
    await journal.deliver()
    transport.merge_replica.assert_not_awaited()
    await journal.deliver()
    transport.merge_replica.assert_awaited_once()
    assert journal._execute("SELECT count(*) FROM hints")[0][0] == 100


@pytest.mark.parametrize(
    "field,value",
    [
        ("clock", {"n": -1}),
        ("clock", {"n": True}),
        ("timestamp", float("nan")),
        ("value", 12),
    ],
)
def test_invalid_replica_envelopes_are_rejected(field, value):
    import json
    from kv_store.replication.records import decode

    row = {"clock": {"n": 1}, "timestamp": 1, "value": "dg=="}
    row[field] = value
    with pytest.raises(ValueError):
        decode(json.dumps({"format": 1, "versions": [row]}).encode())


def test_replica_placement_spreads_across_failure_domains():
    from types import SimpleNamespace
    from kv_store.cluster.placement import ReplicaPlacement

    order = ["a", "b", "c", "d"]
    ring = SimpleNamespace(nodes=order, get_nodes=lambda key, count: order[:count])
    placement = ReplicaPlacement(
        ring, {"a": "east", "b": "east", "c": "west", "d": "north"}
    )
    assert placement.get_nodes("key", 3) == ["a", "c", "d"]
    assert placement.get_nodes("key", 4) == ["a", "c", "d", "b"]
    assert ReplicaPlacement(ring, {}).get_nodes("key", 3) == order[:3]


@pytest.mark.asyncio
async def test_quorum_remains_available_with_one_failed_replica(tmp_path):
    nodes = await cluster(tmp_path)
    try:
        await nodes[2].stop()
        written = await asyncio.wait_for(nodes[0].put("outage", b"available"), 3)
        read = await asyncio.wait_for(nodes[1].get("outage"), 3)
        assert written.success and read.value == b"available"
    finally:
        for node in reversed(nodes):
            if node.is_running:
                await node.stop()


@pytest.mark.asyncio
async def test_bounded_clock_keeps_coordinator_counter_across_restart(tmp_path):
    config = configuration(tmp_path, "bounded-clock", n=1)
    config.replication.vector_clock_max_entries = 1
    node = KVNode(config)
    await node.start()
    try:
        first = await node.put("key", b"first", VectorClock({"old-writer": 100}))
        assert first.vector_clock.to_dict() == {node.node_id: 1}
        await node.stop()
        await node.start()
        second = await node.put("key", b"second")
        assert second.vector_clock.to_dict() == {node.node_id: 2}
    finally:
        if node.is_running:
            await node.stop()
