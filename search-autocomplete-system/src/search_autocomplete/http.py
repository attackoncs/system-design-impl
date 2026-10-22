"""Bounded HTTP shard replicas and a version-fenced failover coordinator."""

import collections
import concurrent.futures
import http.server
import json
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request

import search_autocomplete.service
import search_autocomplete.store
import search_autocomplete.trie


class Coordinator:
    """A bounded replica client merging one consistent generation of shard
    results.
    """

    def __init__(self, store, replicas, timeout=0.5, concurrency=16):
        if timeout <= 0 or not 1 <= concurrency <= 256:
            raise ValueError("invalid coordinator bounds")
        self.store, self.replicas, self.timeout = store, replicas, timeout
        if any(
            (not isinstance(key, int) or isinstance(key, bool))
            or not 0 <= key < 256
            or not isinstance(endpoints, list)
            or not 1 <= len(endpoints) <= 8
            or any(
                not isinstance(url, str)
                or urllib.parse.urlsplit(url).scheme not in ("http", "https")
                or not urllib.parse.urlsplit(url).hostname
                for url in endpoints
            )
            for key, endpoints in replicas.items()
        ):
            raise ValueError("invalid replica map")
        self.snapshots = collections.OrderedDict()
        self.lock = threading.RLock()
        self.pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=concurrency
        )

    def close(self):
        self.pool.shutdown(wait=True)

    def _query(self, shard, prefix, version, revision):
        params = urllib.parse.urlencode(
            {
                "prefix": prefix,
                "version": version,
                "revision": revision,
                "shard": shard,
            }
        )
        for endpoint in self.replicas.get(shard, []):
            try:
                with urllib.request.urlopen(
                    endpoint.rstrip("/") + "/suggest?" + params,
                    timeout=self.timeout,
                ) as response:
                    raw = response.read(65537)
                if len(raw) > 65536:
                    continue
                result = json.loads(raw)
                if (result["version"], result["revision"], result["shard"]) != (
                    version,
                    revision,
                    shard,
                ):
                    continue
                items = result["suggestions"]
                if not isinstance(items, list) or len(items) > 5:
                    continue
                values = []
                for item in items:
                    query, frequency = item["query"], item["frequency"]
                    if (
                        search_autocomplete.trie.normalize(query) != query
                        or not query.startswith(prefix)
                        or (
                            not isinstance(frequency, int)
                            or isinstance(frequency, bool)
                        )
                        or frequency <= 0
                    ):
                        raise ValueError("invalid replica candidate")
                    values.append((query, frequency))
                if len({query for query, _ in values}) != len(
                    values
                ) or values != search_autocomplete.trie.ranked(values):
                    continue
                return values
            except (
                urllib.error.URLError,
                OSError,
                ValueError,
                KeyError,
                TypeError,
            ):
                continue
        raise search_autocomplete.store.Unavailable(
            "required shard has no valid replica"
        )

    def suggest(self, prefix):
        """Return one version-fenced set of prefix suggestions from remote
        shards.

        Args:
            prefix: English query prefix.

        Returns:
            A tuple of (snapshot generation, filter revision, ranked
            suggestions).

        Raises:
            Unavailable: A shard has no valid replica or the filter changes
                mid-query.
            ValueError: The prefix is invalid.
        """
        prefix = search_autocomplete.trie.normalize(prefix, empty=True)
        version, revision, blocked = self.store.view()
        with self.lock:
            if version not in self.snapshots:
                self.snapshots[version] = self.store.snapshot(version)
            snapshot = self.snapshots[version]
            self.snapshots.move_to_end(version)
            while len(self.snapshots) > 2:
                self.snapshots.popitem(last=False)
        futures = [
            self.pool.submit(self._query, shard, prefix, version, revision)
            for shard in search_autocomplete.service.relevant_shards(
                snapshot, prefix
            )
        ]
        candidates = []
        for future in futures:
            candidates.extend(future.result())
        # Fence rule changes while the remote work was in flight.
        if self.store.view()[1] != revision:
            raise search_autocomplete.store.Unavailable(
                "filter changed during query; retry"
            )
        if any(query in blocked for query, _ in candidates):
            raise search_autocomplete.store.Unavailable(
                "replica returned blocked suggestion"
            )
        return (
            version,
            revision,
            tuple(search_autocomplete.trie.ranked(candidates)),
        )


class BoundedHTTPServer(http.server.ThreadingHTTPServer):
    """A threaded HTTP server with 64 request slots and bounded socket
    waits.
    """

    request_queue_size = 128
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(64)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nContent-Length:"
                    b" 0\r\nConnection: close\r\n\r\n"
                )
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


def server(service, host="127.0.0.1", port=0, shard=None):
    """Create an autocomplete HTTP server without starting its serving loop.

    Args:
        service: Local Autocomplete service or remote Coordinator.
        host: Bind address; defaults to loopback.
        port: TCP port, or zero to request an available port.
        shard: Local shard restriction, or None for unrestricted serving.

    Returns:
        The bound BoundedHTTPServer; the caller owns shutdown and server_close.
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        """An HTTP request handler bound to the enclosing service instance."""

        def log_message(self, *_):
            pass

        def do_GET(self):
            status = 200
            try:
                if len(self.path) > 2048:
                    raise ValueError("request target too long")
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path != "/suggest":
                    self.send_error(404)
                    return
                params = urllib.parse.parse_qs(
                    parsed.query, keep_blank_values=True, max_num_fields=8
                )
                if any(len(values) != 1 for values in params.values()):
                    raise ValueError("duplicate parameter")
                prefix = params.get("prefix", [""])[0]
                if isinstance(service, Coordinator):
                    version, revision, suggestions = service.suggest(prefix)
                else:
                    version = (
                        int(params["version"][0])
                        if "version" in params
                        else service.store.view()[0]
                    )
                    revision = (
                        int(params["revision"][0])
                        if "revision" in params
                        else service.store.view()[1]
                    )
                    requested = (
                        int(params["shard"][0]) if "shard" in params else shard
                    )
                    if requested != shard:
                        raise ValueError("wrong shard endpoint")
                    suggestions = service.suggest(
                        prefix, version, revision, shard
                    )
                body = {
                    "version": version,
                    "revision": revision,
                    "shard": shard,
                    "suggestions": [
                        {"query": query, "frequency": frequency}
                        for query, frequency in suggestions
                    ],
                }
            except (ValueError, KeyError):
                status, body = 400, {"error": "invalid request"}
            except (search_autocomplete.store.Unavailable, sqlite3.Error):
                status, body = 503, {
                    "error": "required generation or shard unavailable; retry"
                }
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            # Do not let browser caching hide immediate operator removals.
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except OSError:
                pass

    return BoundedHTTPServer((host, port), Handler)
