"""HTTP fetcher with bounded downloads and host-preserving DNS caching."""

from __future__ import annotations

import abc
import asyncio
import contextlib
import dataclasses
import http.client
import math
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional

import web_crawler.exceptions
from web_crawler.dns import DNSCache


@dataclasses.dataclass
class FetchResponse:
    """Response from an HTTP fetch operation."""

    status_code: int
    headers: Dict[str, str]
    body: bytes
    url: str


class Fetcher(abc.ABC):
    """Abstract interface for HTTP fetching."""

    @abc.abstractmethod
    async def fetch(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 30.0,
    ) -> FetchResponse:
        """Fetch a URL and return the response.

        Args:
            url: The URL to fetch.
            headers: Optional request headers.
            timeout: Request timeout in seconds.

        Returns:
            FetchResponse with status, headers, and body.

        Raises:
            FetchError: If the fetch fails.
        """


class _FetchControl:
    """Own a request's sockets and deadline across the worker thread boundary."""

    def __init__(self, deadline: float) -> None:
        self.deadline = deadline
        self._cancelled = False
        self._sockets: list[socket.socket] = []
        self._lock = threading.Lock()

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if self._cancelled or remaining <= 0:
            raise TimeoutError("Request deadline exceeded")
        return remaining

    def register_socket(self, sock: socket.socket) -> None:
        with self._lock:
            self._sockets.append(sock)
            if self._cancelled:
                sock.close()
                raise TimeoutError("Request cancelled")

    def set_read_timeout(self) -> None:
        remaining = self.remaining()
        with self._lock:
            # HTTPS replaces its raw socket after the TLS handshake. The latest
            # registered socket owns the current connection's file handle.
            for sock in reversed(self._sockets):
                if sock.fileno() >= 0:
                    sock.settimeout(remaining)
                    break

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            for sock in self._sockets:
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
                sock.close()


class _CachedConnectionMixin:
    def __init__(self, host, *, resolver, control, **kwargs) -> None:
        super().__init__(host, **kwargs)
        self._control = control
        self._create_connection = lambda address, timeout, source_address: (
            resolver.create_connection(
                address,
                timeout,
                source_address,
                remaining=control.remaining,
                register_socket=control.register_socket,
            )
        )

    def connect(self) -> None:
        super().connect()
        self._control.register_socket(self.sock)


class _CachedHTTPConnection(_CachedConnectionMixin, http.client.HTTPConnection):
    pass


class _CachedHTTPSConnection(_CachedConnectionMixin, http.client.HTTPSConnection):
    pass


class _DNSHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, resolver, local) -> None:
        super().__init__()
        self._resolver = resolver
        self._local = local

    def http_open(self, request):
        return self.do_open(
            lambda host, **kwargs: _CachedHTTPConnection(
                host, resolver=self._resolver, control=self._local.control, **kwargs
            ),
            request,
        )


class _DNSHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, resolver, local) -> None:
        super().__init__()
        self._resolver = resolver
        self._local = local

    def https_open(self, request):
        return self.do_open(
            lambda host, **kwargs: _CachedHTTPSConnection(
                host, resolver=self._resolver, control=self._local.control, **kwargs
            ),
            request,
            context=self._context,
        )


def _origin(url: str) -> tuple:
    parsed = urllib.parse.urlsplit(url)
    return (
        parsed.scheme.lower(),
        parsed.hostname,
        parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
    )


