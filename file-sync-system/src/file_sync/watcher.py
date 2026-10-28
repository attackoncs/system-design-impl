"""Portable bounded directory scanning and durable notification synchronization."""

import math
import os
import threading


def _duration(value, name, *, maximum=None):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        valid = False
    if not valid or maximum is not None and value > maximum:
        raise ValueError(f"invalid {name}")
    return value


class SyncWatcher:
    """Synchronize one workspace using scans, not a native OS watcher.

    Each cycle scans up to max_files filesystem entries, pushes dirty/new files
    and local deletes, then applies notifications. Work is serial and bounded;
    rapid rewrites are retried on the next cycle. Preserved local-conflict copies
    remain local until explicitly pushed or renamed by the user.
    """

    def __init__(self, client, space, *, interval=1, poll_wait=1, max_files=10000):
        self.interval = _duration(interval, "scan interval", maximum=86400)
        self.poll_wait = _duration(poll_wait, "poll wait", maximum=25)
        if type(max_files) is not int or not 1 <= max_files <= 1000000:
            raise ValueError("invalid scan capacity")
        self.client, self.space, self.max_files = client, space, max_files
        self._running = threading.Lock()
        client.bind_space(space)

    def _scan(self, stop):
        client = self.client
        protected = {
            row[0] for row in client.db.execute("SELECT path FROM local_copies")
        }
        paths, pending, visited = [], [client.root], 0
        while pending and not stop.is_set():
            folder = pending.pop()
            with os.scandir(folder) as entries:
                for item in entries:
                    visited += 1
                    if visited > self.max_files:
                        raise OSError("directory scan exceeds configured capacity")
                    if item.name.casefold() == ".sync" or item.is_symlink():
                        continue
                    relative = os.path.relpath(item.path, client.root).replace(
                        os.sep, "/"
                    )
                    if relative in protected:
                        continue
                    client.path(relative)
                    if item.is_dir(follow_symlinks=False):
                        pending.append(client.root / relative)
                    elif item.is_file(follow_symlinks=False):
                        paths.append(relative)
        return paths

    def run_once(self, stop=None):
        """Scan/apply one cycle; fail after allowing independent files and pull."""
        stop = threading.Event() if stop is None else stop
        client = self.client
        # Coordinate with manual client operations and its close(). The request
        # lock is reentrant, and SQLite ownership spans this complete cycle.
        with client._lock:
            if client._closed:
                raise RuntimeError("device client is closed")
            paths = self._scan(stop)
            existing = set(paths)
            rows = client.db.execute(
                "SELECT path,deleted FROM files WHERE space=?", (self.space,)
            ).fetchall()
            report = {"pushed": 0, "deleted": 0, "events": 0}
            failure = None
            for relative in paths:
                if stop.is_set():
                    return report
                try:
                    result = client.push(self.space, relative)
                    report["pushed"] += not result.get("unchanged", False)
                except OSError as error:
                    failure = failure or error
            for relative, deleted in rows:
                if stop.is_set():
                    return report
                # Recheck the actual path: a scan excludes symlinks and protected
                # copies, and must never misinterpret them as an ordinary delete.
                path = client.path(relative)
                if not deleted and relative not in existing and not path.exists():
                    try:
                        client.delete(self.space, relative)
                        report["deleted"] += 1
                    except OSError as error:
                        failure = failure or error
            if not stop.is_set():
                report["events"] = len(client.pull(wait=self.poll_wait)["events"])
            if failure is not None:
                raise failure
            return report

    def run(self, stop=None, *, on_error=None):
        """Run until stopped, with interruptible waits and capped retry backoff.

        Stop drains the current request/file operation; it cannot cancel system
        DNS resolution or a blocked filesystem call. Callback failures propagate.
        """
        if not self._running.acquire(blocking=False):
            raise RuntimeError("watcher already has a running loop")
        stop = threading.Event() if stop is None else stop
        failures = 0
        try:
            while not stop.is_set():
                try:
                    self.run_once(stop)
                except OSError as error:
                    failures += 1
                    if on_error is not None:
                        on_error(error)
                    delay = min(30, self.interval * 2 ** min(failures - 1, 10))
                else:
                    failures = 0
                    delay = self.interval
                if stop.wait(delay):
                    return
        finally:
            self._running.release()
