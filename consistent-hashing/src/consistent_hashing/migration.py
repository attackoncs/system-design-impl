"""Topology-only migration planning; no key scan or data transfer is performed."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

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


def affected_ranges(before: RingSnapshot, after: RingSnapshot) -> list[AffectedRange]:
    """Return disjoint changed intervals, including wraparound and empty rings.

    Both views must use the same hash callable. Custom callable equivalence
    cannot be established safely. Intervals may be adjacent; they are not merged.
    The planner compares primary owners, not replica sets or physical data.
    """
    if before._hash_function is not after._hash_function:
        raise ValueError("snapshots must use the same hash function instance")
    boundaries = sorted(set(before._state.positions) | set(after._state.positions))

    def owner(snapshot, position):
        state = snapshot._state
        if not state.entries:
            return None
        index = bisect_right(state.positions, position) % len(state.entries)
        return state.entries[index][1]

    changed = []
    for index, start in enumerate(boundaries):
        end = boundaries[(index + 1) % len(boundaries)]
        old, new = owner(before, start), owner(after, start)
        if old != new:
            changed.append(AffectedRange(start, end, old, new))
    return changed
