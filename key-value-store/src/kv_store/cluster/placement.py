"""Deterministic preference lists shared by coordination and replica repair."""

from __future__ import annotations


class ReplicaPlacement:
    """Prefer distinct configured data centers, then fill in ring order.

    Every node must use the same data-center map and vnode counts. Unknown
    nodes share one unknown failure domain; they are not assumed independent.
    """

    def __init__(self, ring, data_centers):
        self._ring = ring
        self._data_centers = dict(data_centers)

    @property
    def nodes(self):
        return self._ring.nodes

    def get_nodes(self, key, count):
        order = self._ring.get_nodes(key, len(self._ring.nodes))
        diverse, remaining, seen = [], [], set()
        for node in order:
            domain = self._data_centers.get(node, "")
            if domain in seen:
                remaining.append(node)
            else:
                seen.add(domain)
                diverse.append(node)
        return (diverse + remaining)[:count]
