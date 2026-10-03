"""Version-fenced serving, bounded LRU and balanced lexical shard routing."""
from collections import OrderedDict
import threading

from .store import Unavailable
from .trie import Trie, normalize, ranked


def relevant_shards(snapshot, prefix):
    if not prefix:
        return []
    upper = prefix + "{"  # All supported suffix characters sort below '{'.
    bounds = snapshot["lower"]
    return [i for i, lower in enumerate(bounds)
            if lower < upper and (i + 1 == len(bounds) or bounds[i + 1] > prefix)]


class Autocomplete:
    def __init__(self, store, cache_size=1000):
        if type(cache_size) is not int or cache_size < 0:
            raise ValueError("invalid cache capacity")
        self.store, self.cache_size = store, cache_size
        self.lock = threading.RLock()
        self.tries = OrderedDict()
        self.snapshots = OrderedDict()
        self.cache = OrderedDict()

    def suggest(self, prefix, version=None, revision=None, shard=None):
        prefix = normalize(prefix, empty=True)
        active, actual_revision, blocked = self.store.view()
        version = active if version is None else version
        if revision is not None and revision != actual_revision:
            raise Unavailable("filter revision changed; retry")
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
            if any(type(i) is not int or not 0 <= i < len(snapshot["shards"]) for i in indices):
                raise ValueError("invalid shard")
            candidates = []
            for i in indices:
                trie_key = (version, i)
                if trie_key not in self.tries:
                    self.tries[trie_key] = Trie(snapshot["shards"][i])
                self.tries.move_to_end(trie_key)
                trie = self.tries[trie_key]
                while len(self.tries) > 256:
                    self.tries.popitem(last=False)
                candidates.extend(trie.suggest(prefix, blocked))
            result = tuple(ranked(candidates))
            if self.cache_size:
                self.cache[key] = result
                while len(self.cache) > self.cache_size:
                    self.cache.popitem(last=False)
            return result
