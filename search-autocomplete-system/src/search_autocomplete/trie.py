"""Deterministic prefix-tree ranking; no database access in the lookup itself."""
import re


def normalize(value, empty=False):
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


def ranked(items):
    return sorted(items, key=lambda item: (-item[1], item[0]))[:5]


class Node:
    def __init__(self):
        self.children = {}
        self.terminal = None
        self.top = ()


class Trie:
    def __init__(self, frequencies):
        self.root = Node()
        for query, frequency in frequencies:
            if normalize(query) != query or type(frequency) is not int or frequency <= 0:
                raise ValueError("invalid snapshot frequency")
            node = self.root
            for char in query:
                node = node.children.setdefault(char, Node())
            if node.terminal:
                raise ValueError("duplicate snapshot query")
            node.terminal = (query, frequency)
        self._build(self.root)

    def _build(self, node):
        items = [node.terminal] if node.terminal else []
        for child in node.children.values():
            items.extend(self._build(child))
        node.top = tuple(ranked(items))
        return node.top

    def suggest(self, prefix, blocked=frozenset()):
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
        # Refill after immediate removal; subsequent offline snapshots exclude rules.
        items, stack = [], [node]
        while stack:
            current = stack.pop()
            if current.terminal and current.terminal[0] not in blocked:
                items.append(current.terminal)
                items = ranked(items)
            stack.extend(current.children.values())
        return tuple(ranked(items))
