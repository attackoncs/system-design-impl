"""Writer-preferred ownership for a node's active pack and disposable index."""

from contextlib import contextmanager
import threading


class NodeLock:
    """Share reads; serialize publication, recovery, collection and compaction.

    Writers can reenter and call read helpers. Read-to-write upgrades are
    deliberately rejected: callers must release their snapshot first.
    """

    def __init__(self):
        self._condition = threading.Condition(threading.RLock())
        self._writer = None
        self._depth = 0
        self._readers = {}
        self._waiting = 0

    def __enter__(self):
        owner = threading.get_ident()
        with self._condition:
            if self._writer == owner:
                self._depth += 1
                return self
            if owner in self._readers:
                raise RuntimeError("node lock cannot upgrade a read snapshot")
            self._waiting += 1
            try:
                while self._writer is not None or self._readers:
                    self._condition.wait()
                self._writer, self._depth = owner, 1
            finally:
                self._waiting -= 1
        return self

    def __exit__(self, *args):
        with self._condition:
            self._depth -= 1
            if not self._depth:
                self._writer = None
                self._condition.notify_all()

    @contextmanager
    def read(self):
        owner = threading.get_ident()
        with self._condition:
            while self._writer not in (None, owner) or (
                self._writer != owner and self._waiting and owner not in self._readers
            ):
                self._condition.wait()
            self._readers[owner] = self._readers.get(owner, 0) + 1
        try:
            yield self
        finally:
            with self._condition:
                remaining = self._readers[owner] - 1
                if remaining:
                    self._readers[owner] = remaining
                else:
                    del self._readers[owner]
                self._condition.notify_all()
