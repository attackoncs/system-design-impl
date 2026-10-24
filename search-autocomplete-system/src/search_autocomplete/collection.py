"""Stable sampling of completed searches, separate from suggestion traffic."""

import hashlib
import math

from search_autocomplete.trie import normalize


class Collector:
    """Record one in N events; retries select the same event across processes.

    Keep sample_every unchanged for a log stream. Frequencies remain sampled
    counts, not estimates multiplied by the sampling factor.
    """

    def __init__(self, store, sample_every=1):
        if (
            not isinstance(sample_every, int)
            or isinstance(sample_every, bool)
            or sample_every < 1
        ):
            raise ValueError("sample_every must be a positive integer")
        self.store = store
        self.sample_every = sample_every

    def selected(self, event_id):
        """Select by stable event identity, rather than biased query text."""
        if not isinstance(event_id, str) or not 1 <= len(event_id) <= 200:
            raise ValueError("invalid event ID")
        digest = hashlib.blake2b(event_id.encode("utf-8"), digest_size=16).digest()
        return int.from_bytes(digest, "big") % self.sample_every == 0

    def record(self, event_id, query, at=None):
        """Return True only when a selected event was newly persisted."""
        selected = self.selected(event_id)
        query = normalize(query)
        if at is not None and (
            not isinstance(at, (int, float))
            or isinstance(at, bool)
            or not math.isfinite(at)
            or at < 0
        ):
            raise ValueError("invalid event time")
        return selected and self.store.record(event_id, query, at)
