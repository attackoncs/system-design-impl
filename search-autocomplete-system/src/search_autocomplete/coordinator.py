"""Version-fenced fan-out to HTTP shard replicas with bounded failover."""

import collections
import concurrent.futures
import contextlib
import http.client
import json
import socket
import threading
import time
import urllib.parse

import search_autocomplete.service
import search_autocomplete.store
import search_autocomplete.trie
from search_autocomplete.validation import finite_number

_MAX_REPLY_BYTES = 65536


def _positive_seconds(value):
    return finite_number(value) and value > 0


def _expired(deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError("replica deadline expired")


def _valid_endpoint(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
        return (
            parsed.scheme in ("http", "https")
            and bool(parsed.hostname)
            and parsed.port != 0
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


def _validate_reply(result, shard, prefix, version, revision, lower, upper):
    """Validate both the generation fence and the shard's lexical ownership."""
    if not isinstance(result, dict) or any(
        type(result.get(name)) is not int or result[name] != expected
        for name, expected in (
            ("version", version),
            ("revision", revision),
            ("shard", shard),
        )
    ):
        raise ValueError("invalid replica generation")
    items = result.get("suggestions")
    if not isinstance(items, list) or len(items) > 5:
        raise ValueError("invalid replica suggestions")
    values = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("invalid replica candidate")
        query, frequency = item.get("query"), item.get("frequency")
        if (
            search_autocomplete.trie.normalize(query) != query
            or not query.startswith(prefix)
            or query < lower
            or (upper is not None and query >= upper)
            or type(frequency) is not int
            or frequency <= 0
        ):
            raise ValueError("invalid replica candidate")
        values.append((query, frequency))
    if len({query for query, _ in values}) != len(values) or (
        values != search_autocomplete.trie.ranked(values)
    ):
        raise ValueError("invalid replica ranking")
    return values


def _read_body(response, deadline):
    """Read a bounded, complete body without letting a slow stream reset its wait."""
    raw = bytearray()
    while len(raw) <= _MAX_REPLY_BYTES:
        if response.fp is None:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("replica deadline expired")
        # read1 returns after one buffered/raw read, preventing a slow
        # stream from resetting a per-socket timeout indefinitely.
        response.fp.raw._sock.settimeout(remaining)
        part = response.read1(min(8192, _MAX_REPLY_BYTES + 1 - len(raw)))
        if not part:
            break
        raw.extend(part)
    if len(raw) > _MAX_REPLY_BYTES:
        raise ValueError("replica response too large")
    if response.length not in (None, 0):
        # read1() permits early EOF even when Content-Length remains.
        # A parseable JSON prefix does not make that response complete.
        raise http.client.IncompleteRead(bytes(raw), response.length)
    return raw


class Coordinator:
    """Merge one consistent generation with bounded outstanding shard work.

    ``timeout`` bounds each replica attempt; ``request_timeout`` bounds the
    admission and all failover attempts within one budget. Metadata reads
    subtract from that budget but SQLite/Store lock waits can outlive it. Timed-out
    running work retains its slot until it exits, so repeated client timeouts
    cannot grow the executor queue. ``close`` cancels queued work and joins
    owned workers. Platform DNS resolution may outlive a request deadline.
    """

    def __init__(
        self, store, replicas, timeout=0.5, concurrency=16, *, request_timeout=None
    ):
        if not _positive_seconds(timeout):
            raise ValueError("invalid coordinator timeout")
        request_timeout = timeout * 2 if request_timeout is None else request_timeout
        if (
            not _positive_seconds(request_timeout)
            or type(concurrency) is not int
            or not 1 <= concurrency <= 256
        ):
            raise ValueError("invalid coordinator bounds")
        if not isinstance(replicas, dict) or any(
            type(key) is not int
            or not 0 <= key < 256
            or not isinstance(endpoints, list)
            or not 1 <= len(endpoints) <= 8
            or any(not _valid_endpoint(url) for url in endpoints)
            for key, endpoints in replicas.items()
        ):
            raise ValueError("invalid replica map")
        self.store, self.timeout, self.request_timeout = store, timeout, request_timeout
        self.replicas = {key: list(endpoints) for key, endpoints in replicas.items()}
        self.snapshots = collections.OrderedDict()
        self.lock = threading.RLock()
        self.slots = threading.BoundedSemaphore(concurrency)
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=concurrency)
        self._closed = False
        self._futures = set()

    def close(self):
        """Reject new requests, cancel pending work and drain owned workers."""
        with self.lock:
            self._closed = True
        self.pool.shutdown(wait=True, cancel_futures=True)

    def _snapshot(self, version):
        with self.lock:
            snapshot = self.snapshots.get(version)
            if snapshot is not None:
                self.snapshots.move_to_end(version)
                return snapshot
        # Reading SQLite must not block every unrelated cache hit or completion.
        loaded = self.store.snapshot(version)
        with self.lock:
            snapshot = self.snapshots.setdefault(version, loaded)
            self.snapshots.move_to_end(version)
            while len(self.snapshots) > 2:
                self.snapshots.popitem(last=False)
            return snapshot

    def _completed(self, future):
        with self.lock:
            self._futures.discard(future)
        self.slots.release()

    def _submit(self, *args, deadline):
        if not self.slots.acquire(timeout=max(0, deadline - time.monotonic())):
            raise search_autocomplete.store.Unavailable("coordinator is busy; retry")
        with self.lock:
            if self._closed or time.monotonic() >= deadline:
                self.slots.release()
                raise search_autocomplete.store.Unavailable("coordinator unavailable")
            try:
                future = self.pool.submit(self._query, *args, deadline)
            except BaseException:
                self.slots.release()
                raise
            self._futures.add(future)
            future.add_done_callback(self._completed)
            return future

    def _read_reply(self, endpoint, params, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("replica deadline expired")
        parsed = urllib.parse.urlsplit(endpoint)
        connection_type = (
            http.client.HTTPSConnection
            if parsed.scheme == "https"
            else http.client.HTTPConnection
        )
        # Direct connections preserve normal Host, SNI and certificate checks;
        # no environment proxy or automatic redirect changes the replica target.
        connection = connection_type(parsed.hostname, parsed.port, timeout=remaining)
        response = None
        state_lock = threading.Lock()

        def interrupt():
            # Socket shutdown interrupts reads even when headers arrive a byte
            # at a time. Merely setting a per-read timeout would not do that.
            with state_lock:
                active_socket = connection.sock
                active_file = response.fp if response is not None else None
                if active_file is not None:
                    active_socket = active_file.raw._sock
                if active_socket is not None:
                    with contextlib.suppress(OSError):
                        active_socket.shutdown(socket.SHUT_RDWR)

        timer = threading.Timer(remaining, interrupt)
        timer.daemon = True
        timer.start()
        try:
            target = parsed.path.rstrip("/") + "/suggest?" + params
            connection.connect()
            _expired(deadline)
            connection.request("GET", target, headers={"Accept": "application/json"})
            _expired(deadline)
            received = connection.getresponse()
            with state_lock:
                response = received
            if response.status != 200:
                raise ValueError("replica did not return success")
            return json.loads(_read_body(response, deadline))
        finally:
            timer.cancel()
            timer.join()
            if response is not None:
                response.close()
            connection.close()

    def _query(self, shard, prefix, version, revision, lower, upper, deadline):
        params = urllib.parse.urlencode(
            {"prefix": prefix, "version": version, "revision": revision, "shard": shard}
        )
        endpoints = tuple(self.replicas.get(shard, ()))
        for index, endpoint in enumerate(endpoints):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Reserve time for the remaining failover choices as well.
            attempt = min(self.timeout, remaining / (len(endpoints) - index))
            try:
                result = self._read_reply(
                    endpoint, params, min(deadline, time.monotonic() + attempt)
                )
                return _validate_reply(
                    result, shard, prefix, version, revision, lower, upper
                )
            except (
                OSError,
                http.client.HTTPException,
                ValueError,
                KeyError,
                TypeError,
                RecursionError,
            ):
                continue
        raise search_autocomplete.store.Unavailable(
            "required shard has no valid replica"
        )

    def suggest(self, prefix):
        """Return (generation, filter revision, ranked suggestions) or fail closed."""
        deadline = time.monotonic() + self.request_timeout
        prefix = search_autocomplete.trie.normalize(prefix, empty=True)
        with self.lock:
            if self._closed:
                raise search_autocomplete.store.Unavailable("coordinator is closed")
        version, revision, blocked = self.store.view()
        if version == 0:
            raise search_autocomplete.store.Unavailable("no active generation")
        snapshot = self._snapshot(version)
        futures = []
        try:
            for shard in search_autocomplete.service.relevant_shards(snapshot, prefix):
                lower, upper = search_autocomplete.service.shard_range(snapshot, shard)
                futures.append(
                    self._submit(
                        shard,
                        prefix,
                        version,
                        revision,
                        lower,
                        upper,
                        deadline=deadline,
                    )
                )
            done, pending = concurrent.futures.wait(
                futures,
                timeout=max(0, deadline - time.monotonic()),
                return_when=concurrent.futures.FIRST_EXCEPTION,
            )
            if pending or any(future.cancelled() for future in done):
                raise search_autocomplete.store.Unavailable(
                    "replica request expired; retry"
                )
            candidates = [item for future in futures for item in future.result()]
            if time.monotonic() >= deadline or self.store.revision() != revision:
                raise search_autocomplete.store.Unavailable(
                    "serving view changed or expired; retry"
                )
            if any(query in blocked for query, _ in candidates):
                raise search_autocomplete.store.Unavailable(
                    "replica returned blocked suggestion"
                )
            return version, revision, tuple(search_autocomplete.trie.ranked(candidates))
        finally:
            # Running I/O remains bounded and owned by this coordinator; slots
            # are released by completion callbacks, never prematurely here.
            for future in futures:
                future.cancel()
