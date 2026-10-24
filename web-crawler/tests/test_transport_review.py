"""Real transport checks for limits, DNS and cancellation boundaries."""

import asyncio
import concurrent.futures
import contextlib
import http.server
import socket
import threading
import time
import unittest.mock

import pytest

from web_crawler.dns import DNSCache
from web_crawler.exceptions import FetchError
from web_crawler.fetcher import URLLibFetcher, _CachedHTTPSConnection, _FetchControl


@pytest.fixture
def origin():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/host")
                self.end_headers()
                return
            if self.path == "/cross":
                self.send_response(302)
                self.send_header("Location", "http://other.invalid/forbidden")
                self.end_headers()
                return
            self.send_response(200)
            if self.path == "/declared-large":
                self.send_header("Content-Length", "1000000")
            if self.path == "/truncated":
                self.send_header("Content-Length", "64")
            self.end_headers()
            if self.path == "/slow":
                for _ in range(100):
                    try:
                        self.wfile.write(b"x")
                        self.wfile.flush()
                    except OSError:
                        break
                    time.sleep(0.02)
                return
            body = (
                self.headers.get("Host", "").encode()
                if self.path == "/host"
                else b"x" * 32
            )
            with contextlib.suppress(OSError):
                self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


async def test_dns_connection_keeps_http_host_and_reuses_resolution(origin):
    addresses = socket.getaddrinfo("127.0.0.1", origin, 0, socket.SOCK_STREAM)
    fetcher = URLLibFetcher()
    with unittest.mock.patch(
        "web_crawler.dns.socket.getaddrinfo", return_value=addresses
    ) as resolve:
        url = f"http://crawler.example:{origin}/host"
        first = await fetcher.fetch(url)
        second = await fetcher.fetch(url)
    assert first.body == second.body == f"crawler.example:{origin}".encode()
    assert first.url == url
    assert resolve.call_count == 1


def test_https_uses_original_host_for_sni_and_certificate_check():
    resolver = unittest.mock.Mock()
    sock = unittest.mock.Mock()
    resolver.create_connection.return_value = sock
    context = unittest.mock.Mock()
    context.wrap_socket.return_value = sock
    connection = _CachedHTTPSConnection(
        "secure.example",
        resolver=resolver,
        control=_FetchControl(time.monotonic() + 10),
        context=context,
        timeout=10,
    )
    connection.connect()
    context.wrap_socket.assert_called_once_with(sock, server_hostname="secure.example")
    assert resolver.create_connection.call_args.args[0] == ("secure.example", 443)


@pytest.mark.parametrize("path", ["/declared-large", "/undeclared-large"])
async def test_response_limit_rejects_declared_and_undeclared_bodies(origin, path):
    with pytest.raises(FetchError, match="max_response_bytes"):
        await URLLibFetcher(max_response_bytes=8).fetch(
            f"http://127.0.0.1:{origin}{path}"
        )


async def test_truncated_content_length_is_not_accepted_as_complete(origin):
    with pytest.raises(FetchError, match="Incomplete"):
        await URLLibFetcher().fetch(f"http://127.0.0.1:{origin}/truncated")


async def test_cross_origin_redirect_is_returned_for_scheduler(origin):
    response = await URLLibFetcher().fetch(f"http://127.0.0.1:{origin}/cross")
    assert response.status_code == 302
    assert response.headers["Location"] == "http://other.invalid/forbidden"


async def test_same_origin_redirect_limit(origin):
    with pytest.raises(FetchError, match="redirect"):
        await URLLibFetcher(max_redirects=0, follow_same_origin_redirects=True).fetch(
            f"http://127.0.0.1:{origin}/redirect"
        )


async def test_same_origin_redirect_is_deferred_unless_explicitly_enabled(origin):
    url = f"http://127.0.0.1:{origin}/redirect"
    assert (await URLLibFetcher().fetch(url)).status_code == 302
    followed = await URLLibFetcher(follow_same_origin_redirects=True).fetch(url)
    assert followed.status_code == 200
    assert followed.url.endswith("/host")


async def test_deadline_stops_drip_feed_and_slot_is_reused(origin):
    fetcher = URLLibFetcher(max_concurrent_downloads=1)
    started = time.monotonic()
    with pytest.raises(FetchError, match="timed out"):
        await fetcher.fetch(f"http://127.0.0.1:{origin}/slow", timeout=0.12)
    assert time.monotonic() - started < 1
    assert (
        await fetcher.fetch(f"http://127.0.0.1:{origin}/host", timeout=1)
    ).status_code == 200


async def test_cancelled_download_releases_slot_after_worker_exits(origin):
    fetcher = URLLibFetcher(max_concurrent_downloads=1)
    task = asyncio.create_task(fetcher.fetch(f"http://127.0.0.1:{origin}/slow"))
    await asyncio.sleep(0.06)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (
        await fetcher.fetch(f"http://127.0.0.1:{origin}/host", timeout=1)
    ).status_code == 200


async def test_timed_out_dns_lookup_keeps_its_work_slot_until_finished(origin):
    addresses = socket.getaddrinfo("127.0.0.1", origin, 0, socket.SOCK_STREAM)
    release = threading.Event()

    def stalled_lookup(*_):
        release.wait(2)
        return addresses

    fetcher = URLLibFetcher(max_concurrent_downloads=1)
    try:
        with unittest.mock.patch(
            "web_crawler.dns.socket.getaddrinfo", side_effect=stalled_lookup
        ) as lookup:
            with pytest.raises(FetchError, match="timed out"):
                await fetcher.fetch(
                    f"http://crawler.example:{origin}/host", timeout=0.05
                )
            with pytest.raises(FetchError, match="download slot"):
                await fetcher.fetch(
                    f"http://crawler.example:{origin}/host", timeout=0.05
                )
            assert lookup.call_count == 1
            release.set()
            assert (
                await fetcher.fetch(f"http://crawler.example:{origin}/host", timeout=1)
            ).status_code == 200
    finally:
        release.set()


def test_dns_cache_coalesces_refreshes_and_evicts():
    cache = DNSCache(ttl=10, capacity=1)
    addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
    with unittest.mock.patch(
        "web_crawler.dns.socket.getaddrinfo", return_value=addresses
    ) as resolve:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            assert all(pool.map(lambda _: cache.resolve("example.test", 80), range(32)))
        assert resolve.call_count == 1
        cache.resolve("second.test", 80)
        cache.resolve("example.test", 80)
        assert resolve.call_count == 3
        with unittest.mock.patch(
            "web_crawler.dns.time.monotonic", return_value=time.monotonic() + 11
        ):
            cache.resolve("example.test", 80)
        assert resolve.call_count == 4


@pytest.mark.parametrize(
    "url", ["file:///tmp/secret", "ftp://example.test/", "http://example.test:bad/"]
)
async def test_non_http_and_invalid_ports_rejected(url):
    with pytest.raises(FetchError):
        await URLLibFetcher().fetch(url)
