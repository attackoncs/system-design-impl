"""Version-fenced serving, bounded LRU and balanced lexical shard routing."""

import collections
import threading

import search_autocomplete.store
import search_autocomplete.trie
from search_autocomplete.validation import require_integer

_MISSING = object()


def _require_revision(expected, actual):
    if expected != actual:
        raise search_autocomplete.store.Unavailable("filter revision changed; retry")


def relevant_shards(snapshot, prefix):
    """Return lexical shard indices intersecting the prefix's key range."""
    if not prefix:
        return []
    upper = prefix + "{"  # All supported suffix characters sort below '{'.
    bounds = snapshot["lower"]
    return [
        i
        for i, lower in enumerate(bounds)
        if lower < upper and (i + 1 == len(bounds) or bounds[i + 1] > prefix)
    ]


def shard_range(snapshot, index):
    """Return a shard's inclusive lower and exclusive upper bound (None = open)."""
    bounds = snapshot["lower"]
    return bounds[index], bounds[index + 1] if index + 1 < len(bounds) else None


class Autocomplete:
    """Versioned local trie serving with bounded snapshot and result caches."""

    def __init__(self, store, cache_size=1000):
        """Attach a snapshot store and set the maximum number of cached
        results.
        """
        require_integer(cache_size, 0, message="invalid cache capacity")
        self.store, self.cache_size = store, cache_size
        self.lock = threading.RLock()
        self.tries = collections.OrderedDict()
        self.snapshots = collections.OrderedDict()
        self.cache = collections.OrderedDict()
        # Fixed-size stripes coalesce cold loads without growing a lock per prefix.
        # Warm readers use only the small LRU lock, even during a cold trie build.
        self._loads = tuple(threading.RLock() for _ in range(64))

    def _get(self, cache, key):
        with self.lock:
            value = cache.get(key, _MISSING)
            if value is not _MISSING:
                cache.move_to_end(key)
            return value

    def _put(self, cache, key, value, capacity):
        with self.lock:
            if capacity:
                cache[key] = value
                cache.move_to_end(key)
                while len(cache) > capacity:
                    cache.popitem(last=False)
        return value

    def _load(self, cache, key, capacity, factory):
        value = self._get(cache, key)
        if value is not _MISSING:
            return value
        with self._loads[hash((id(cache), key)) % len(self._loads)]:
            value = self._get(cache, key)
            if value is _MISSING:
                value = self._put(cache, key, factory(), capacity)
            return value

    def _check_revision(self, revision):
        _require_revision(revision, self.store.revision())

    def _compute(self, version, shard, prefix, blocked):
        """Merge each relevant shard trie's filtered top five for one generation."""
        snapshot = self._load(
            self.snapshots, version, 2, lambda: self.store.snapshot(version)
        )
        indices = relevant_shards(snapshot, prefix) if shard is None else [shard]
        if any(i >= len(snapshot["shards"]) for i in indices):
            raise ValueError("invalid shard")
        candidates = []
        for index in indices:
            trie = self._load(
                self.tries,
                (version, index),
                256,
                # Resolved at call time so the module's Trie stays replaceable.
                lambda: search_autocomplete.trie.Trie(snapshot["shards"][index]),
            )
            candidates.extend(trie.suggest(prefix, blocked))
        return tuple(search_autocomplete.trie.ranked(candidates))

    def suggest(self, prefix, version=None, revision=None, shard=None):
        """Return five ranked matches from one consistent local serving view.

        Args:
            prefix: English query prefix to normalize.
            version: Snapshot generation; None selects the active generation.
            revision: Required filter revision, or None to accept the current
                one.
            shard: One lexical shard index; None searches all relevant shards.

        Returns:
            A tuple of (query, frequency) pairs excluding blocked queries.

        Raises:
            Unavailable: The snapshot is absent or the filter revision changed.
            ValueError: The prefix or shard index is invalid.
        """
        prefix = search_autocomplete.trie.normalize(prefix, empty=True)
        for value, label in (
            (version, "version"),
            (revision, "revision"),
            (shard, "shard"),
        ):
            if value is not None:
                require_integer(value, 0, message=f"invalid {label}")
        active, actual_revision, blocked = self.store.view()
        version = active if version is None else version
        if version == 0:
            raise search_autocomplete.store.Unavailable(
                "no snapshot has been published"
            )
        if revision is not None:
            _require_revision(revision, actual_revision)
        key = (version, actual_revision, shard, prefix)
        result = self._get(self.cache, key)
        if result is _MISSING:
            result = self._compute(version, shard, prefix, blocked)
        # Include cache hits: a rule may change while the captured view is served.
        self._check_revision(actual_revision)
        return self._put(self.cache, key, result, self.cache_size)
