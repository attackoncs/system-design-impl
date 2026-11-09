"""Stable sampling of completed searches, separate from suggestion traffic."""

import hashlib

from search_autocomplete.trie import normalize
from search_autocomplete.validation import (
    event_time,
    require_event_id,
    require_integer,
)


class Collector:
    """Record one in N events; retries select the same event across processes.

    Keep sample_every unchanged for a log stream. Frequencies remain sampled
    counts, not estimates multiplied by the sampling factor.
    """

    def __init__(self, store, sample_every=1):
        self.store = store
        self.sample_every = require_integer(
            sample_every, 1, message="sample_every must be a positive integer"
        )

    def selected(self, event_id):
        """Select by stable event identity, rather than biased query text."""
        require_event_id(event_id)
        digest = hashlib.blake2b(event_id.encode("utf-8"), digest_size=16).digest()
        return int.from_bytes(digest, "big") % self.sample_every == 0

    def record(self, event_id, query, at=None):
        """Return True only when a selected event was newly persisted."""
        selected = self.selected(event_id)
        query = normalize(query)
        if at is not None and not event_time(at):
            raise ValueError("invalid event time")
        return selected and self.store.record(event_id, query, at)
