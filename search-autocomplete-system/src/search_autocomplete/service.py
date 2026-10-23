"""Version-fenced serving, bounded LRU and balanced lexical shard routing."""

import collections
import threading

import search_autocomplete.store
import search_autocomplete.trie


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


class Autocomplete:
    """Versioned local trie serving with bounded snapshot and result caches."""

    def __init__(self, store, cache_size=1000):
        """Attach a snapshot store and set the maximum number of cached
        results.
        """
        if (
            not isinstance(cache_size, int) or isinstance(cache_size, bool)
        ) or cache_size < 0:
            raise ValueError("invalid cache capacity")
        self.store, self.cache_size = store, cache_size
        self.lock = threading.RLock()
        self.tries = collections.OrderedDict()
        self.snapshots = collections.OrderedDict()
        self.cache = collections.OrderedDict()

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
        active, actual_revision, blocked = self.store.view()
        version = active if version is None else version
        if revision is not None and revision != actual_revision:
            raise search_autocomplete.store.Unavailable(
                "filter revision changed; retry"
            )
        key = (version, actual_revision, shard, prefix)
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            if version not in self.snapshots:
                self.snapshots[version] = self.store.snapshot(version)
            self.snapshots.move_to_end(version)
            snapshot = self.snapshots[version]
            while len(self.snapshots) > 2:
                self.snapshots.popitem(last=False)
            indices = relevant_shards(snapshot, prefix) if shard is None else [shard]
            if any(
                (not isinstance(i, int) or isinstance(i, bool))
                or not 0 <= i < len(snapshot["shards"])
                for i in indices
            ):
                raise ValueError("invalid shard")
            candidates = []
            for i in indices:
                trie_key = (version, i)
                if trie_key not in self.tries:
                    self.tries[trie_key] = search_autocomplete.trie.Trie(
                        snapshot["shards"][i]
                    )
                self.tries.move_to_end(trie_key)
                trie = self.tries[trie_key]
                while len(self.tries) > 256:
                    self.tries.popitem(last=False)
                candidates.extend(trie.suggest(prefix, blocked))
            result = tuple(search_autocomplete.trie.ranked(candidates))
            if self.cache_size:
                self.cache[key] = result
                while len(self.cache) > self.cache_size:
                    self.cache.popitem(last=False)
            return result
