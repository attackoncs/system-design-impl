"""Snapshot safety and filtered ranking against an independent sort oracle."""

import concurrent.futures
import dataclasses
import random
import string

import pytest
from search_autocomplete import Trie


def oracle(rows, prefix, blocked=frozenset()):
    return tuple(
        sorted(
            (
                row
                for row in rows
                if row[0].startswith(prefix) and row[0] not in blocked
            ),
            key=lambda row: (-row[1], row[0]),
        )[:5]
    )


def test_published_snapshot_rejects_mutation_at_every_level():
    rows = [("alpha", 20), ("alphabet", 10), ("beta", 5)]
    trie = Trie(rows)
    rows[0] = ("alpha", 1)
    child = trie.root.children["a"]
    with pytest.raises(TypeError):
        trie.root.children["a"] = child
    with pytest.raises(TypeError):
        child.children["x"] = child
    with pytest.raises(dataclasses.FrozenInstanceError):
        child.top = ()
    with pytest.raises(dataclasses.FrozenInstanceError):
        child.terminal = ("altered", 999)
    with pytest.raises(dataclasses.FrozenInstanceError):
        trie.root = child
    assert trie.suggest("a") == (("alpha", 20), ("alphabet", 10))


@pytest.mark.parametrize(
    "frequency", [True, False, 0, -1, 1.5, float("inf"), float("nan"), "2"]
)
def test_snapshot_rejects_invalid_frequencies(frequency):
    with pytest.raises(ValueError, match="invalid snapshot frequency"):
        Trie([("alpha", frequency)])


@pytest.mark.parametrize("query", ["Alpha", "alpha!", "", "a" * 51, "new  york"])
def test_snapshot_requires_normalized_bounded_queries(query):
    with pytest.raises(ValueError):
        Trie([(query, 1)])


def test_snapshot_rejects_duplicate_query_even_when_count_differs():
    with pytest.raises(ValueError, match="duplicate snapshot query"):
        Trie([("alpha", 1), ("alpha", 2)])


def test_filter_can_recover_terminal_below_its_five_cached_descendants():
    rows = [("a", 1), *(("a" + char, 10) for char in "bcdef")]
    blocked = frozenset(query for query, _ in rows if query != "a")
    assert Trie(rows).suggest("a", blocked) == (("a", 1),)


def test_filtered_ranking_matches_full_sort_over_random_trees():
    rng = random.Random(1401)
    for _ in range(80):
        queries = {"a", "a phrase", "a prefix", "another", "bet"}
        queries.update(
            "a" + "".join(rng.choices("abcdef", k=rng.randint(1, 8)))
            for _ in range(250)
        )
        rows = [(query, rng.randint(1, 20)) for query in sorted(queries)]
        rng.shuffle(rows)
        trie = Trie(iter(rows))
        prefixes = ["a", "a ", "ab", "aa", "ac", "another", "b", "zzz"]
        filters = [
            frozenset(),
            frozenset(query for query in queries if rng.random() < 0.1),
            frozenset(query for query in queries if rng.random() < 0.8),
            frozenset(queries),
            frozenset(query for query, _ in oracle(rows, "a")),
        ]
        for blocked in filters:
            for prefix in prefixes:
                assert trie.suggest(prefix, blocked) == oracle(rows, prefix, blocked)


class CountingFilter(frozenset):
    def __new__(cls, values):
        instance = super().__new__(cls, values)
        instance.probes = 0
        return instance

    def __contains__(self, query):
        self.probes += 1
        return super().__contains__(query)


def test_one_removal_refills_without_examining_the_whole_subtree():
    letters = string.ascii_lowercase
    rows = [
        ("a" + first + second + third, 1)
        for first in letters
        for second in letters
        for third in letters
    ]
    blocked = CountingFilter(["aaaa"])
    trie = Trie(rows)
    result = trie.suggest("a", blocked)
    # A full subtree scan would perform at least one probe per 17,576 terminals.
    assert blocked.probes < 250
    assert result == oracle(rows, "a", frozenset(["aaaa"]))


def test_concurrent_readers_share_the_same_snapshot():
    rows = [("a" + char, count) for count, char in enumerate(string.ascii_lowercase, 1)]
    trie = Trie(rows)
    filters = [
        frozenset(query for query, count in rows if count % modulus == 0)
        for modulus in range(2, 12)
    ]
    work = filters * 20
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda blocked: trie.suggest("a", blocked), work))
    assert results == [oracle(rows, "a", blocked) for blocked in work]
