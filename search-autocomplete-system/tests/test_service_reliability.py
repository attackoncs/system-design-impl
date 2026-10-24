"""Serving isolation and revision fences under concurrent cold/cache paths."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from search_autocomplete import Autocomplete, Store, Unavailable
import search_autocomplete.trie as trie_module


def test_no_generation_is_unavailable():
    instance = Store()
    try:
        with pytest.raises(Unavailable):
            Autocomplete(instance).suggest("a")
    finally:
        instance.close()


@pytest.fixture
def store():
    instance = Store()
    instance.record("a", "apple", 1)
    instance.record("z", "zebra", 1)
    instance.build(0, 2, 2)
    try:
        yield instance
    finally:
        instance.close()


@pytest.mark.parametrize("name", ["version", "revision", "shard"])
@pytest.mark.parametrize("value", [True, False, 1.0, "1", -1, [], {}])
def test_strict_identifiers_even_when_cache_is_warm(store, name, value):
    service = Autocomplete(store)
    service.suggest("a")
    with pytest.raises(ValueError):
        service.suggest("a", **{name: value})


def test_cold_trie_build_does_not_block_warm_read(store, monkeypatch):
    service = Autocomplete(store, cache_size=0)
    assert service.suggest("a") == (("apple", 1),)
    entered, release = threading.Event(), threading.Event()
    original = trie_module.Trie

    def slow_trie(items):
        entered.set()
        assert release.wait(5)
        return original(items)

    monkeypatch.setattr(trie_module, "Trie", slow_trie)
    with ThreadPoolExecutor(2) as pool:
        cold = pool.submit(service.suggest, "z")
        try:
            assert entered.wait(2)
            warm = pool.submit(service.suggest, "a")
            assert warm.result(2) == (("apple", 1),)
        finally:
            release.set()
        assert cold.result(2) == (("zebra", 1),)


def test_cache_hit_fences_mid_request_filter_change(store, monkeypatch):
    service = Autocomplete(store)
    service.suggest("a")
    original_get = service._get

    def get_and_block(cache, key):
        result = original_get(cache, key)
        if cache is service.cache:
            store.block("apple")
        return result

    monkeypatch.setattr(service, "_get", get_and_block)
    with pytest.raises(Unavailable, match="revision"):
        service.suggest("a")


def test_concurrent_cold_read_constructs_one_shared_trie(store, monkeypatch):
    service = Autocomplete(store, cache_size=0)
    original, calls = trie_module.Trie, []

    def counted(items):
        calls.append(1)
        return original(items)

    monkeypatch.setattr(trie_module, "Trie", counted)
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(service.suggest, ["a"] * 32))
    assert results == [(("apple", 1),)] * 32
    assert len(calls) == 1
