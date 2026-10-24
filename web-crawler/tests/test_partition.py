"""Host partitioning, topology changes, and immutable snapshots."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from web_crawler.partition import ConsistentHashPartition


def test_canonical_host_stays_on_one_worker():
    partition = ConsistentHashPartition(["worker-a", "worker-b", "worker-c"])
    variants = [
        "https://EXAMPLE.com/one",
        "http://example.com/two?q=1",
        "https://example.com.:8443/three",
        "https://user:pass@example.com/four",
    ]
    assert len({partition.owner_for_url(url) for url in variants}) == 1
    assert partition.owner_for_url(
        "https://bücher.example/"
    ) == partition.owner_for_url("http://xn--bcher-kva.example:8080/")
    assert partition.owner_for_url("http://[2001:0db8::1]/") == partition.owner_for_url(
        "https://[2001:db8::1]:8443/"
    )


def test_membership_order_does_not_change_assignment():
    first = ConsistentHashPartition(["a", "b", "c"], virtual_nodes=32)
    second = ConsistentHashPartition(["c", "a", "b"], virtual_nodes=32)
    for index in range(200):
        url = f"https://host-{index}.example/"
        assert first.owners_for_url(url, 3) == second.owners_for_url(url, 3)
        assert len(set(first.owners_for_url(url, 20))) == 3


def test_add_and_remove_only_reassign_affected_owners():
    partition = ConsistentHashPartition(["a", "b", "c"], virtual_nodes=64)
    urls = [f"https://host-{index}.example/" for index in range(2000)]
    original = {url: partition.owner_for_url(url) for url in urls}
    before = partition.snapshot()
    assert partition.add_node("d")
    assert not partition.add_node("d")
    added = {url: partition.owner_for_url(url) for url in urls}
    changed = [url for url in urls if added[url] != original[url]]
    assert changed
    assert all(added[url] == "d" for url in changed)
    assert partition.remove_node("d")
    assert not partition.remove_node("d")
    assert {url: partition.owner_for_url(url) for url in urls} == original
    assert before.nodes == ("a", "b", "c")
    assert all(before.owner_for_url(url) == original[url] for url in urls)
    assert partition.remove_node("b")
    assert all(
        partition.owner_for_url(url) == owner
        for url, owner in original.items()
        if owner != "b"
    )


def test_hash_collisions_preserve_all_physical_workers(monkeypatch):
    monkeypatch.setattr("web_crawler.partition._hash_bytes", lambda value: 0)
    partition = ConsistentHashPartition(["c", "a", "b"], virtual_nodes=3)
    assert partition.snapshot().token_count == 9
    assert partition.owners_for_url("https://example.com", 100) == ("a", "b", "c")
    assert partition.remove_node("a")
    assert partition.owners_for_url("https://example.com", 100) == ("b", "c")


def test_empty_ring_and_local_ownership():
    partition = ConsistentHashPartition([], local_node_id="a")
    with pytest.raises(LookupError):
        partition.owner_for_url("https://example.com")
    partition.add_node("a")
    assert partition.owns("https://example.com")
    partition.remove_node("a")
    partition.add_node("b")
    assert not partition.owns("https://example.com")
    with pytest.raises(ValueError, match="local_node_id"):
        ConsistentHashPartition(["a"]).owns("https://example.com")


@pytest.mark.parametrize(
    "url",
    [
        "",
        "example.com",
        "ftp://example.com",
        "http:///path",
        "https://exa mple.com",
        "http://[broken/",
        "http://example.com:bad/",
        "http://./",
        " https://example.com",
    ],
)
def test_invalid_urls_are_rejected(url):
    with pytest.raises(ValueError):
        ConsistentHashPartition(["a"]).owner_for_url(url)


@pytest.mark.parametrize("count", [0, -1, True, 1.5])
def test_invalid_replica_counts_are_rejected(count):
    with pytest.raises(ValueError):
        ConsistentHashPartition(["a"]).owners_for_url("https://example.com", count)


@pytest.mark.parametrize(
    "nodes,virtual_nodes",
    [
        (["a", "a"], 1),
        ([""], 1),
        ([" a"], 1),
        ([None], 1),
        ("worker-a", 1),
        (["a"], 0),
        (["a"], True),
        (["a"], 1.5),
    ],
)
def test_invalid_configuration_is_rejected(nodes, virtual_nodes):
    with pytest.raises((ValueError, TypeError)):
        ConsistentHashPartition(nodes, virtual_nodes=virtual_nodes)


def test_snapshot_readers_survive_concurrent_membership_changes():
    partition = ConsistentHashPartition(["stable"], virtual_nodes=4)

    def update_membership():
        for index in range(100):
            node_id = f"temporary-{index}"
            partition.add_node(node_id)
            partition.remove_node(node_id)

    def read_topology():
        for _ in range(200):
            snapshot = partition.snapshot()
            assert snapshot.token_count == len(snapshot.nodes) * 4
            assert snapshot.owner_for_url("https://example.com") in snapshot.nodes

    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [pool.submit(update_membership)] + [
            pool.submit(read_topology) for _ in range(3)
        ]
        for job in jobs:
            job.result()
    assert partition.snapshot().nodes == ("stable",)