class _CrawlerRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Defer redirects crossing an origin to the crawler's robots/frontier."""

    def __init__(self, max_redirects: int, follow_same_origin: bool) -> None:
        self._max_redirects = max_redirects
        self._follow_same_origin = follow_same_origin

    def http_error_302(self, request, fp, code, msg, headers):
        location = headers.get("location") or headers.get("uri")
        if not location or not self._follow_same_origin:
            return None
        target = urllib.parse.urljoin(request.full_url, location)
        if _origin(request.full_url) != _origin(target):
            return None
        try:
            redirected = self.redirect_request(request, fp, code, msg, headers, target)
        finally:
            # Do not drain a redirect body: urllib's default handler reads it
            # without a size limit before following the next URL.
            fp.close()
        return self.parent.open(redirected, timeout=request.timeout)

    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302
    http_error_308 = http_error_302

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        if _origin(request.full_url) != _origin(newurl):
            return None
        count = getattr(request, "_crawler_redirects", 0)
        if count >= self._max_redirects:
            raise web_crawler.exceptions.FetchError(
                request.full_url, "Maximum redirect count exceeded"
            )
        redirected = super().redirect_request(request, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected._crawler_redirects = count + 1
        return redirected


class URLLibFetcher(Fetcher):
    """Fetch HTTP(S) in workers with bounded memory, work and request lifetime.

    Uses direct connections rather than ambient proxy settings. A private urllib
    opener avoids global handlers or socket monkey patches. DNS is cached at
    socket creation, preserving URL host names for Host, SNI and certificates.
    """

    def __init__(
        self,
        *,
        max_response_bytes: int = 10 * 1024 * 1024,
        max_redirects: int = 5,
        max_concurrent_downloads: int = 32,
        dns_cache: Optional[DNSCache] = None,
        follow_same_origin_redirects: bool = False,
    ) -> None:
        for name, value, minimum in (
            ("max_response_bytes", max_response_bytes, 1),
            ("max_redirects", max_redirects, 0),
            ("max_concurrent_downloads", max_concurrent_downloads, 1),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        if not isinstance(follow_same_origin_redirects, bool):
            raise ValueError("follow_same_origin_redirects must be a boolean")
        self._max_response_bytes = max_response_bytes
        self._slots = asyncio.Semaphore(max_concurrent_downloads)
        self._local = threading.local()
        self._dns_cache = dns_cache if dns_cache is not None else DNSCache()
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _DNSHTTPHandler(self._dns_cache, self._local),
            _DNSHTTPSHandler(self._dns_cache, self._local),
            _CrawlerRedirectHandler(max_redirects, follow_same_origin_redirects),
        )

    async def fetch(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 30.0,
    ) -> FetchResponse:
        """Fetch with one deadline covering admission, DNS and response reading."""
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be a finite positive number")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite positive number")
        try:
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                raise ValueError("Only HTTP(S) URLs with a host are supported")
            _origin(url)
        except ValueError as error:
            raise web_crawler.exceptions.FetchError(url, str(error)) from error
        control = _FetchControl(time.monotonic() + timeout)
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout)
        except asyncio.TimeoutError as error:
            raise web_crawler.exceptions.FetchError(
                url, "Request timed out waiting for a download slot"
            ) from error
        loop = asyncio.get_running_loop()
        try:
            future = loop.run_in_executor(
                None, self._blocking_fetch, url, dict(headers or {}), control
            )
        except BaseException:
            self._slots.release()
            raise

        def completed(done):
            self._slots.release()
            if not done.cancelled():
                done.exception()

        future.add_done_callback(completed)
        try:
            return await asyncio.wait_for(asyncio.shield(future), control.remaining())
        except asyncio.TimeoutError as error:
            control.cancel()
            raise web_crawler.exceptions.FetchError(url, "Request timed out") from error
        except asyncio.CancelledError:
            control.cancel()
            raise

    def _read_response(
        self, response, url: str, control: _FetchControl
    ) -> FetchResponse:
        headers = dict(response.getheaders())
        length = next(
            (
                value
                for key, value in headers.items()
                if key.lower() == "content-length"
            ),
            None,
        )
        if length is not None:
            try:
                declared = int(length)
            except (ValueError, TypeError) as error:
                raise web_crawler.exceptions.FetchError(
                    url, "Invalid Content-Length"
                ) from error
            if declared < 0 or declared > self._max_response_bytes:
                raise web_crawler.exceptions.FetchError(
                    url, "Response exceeds max_response_bytes"
                )
        control.set_read_timeout()
        body = response.read(self._max_response_bytes + 1)
        control.remaining()
        if len(body) > self._max_response_bytes:
            raise web_crawler.exceptions.FetchError(
                url, "Response exceeds max_response_bytes"
            )
        if length is not None and len(body) != declared:
            raise web_crawler.exceptions.FetchError(url, "Incomplete response body")
        return FetchResponse(response.status, headers, body, response.geturl())

    def _blocking_fetch(
        self,
        url: str,
        headers: Dict[str, str],
        control: _FetchControl,
    ) -> FetchResponse:
        """Synchronous fetch using urllib.request."""
        req = urllib.request.Request(url, headers=headers)
        self._local.control = control
        try:
            try:
                response = self._opener.open(req, timeout=control.remaining())
            except urllib.error.HTTPError as error:
                with contextlib.closing(error):
                    if 300 <= error.code < 400:
                        return self._read_response(error, url, control)
                    raise web_crawler.exceptions.FetchError(
                        url, f"HTTP {error.code}: {error.reason}"
                    ) from error
            with contextlib.closing(response):
                return self._read_response(response, url, control)
        except urllib.error.URLError as e:
            raise web_crawler.exceptions.FetchError(url, str(e.reason)) from e
        except TimeoutError as e:
            raise web_crawler.exceptions.FetchError(url, "Request timed out") from e
        except (OSError, http.client.HTTPException, ValueError) as error:
            raise web_crawler.exceptions.FetchError(url, str(error)) from error
        finally:
            del self._local.control
