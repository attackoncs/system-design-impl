"""Immutable prefix-tree snapshots with cached, deterministic top-five results."""

import heapq
import itertools
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import AbstractSet, Dict, Iterable, List, Mapping, Optional, Tuple

Match = Tuple[str, int]
Rank = Tuple[int, str]
_TOP_LIMIT = 5


def normalize(value: str, empty: bool = False) -> str:
    """Normalize a bounded English query or prefix, preserving prefix spaces."""
    if not isinstance(value, str) or len(value) > 200:
        raise ValueError("query must be a bounded string")
    trailing = empty and bool(value) and value[-1].isspace()
    value = " ".join(value.lower().split())
    if value and trailing:
        value += " "
    if not value:
        if empty:
            return ""
        raise ValueError("query is empty")
    pattern = r"[a-z]+(?: [a-z]+)* ?" if empty else r"[a-z]+(?: [a-z]+)*"
    if len(value) > 50 or not re.fullmatch(pattern, value):
        raise ValueError("only English letters/spaces up to 50 characters supported")
    return value


def _rank(item: Match) -> Rank:
    return -item[1], item[0]


def ranked(items: Iterable[Match]) -> List[Match]:
    """Return the five best pairs, ordered by descending count then query."""
    return sorted(items, key=_rank)[:_TOP_LIMIT]


@dataclass(frozen=True)
class Node:
    """A published node: children, terminal and cached matches are read-only."""

    children: Mapping[str, "Node"]
    terminal: Optional[Match]
    top: Tuple[Match, ...]

    def __post_init__(self) -> None:
        # Copy before exposing a proxy so no retained builder dict can mutate it.
        object.__setattr__(self, "children", MappingProxyType(dict(self.children)))


@dataclass
class _BuilderNode:
    children: Dict[str, "_BuilderNode"] = field(default_factory=dict)
    terminal: Optional[Match] = None


def _freeze(builder: _BuilderNode) -> Node:
    children = {char: _freeze(child) for char, child in builder.children.items()}
    candidates = [builder.terminal] if builder.terminal is not None else []
    for child in children.values():
        candidates.extend(child.top)
    return Node(children, builder.terminal, tuple(ranked(candidates)))


def _available_bound(node: Node, blocked: AbstractSet[str]) -> Optional[Rank]:
    """Bound the best allowed match without expanding this subtree.

    If all five cached entries are blocked, any uncached result ranks below
    the fifth one. A shorter cache already contains the complete subtree.
    """
    for item in node.top:
        if item[0] not in blocked:
            return _rank(item)
    if len(node.top) == _TOP_LIMIT:
        return _rank(node.top[-1])
    return None


def _filtered_top(node: Node, blocked: AbstractSet[str]) -> Tuple[Match, ...]:
    """Refill from promising subtrees, stopping once no remaining bound wins."""
    sequence = itertools.count()
    pending: List[Tuple[Rank, int, Node]] = []
    best: List[Match] = []
    bound = _available_bound(node, blocked)
    if bound is not None:
        heapq.heappush(pending, (bound, next(sequence), node))
    while pending:
        bound, _, current = heapq.heappop(pending)
        if len(best) == _TOP_LIMIT and bound >= _rank(best[-1]):
            break
        candidates = dict(best)
        # A terminal can rank below the cached five and needs its own visit.
        for item in (*current.top, current.terminal):
            if item is not None and item[0] not in blocked:
                candidates[item[0]] = item[1]
        best = ranked(candidates.items())
        cutoff = _rank(best[-1]) if len(best) == _TOP_LIMIT else None
        for child in current.children.values():
            bound = _available_bound(child, blocked)
            if bound is not None and (cutoff is None or bound < cutoff):
                heapq.heappush(pending, (bound, next(sequence), child))
    return tuple(best)


@dataclass(frozen=True, init=False)
class Trie:
    """An immutable snapshot safe to share among concurrent readers.

    Each prefix caches five matches. Immediate filtering uses rank bounds to
    avoid scanning unrelated, lower-ranked branches, though arbitrary large
    block sets can still require visiting the whole matching subtree.
    """

    root: Node

    def __init__(self, frequencies: Iterable[Match]):
        """Build from unique normalized queries and strictly positive counts."""
        root = _BuilderNode()
        for query, frequency in frequencies:
            if (
                normalize(query) != query
                or not isinstance(frequency, int)
                or isinstance(frequency, bool)
                or frequency <= 0
            ):
                raise ValueError("invalid snapshot frequency")
            node = root
            for char in query:
                child = node.children.get(char)
                if child is None:
                    child = _BuilderNode()
                    node.children[char] = child
                node = child
            if node.terminal is not None:
                raise ValueError("duplicate snapshot query")
            node.terminal = query, frequency
        object.__setattr__(self, "root", _freeze(root))

    def suggest(
        self, prefix: str, blocked: AbstractSet[str] = frozenset()
    ) -> Tuple[Match, ...]:
        """Return up to five ranked, unblocked matches for an English prefix."""
        prefix = normalize(prefix, empty=True)
        if not prefix:
            return ()
        node = self.root
        for char in prefix:
            node = node.children.get(char)
            if node is None:
                return ()
        if not any(query in blocked for query, _ in node.top):
            return node.top
        return _filtered_top(node, blocked)
