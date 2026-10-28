"""Incremental Merkle invariants and real bounded gRPC repair."""

import asyncio
import dataclasses
import hashlib

import grpc
import pytest

from kv_store.cluster.merkle_tree import MerkleTree
from kv_store.network import kvstore_pb2 as proto
from kv_store.node import KVNode
from kv_store.replication.records import Version
from kv_store.replication.vector_clock import VectorClock
from tests.test_runtime_cluster import configuration, wait_until


def reference_root(tree):
    level = [
        hashlib.sha256(value.encode()).digest()
        for value in tree.get_bucket_hashes().values()
    ]
    while len(level) > 1:
        level = [
            hashlib.sha256(level[i] + level[i + 1]).digest()
            for i in range(0, len(level), 2)
        ]
    return level[0].hex()


@pytest.mark.parametrize("buckets", [1, 2, 16, 1024])
def test_incremental_roots_preserve_the_existing_hash_format(buckets):
    tree = MerkleTree(buckets)
    view = tree.snapshot()
    original = view.root_hash
    for index in range(35):
        tree.update(f"k{index}", f"value{index}")
        assert tree.get_root_hash() == reference_root(tree)
    for index in range(0, 35, 2):
        tree.remove(f"k{index}")
        assert tree.get_root_hash() == reference_root(tree)
    assert view.root_hash == original
    with pytest.raises(dataclasses.FrozenInstanceError):
        view.bucket_count = 9


def test_root_update_hashes_only_one_path(monkeypatch):
    tree = MerkleTree(4096)
    original, calls = hashlib.sha256, []

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(hashlib, "sha256", counted)
    tree.update("one", "value")
    assert len(calls) <= 16  # bucket assignment, bucket, leaf and12 parents


def test_bucket_pages_are_sorted_bounded_and_survive_deletion():
    tree = MerkleTree(1)
    for index in reversed(range(91)):
        tree.update(f"k{index:03}", "value")
    tree.remove("k050")
    keys, after = [], ""
    while True:
        page, more = tree.key_page(0, after, 7)
        assert len(page) <= 7
        keys.extend(page)
        if not more:
            break
        after = page[-1]
    assert keys == [f"k{i:03}" for i in range(91) if i != 50]


@pytest.mark.parametrize("buckets", [True, 0, 3, 131072])
def test_invalid_merkle_capacity_rejected(buckets):
    with pytest.raises(ValueError):
        MerkleTree(buckets)


async def pair(root, *, buckets=16, message_size=64 * 1024):
    nodes = []
    try:
        for name in ("left", "right"):
            config = configuration(root, name, n=2)
            config.cluster.merkle_tree_buckets = buckets
            config.network.max_message_size_bytes = message_size
            config.cluster.seed_nodes = (
                [] if not nodes else [f"127.0.0.1:{nodes[0]._grpc_server.port}"]
            )
            node = KVNode(config)
            await node.start()
            nodes.append(node)
        await wait_until(lambda: all(len(node.hash_ring.nodes) == 2 for node in nodes))
        return nodes
    except BaseException:
        for node in reversed(nodes):
            await node.stop()
        raise


@pytest.mark.asyncio
async def test_real_repair_exceeds_message_limit_using_small_pages(tmp_path):
    nodes = await pair(tmp_path, buckets=1)
    left, right = nodes
    try:
        payload = b"x" * 8192
        for index in range(24):
            await left._records.merge(
                f"left-{index:03}",
                [Version(payload, VectorClock({"left": index + 1}), index)],
            )
        await right._records.merge(
            "tombstone", [Version(None, VectorClock({"right": 1}), 1)]
        )
        # The old all-differences response is larger than the64KiB channel.
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await right._grpc_client._get_stub(
                f"127.0.0.1:{left._grpc_server.port}"
            ).MerkleTreeSync(proto.MerkleTreeSyncRequest())
        assert error.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
        stats = await right._repair.sync("left")
        assert stats["pages"] >= 4 and stats["pulled_records"] == 24
        assert right._merkle_tree.get_root_hash() == left._merkle_tree.get_root_hash()
        assert (await left._records.read("tombstone"))[0].value is None
        assert (await right._records.read("left-023"))[0].value == payload
        again = await right._repair.sync("left")
        assert again["compared_nodes"] == 1 and again["pages"] == 0
    finally:
        for node in reversed(nodes):
            await node.stop()


