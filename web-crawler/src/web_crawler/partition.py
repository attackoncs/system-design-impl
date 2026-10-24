"""Stable host assignment for crawler workers using a consistent hash ring.

The ring decides where a URL belongs. Transport, durable queues, and ownership
handoffs belong to the scheduler that uses this module.
"""

from __future__ import annotations

import hashlib
import ipaddress
from bisect import bisect_left
from collections.abc import Iterable
from dataclasses import dataclass, field
from threading import RLock
from urllib.parse import urlsplit


def _hash_bytes(value: bytes) -> int:
    return int.from_bytes(hashlib.sha256(value).digest(), "big")


def _validate_node_id(node_id: str) -> None:
    if not isinstance(node_id, str) or not node_id or node_id.strip() != node_id:
        raise ValueError("node IDs must be non-empty strings without outer whitespace")


def _host_for_url(url: str) -> str:
    if not isinstance(url, str) or not url or url.strip() != url:
        raise ValueError("url must be a non-empty HTTP or HTTPS URL")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        # Validate the port even though ownership is shared across all ports.
        _ = parsed.port
        if parsed.scheme.lower() not in ("http", "https") or not host:
            raise ValueError("url must contain an HTTP or HTTPS host")
        if any(character.isspace() for character in host):
            raise ValueError("url host cannot contain whitespace")
        host = host.rstrip(".")
        try:
            return ipaddress.ip_address(host).compressed.lower()
        except ValueError:
            if not host:
                raise ValueError("url must contain a host")
            return host.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError) as error:
        raise ValueError("url must be a valid HTTP or HTTPS URL") from error


@dataclass(frozen=True, order=True)
class _RingPoint:
    position: int
    node_id: str
    virtual_index: int


@dataclass(frozen=True)
class PartitionSnapshot:
    """Immutable topology suitable for routing a whole batch consistently."""

    nodes: tuple[str, ...]
    virtual_nodes: int
    _points: tuple[_RingPoint, ...] = field(repr=False)
    _positions: tuple[int, ...] = field(repr=False)

    @property
    def token_count(self) -> int:
        return len(self._points)

    def owners_for_url(self, url: str, count: int = 1) -> tuple[str, ...]:
        """Return clockwise, distinct physical workers, up to ``count``.

        Empty rings cannot route URLs. More requested owners than workers yields
        every worker once. Colliding tokens remain separate, with deterministic
        ordering by worker ID and virtual index.
        """
        if type(count) is not int or count < 1:
            raise ValueError("count must be a positive integer")
        host = _host_for_url(url)
        if not self._points:
            raise LookupError("cannot assign a URL without crawler workers")
        position = _hash_bytes(b"host\x00" + host.encode("ascii"))
        start = bisect_left(self._positions, position)
        owners = []
        selected = set()
        required = min(count, len(self.nodes))
        for offset in range(len(self._points)):
            point = self._points[(start + offset) % len(self._points)]
            if point.node_id not in selected:
                selected.add(point.node_id)
                owners.append(point.node_id)
                if len(owners) == required:
                    break
        return tuple(owners)

    def owner_for_url(self, url: str) -> str:
        """Return the primary worker for a URL's canonical hostname."""
        return self.owners_for_url(url)[0]


class ConsistentHashPartition:
    """Thread-safe, copy-on-write worker membership and host assignment.

    All workers must use the same node IDs and ``virtual_nodes`` value. A local
    node ID enables the ``owns`` predicate used by an individual crawler.
    """

    def __init__(
        self,
        nodes: Iterable[str],
        *,
        local_node_id: str | None = None,
        virtual_nodes: int = 128,
    ) -> None:
        if type(virtual_nodes) is not int or virtual_nodes < 1:
            raise ValueError("virtual_nodes must be a positive integer")
        if isinstance(nodes, (str, bytes)):
            raise TypeError("nodes must be an iterable of worker IDs")
        node_ids = tuple(nodes)
        for node_id in node_ids:
            _validate_node_id(node_id)
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("node IDs must be unique")
        if local_node_id is not None:
            _validate_node_id(local_node_id)
        self._local_node_id = local_node_id
        self._virtual_nodes = virtual_nodes
        self._lock = RLock()
        self._snapshot = self._build_snapshot(node_ids)

    def _build_snapshot(self, nodes: Iterable[str]) -> PartitionSnapshot:
        node_ids = tuple(sorted(nodes))
        points = []
        for node_id in node_ids:
            encoded = node_id.encode("utf-8")
            prefix = b"vnode\x00" + len(encoded).to_bytes(8, "big") + encoded
            for index in range(self._virtual_nodes):
                position = _hash_bytes(prefix + index.to_bytes(8, "big"))
                points.append(_RingPoint(position, node_id, index))
        ordered = tuple(sorted(points))
        return PartitionSnapshot(
            node_ids,
            self._virtual_nodes,
            ordered,
            tuple(point.position for point in ordered),
        )

    def snapshot(self) -> PartitionSnapshot:
        with self._lock:
            return self._snapshot

    def add_node(self, node_id: str) -> bool:
        """Publish a new topology; return false if the worker already exists."""
        _validate_node_id(node_id)
        with self._lock:
            if node_id in self._snapshot.nodes:
                return False
            self._snapshot = self._build_snapshot((*self._snapshot.nodes, node_id))
            return True

    def remove_node(self, node_id: str) -> bool:
        """Publish a new topology; return false if the worker is absent."""
        _validate_node_id(node_id)
        with self._lock:
            if node_id not in self._snapshot.nodes:
                return False
            self._snapshot = self._build_snapshot(
                existing for existing in self._snapshot.nodes if existing != node_id
            )
            return True

    def owner_for_url(self, url: str) -> str:
        return self.snapshot().owner_for_url(url)

    def owners_for_url(self, url: str, count: int = 1) -> tuple[str, ...]:
        return self.snapshot().owners_for_url(url, count)

    def owns(self, url: str) -> bool:
        """Whether this worker owns the URL under the current topology."""
        if self._local_node_id is None:
            raise ValueError("owns requires a local_node_id")
        return self.owner_for_url(url) == self._local_node_id
