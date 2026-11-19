"""Regression coverage for causal ordering and partition recovery."""

import asyncio
import dataclasses

import pytest
from kv_store.cluster.gossip import GossipProtocol, NodeStatus
from kv_store.config import ReplicationConfig
from kv_store.replication.records import Version, decode, encode, reconcile
from kv_store.replication.vector_clock import VectorClock


@pytest.mark.parametrize(
    "left,right",
    [({"a": 0}, {"b": 0}), ({"n": 1, "a": 0}, {"n": 1, "b": 0}), ({"a": 0}, {})],
)
def test_zero_components_cannot_delete_causal_siblings(left, right):
    versions = decode(
        encode(
            [
                Version(b"left", VectorClock(left), 1),
                Version(b"right", VectorClock(right), 2),
            ]
        )
    )
    assert not versions[0].clock.dominates(versions[1].clock)
    assert not versions[1].clock.dominates(versions[0].clock)
    assert {version.value for version in reconcile(versions)} == {b"left", b"right"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("n_replicas", 0),
        ("n_replicas", -1),
        ("n_replicas", True),
        ("n_replicas", 3.0),
        ("w_quorum", 0),
        ("w_quorum", 4),
        ("w_quorum", True),
        ("w_quorum", 1.5),
        ("r_quorum", 0),
        ("r_quorum", 4),
        ("r_quorum", False),
        ("r_quorum", "2"),
    ],
)
def test_invalid_quorum_configuration_is_rejected(field, value):
    with pytest.raises(ValueError, match=field):
        ReplicationConfig(**{field: value})


def test_nonintersecting_quorums_remain_available():
    assert ReplicationConfig(3, 1, 1).w_quorum == 1


async def test_gossip_recovers_when_all_known_peers_were_down():
    nodes = {}

    async def exchange(origin, target, members):
        nodes[target].merge_membership(members)
        nodes[origin].merge_membership(list(nodes[target].members.values()))

    for name in ("a", "b", "c"):
        nodes[name] = GossipProtocol(
            name,
            name,
            gossip_func=lambda target, members, name=name: exchange(
                name, target, members
            ),
        )
    for name, node in nodes.items():
        for peer in nodes.keys() - {name}:
            node.add_seed_node(peer, peer)
            node._members[peer] = dataclasses.replace(
                node._members[peer], last_updated=0
            )
        node._check_timeouts()
        node._check_timeouts()
        assert all(
            node.get_node_status(peer) is NodeStatus.DOWN
            for peer in nodes.keys() - {name}
        )
    for _ in range(3):
        for node in nodes.values():
            await node._gossip_round()
    assert all(len(node.get_alive_nodes()) == 3 for node in nodes.values())


async def test_gossip_fanout_runs_concurrently_and_drains_repeated_cancellation():
    all_started, cleanup_started, release_cleanup = (asyncio.Event() for _ in range(3))
    started, finished = set(), set()

    async def exchange(peer, members):
        started.add(peer)
        if len(started) == 3:
            all_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
            finished.add(peer)

    node = GossipProtocol("a", "a", gossip_fanout=3, gossip_func=exchange)
    for peer in ("b", "c", "d"):
        node.add_seed_node(peer, peer)
    task = asyncio.create_task(node._gossip_round())
    try:
        await asyncio.wait_for(all_started.wait(), 1)
        task.cancel()
        await asyncio.wait_for(cleanup_started.wait(), 1)
        assert not task.done()
        task.cancel()
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished == {"b", "c", "d"}
    finally:
        release_cleanup.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_gossip_payloads_do_not_mutate_membership_or_other_peers():
    observed = []

    async def exchange(peer, members):
        own = next(member for member in members if member.node_id == "a")
        observed.append(own.address)
        own.address = "mutated"

    node = GossipProtocol("a", "original", gossip_fanout=2, gossip_func=exchange)
    node.add_seed_node("b", "b")
    node.add_seed_node("c", "c")
    await node._gossip_round()
    assert observed == ["original", "original"]
    assert node.members["a"].address == "original"