@pytest.mark.asyncio
async def test_real_sparse_difference_uses_subtrees_not_all_bucket_hashes(tmp_path):
    nodes = await pair(tmp_path, buckets=1024)
    try:
        left, right = nodes
        await left._records.merge(
            "sparse", [Version(b"data", VectorClock({"left": 1}), 1)]
        )
        stats = await right._repair.sync("left")
        assert stats["differing_buckets"] == 1
        assert stats["compared_nodes"] == 21  # root and2 children per level
        assert stats["pages"] == 1
    finally:
        for node in reversed(nodes):
            await node.stop()


@pytest.mark.asyncio
async def test_stale_root_and_invalid_inspection_rejected_over_real_rpc(tmp_path):
    nodes = await pair(tmp_path)
    try:
        left, right = nodes
        address = f"127.0.0.1:{left._grpc_server.port}"
        original = left._merkle_tree.get_root_hash()
        await left._records.merge(
            "change", [Version(b"x", VectorClock({"left": 1}), 1)]
        )
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await right._grpc_client.repair_bucket_page(address, 16, 0, original)
        assert error.value.code() == grpc.StatusCode.ABORTED
        for indices in ([], [1, 1], [32], list(range(1, 66))):
            with pytest.raises(grpc.aio.AioRpcError) as error:
                await right._grpc_client.merkle_inspect(address, 16, indices)
            assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await right._grpc_client.merkle_inspect(address, 32, [1])
        assert error.value.code() == grpc.StatusCode.FAILED_PRECONDITION
        request = proto.RepairBucketPageRequest(
            bucket_count=16,
            bucket_id=0,
            expected_root=left._merkle_tree.get_root_hash(),
            limit=129,
        )
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await right._grpc_client._get_stub(address).RepairBucketPage(request)
        assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        request.limit = 1
        request.after_key = "x" * 257
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await right._grpc_client._get_stub(address).RepairBucketPage(request)
        assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    finally:
        for node in reversed(nodes):
            await node.stop()


@pytest.mark.asyncio
async def test_mutation_during_page_aborts_and_later_repair_converges(tmp_path):
    nodes = await pair(tmp_path, buckets=1)
    left, right = nodes
    original = left._records.read
    changed = False

    async def changing_read(key):
        nonlocal changed
        result = await original(key)
        if not changed:
            changed = True
            await left._records.merge(
                "late", [Version(b"late", VectorClock({"left": 2}), 2)]
            )
        return result

    try:
        await left._records.merge(
            "initial", [Version(b"first", VectorClock({"left": 1}), 1)]
        )
        left._records.read = changing_read
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await right._repair.sync("left")
        assert error.value.code() == grpc.StatusCode.ABORTED
        left._records.read = original
        await right._repair.sync("left")
        assert right._merkle_tree.get_root_hash() == left._merkle_tree.get_root_hash()
        assert (await right._records.read("late"))[0].value == b"late"
    finally:
        left._records.read = original
        for node in reversed(nodes):
            await node.stop()


@pytest.mark.asyncio
async def test_page_rejects_visible_commit_before_merkle_publication(tmp_path):
    nodes = await pair(tmp_path, buckets=1)
    left, right = nodes
    entered, release = asyncio.Event(), asyncio.Event()
    original = left._storage_engine.put
    task = None

    async def delayed_index(*args, **kwargs):
        await original(*args, **kwargs)
        entered.set()
        await release.wait()

    try:
        await left._records.merge("key", [Version(b"old", VectorClock({"left": 1}), 1)])
        root = left._merkle_tree.get_root_hash()
        left._storage_engine.put = delayed_index
        task = asyncio.create_task(
            left._records.merge("key", [Version(b"new", VectorClock({"left": 2}), 2)])
        )
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await right._grpc_client.repair_bucket_page(
                f"127.0.0.1:{left._grpc_server.port}", 1, 0, root
            )
        assert error.value.code() == grpc.StatusCode.ABORTED
    finally:
        release.set()
        if task is not None:
            await task
        left._storage_engine.put = original
        for node in reversed(nodes):
            await node.stop()
