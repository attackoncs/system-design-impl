"""Topology-only migration planning; no key scan or data transfer is performed."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from itertools import groupby, repeat

from .ring import RingSnapshot


@dataclass(frozen=True)
class AffectedRange:
    """Changed clockwise interval [start, end); equal bounds mean the full ring.

    Bounds follow strict-clockwise lookup: a key exactly at a virtual-node
    position belongs to the next greater position. None denotes an empty ring.
    """

    start: int
    end: int
    previous_owner: str | None
    new_owner: str | None

    def contains(self, position: int) -> bool:
        if type(position) is not int:
            raise ValueError("position must be an integer")
        if self.start < self.end:
            return self.start <= position < self.end
        return position >= self.start or position < self.end


@dataclass(frozen=True)
class KeyMigration:
    """One candidate key whose primary assignment changes; no data is moved."""

    key: str
    previous_owner: str | None
    new_owner: str | None


def affected_ranges(before: RingSnapshot, after: RingSnapshot) -> list[AffectedRange]:
    """Return disjoint changed intervals, including wraparound and empty rings.

    Both views must use the same hash callable. Custom callable equivalence
    cannot be established safely. Intervals may be adjacent; they are not merged.
    The planner compares primary owners, not replica sets or physical data.
    """
    if not isinstance(before, RingSnapshot) or not isinstance(after, RingSnapshot):
        raise TypeError("affected_ranges requires RingSnapshot inputs")
    if before._hash_function is not after._hash_function:
        raise ValueError("snapshots must use the same hash function instance")
    # Both inputs are sorted runs; CPython's built-in sort merges these in C.
    # This avoids Python heap iteration while keeping collisions adjacent.
    boundaries = [
        position
        for position, _ in groupby(
            sorted(before._state.positions + after._state.positions)
        )
    ]
    changed = []
    owners = zip(
        _owners_at_boundaries(before, boundaries),
        _owners_at_boundaries(after, boundaries),
    )
    for index, (old, new) in enumerate(owners):
        if old != new:
            start = boundaries[index]
            end = boundaries[(index + 1) % len(boundaries)]
            changed.append(AffectedRange(start, end, old, new))
    return changed


def _owners_at_boundaries(
    snapshot: RingSnapshot, boundaries: list[int]
) -> Iterator[str | None]:
    """Advance each sorted topology once, skipping all equal-position entries."""
    entries, positions = snapshot._state.entries, snapshot._state.positions
    size = len(entries)
    if not size:
        yield from repeat(None, len(boundaries))
        return
    index = 0
    for start in boundaries:
        while index < size and positions[index] <= start:
            index += 1
        yield entries[index % size][1]


def iter_migrations(
    before: RingSnapshot, after: RingSnapshot, keys: Iterable[str]
) -> Iterator[KeyMigration]:
    """Plan now, lazily filter candidate keys with one hash and binary search.

    Both immutable snapshots must share the same hash callable. The ring has
    no key registry: callers supply candidates from their own index or scan.
    No-change plans do not consume candidates. Duplicate candidates are retained.
    """
    ranges = affected_ranges(before, after)
    if not ranges:
        return iter(())
    starts = tuple(interval.start for interval in ranges)

    def selected():
        for key in keys:
            position = before._hash(key)
            interval = ranges[bisect_right(starts, position) - 1]
            if interval.contains(position):
                yield KeyMigration(key, interval.previous_owner, interval.new_owner)

    return selected()
