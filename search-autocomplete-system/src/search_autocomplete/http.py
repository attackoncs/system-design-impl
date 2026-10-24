"""Bounded HTTP shard replicas and a version-fenced failover coordinator."""

import collections
import concurrent.futures
import contextlib
import http.client
import http.server
import json
import math
import socket
import sqlite3
import threading
import time
import urllib.parse

import search_autocomplete.service
import search_autocomplete.store
import search_autocomplete.trie


def _positive_seconds(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


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
            if time.monotonic() >= deadline:
                raise TimeoutError("replica deadline expired")
            connection.request("GET", target, headers={"Accept": "application/json"})
            if time.monotonic() >= deadline:
                raise TimeoutError("replica deadline expired")
            received = connection.getresponse()
            with state_lock:
                response = received
            if response.status != 200:
                raise ValueError("replica did not return success")
            raw = bytearray()
            while len(raw) <= 65536:
                if response.fp is None:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("replica deadline expired")
                # read1 returns after one buffered/raw read, preventing a slow
                # stream from resetting a per-socket timeout indefinitely.
                response.fp.raw._sock.settimeout(remaining)
                part = response.read1(min(8192, 65537 - len(raw)))
                if not part:
                    break
                raw.extend(part)
            if len(raw) > 65536:
                raise ValueError("replica response too large")
            return json.loads(raw)
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
            bounds = snapshot["lower"]
            for shard in search_autocomplete.service.relevant_shards(snapshot, prefix):
                futures.append(
                    self._submit(
                        shard,
                        prefix,
                        version,
                        revision,
                        bounds[shard],
                        bounds[shard + 1] if shard + 1 < len(bounds) else None,
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


class BoundedHTTPServer(http.server.ThreadingHTTPServer):
    """Threaded HTTP server with 64 request slots and bounded socket waits."""

    request_queue_size = 128
    # server_close joins handlers before the caller closes their service/store.
    daemon_threads = False

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(64)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(0.1)
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n"
                    b"Cache-Control: no-store\r\nConnection: close\r\n\r\n"
                )
                request.shutdown(socket.SHUT_WR)
                # Drain already-sent request data so Windows does not replace
                # the overload response with a connection reset on close.
                request.recv(8192)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(3)
        return connection, address


def _parameters(target):
    if len(target) > 2048:
        raise ValueError("request target too long")
    parsed = urllib.parse.urlsplit(target)
    if parsed.path != "/suggest":
        return None
    params = urllib.parse.parse_qs(
        parsed.query, keep_blank_values=True, max_num_fields=8
    )
    if any(len(values) != 1 for values in params.values()) or (
        set(params) - {"prefix", "version", "revision", "shard"}
    ):
        raise ValueError("invalid parameter")
    return {name: values[0] for name, values in params.items()}


def _integer(params, name, default):
    if name not in params:
        return default
    value = params[name]
    if not value or not value.isascii() or not value.isdecimal():
        raise ValueError("invalid integer parameter")
    return int(value)


def _suggest(service, params, shard):
    prefix = params.get("prefix", "")
    if isinstance(service, Coordinator):
        if set(params) - {"prefix"}:
            raise ValueError("coordinator selects the serving generation")
        version, revision, suggestions = service.suggest(prefix)
    else:
        active, actual_revision, _ = service.store.view()
        version = _integer(params, "version", active)
        revision = _integer(params, "revision", actual_revision)
        requested = _integer(params, "shard", shard)
        if requested != shard:
            raise ValueError("wrong shard endpoint")
        suggestions = service.suggest(prefix, version, revision, shard)
    return {
        "version": version,
        "revision": revision,
        "shard": shard,
        "suggestions": [
            {"query": query, "frequency": frequency} for query, frequency in suggestions
        ],
    }


def server(service, host="127.0.0.1", port=0, shard=None):
    """Bind a server; its caller owns serve_forever, shutdown and server_close."""
    if shard is not None and (type(shard) is not int or not 0 <= shard < 256):
        raise ValueError("invalid shard")

    class Handler(http.server.BaseHTTPRequestHandler):
        """Thin request adapter; generation selection and parsing are testable."""

        timeout = 3.0

        def log_message(self, *_):
            pass

        def do_GET(self):
            status = 200
            try:
                params = _parameters(self.path)
                if params is None:
                    status, body = 404, {"error": "unknown endpoint"}
                else:
                    body = _suggest(service, params, shard)
            except (ValueError, KeyError):
                status, body = 400, {"error": "invalid request"}
            except (search_autocomplete.store.Unavailable, sqlite3.Error):
                status, body = (
                    503,
                    {"error": "required generation or shard unavailable; retry"},
                )
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            # A version-pinned browser cache would still hide new removal rules.
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            with contextlib.suppress(OSError):
                self.wfile.write(payload)

    return BoundedHTTPServer((host, port), Handler)
