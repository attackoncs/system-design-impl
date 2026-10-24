"""Small authenticated HTTP adapter with one shared application event loop.

This adapter is a runnable local example. A remote deployment should terminate
TLS and enforce connection/rate policies at a gateway. Tokens in query strings
are supported for the chapter's API shape; Bearer headers are preferable.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import datetime
import enum
import http.server
import json
import logging
import math
import socket
import threading
import time
from typing import TYPE_CHECKING, Any, Coroutine, Optional
from urllib.parse import parse_qs, urlsplit

from news_feed_system.exceptions import (
    AuthenticationError,
    FanoutError,
    RateLimitError,
    ValidationError,
)
from news_feed_system.models import FeedRequest

if TYPE_CHECKING:
    from news_feed_system.api import NewsFeedAPI

_LOG = logging.getLogger(__name__)


def _positive_integer(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _positive_timeout(value: float, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be finite and positive")


class Application:
    """Own the API and its workers on a single background asyncio loop.

    HTTP request threads submit operations to this loop. Creating an event loop
    per request would strand the asynchronous fanout workers after publication.
    Close the HTTP server before closing this application.
    """

    def __init__(
        self,
        api: Optional[NewsFeedAPI] = None,
        *,
        database_path: Optional[str] = None,
        request_timeout: float = 15.0,
    ) -> None:
        _positive_timeout(request_timeout, "request_timeout")
        if api is not None and database_path is not None:
            raise ValueError("database_path belongs to the supplied API configuration")
        self.request_timeout = request_timeout
        self.api = api
        self._ready = threading.Event()
        self._closed = False
        self._state_lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run_loop, name="news-feed-event-loop", daemon=True
        )
        self._thread.start()
        self._ready.wait()
        try:
            self.call(self._initialize(database_path))
        except BaseException:
            self.close()
            raise

    def _run_loop(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        try:
            self.loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            if pending:
                self.loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.run_until_complete(self.loop.shutdown_default_executor())
            self.loop.close()

    async def _initialize(self, database_path: Optional[str]) -> None:
        if self.api is None:
            from news_feed_system.api import NewsFeedAPI

            self.api = NewsFeedAPI(database_path=database_path)
        await self.api.start()

    def call(self, operation: Coroutine[Any, Any, Any]) -> Any:
        """Submit an operation and cancel it if its request deadline expires."""
        with self._state_lock:
            if self._closed:
                operation.close()
                raise RuntimeError("Application is closed")
            future = asyncio.run_coroutine_threadsafe(operation, self.loop)
        try:
            return future.result(timeout=self.request_timeout)
        except BaseException:
            future.cancel()
            raise

    def close(self) -> None:
        """Stop workers, cancel outstanding requests and join the loop thread."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        try:
            if self.api is not None:
                stopped = asyncio.run_coroutine_threadsafe(self.api.stop(), self.loop)
                try:
                    stopped.result(timeout=self.request_timeout)
                except concurrent.futures.TimeoutError:
                    stopped.cancel()
                    _LOG.warning("Timed out stopping the feed API; cancelling workers")
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._thread.join(timeout=self.request_timeout + 15)
            if self._thread.is_alive():
                raise RuntimeError("Feed event loop did not stop within its deadline")

    def __enter__(self) -> Application:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _json_default(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, enum.Enum):
        return value.value
    raise TypeError(f"Cannot encode {type(value).__name__}")


class _BoundedHTTPServer(http.server.ThreadingHTTPServer):
    """Bound request threads as well as the OS listen backlog."""

    daemon_threads = False
    block_on_close = True

    def __init__(self, address, handler, max_concurrent_requests: int) -> None:
        self._slots = threading.BoundedSemaphore(max_concurrent_requests)
        self.request_queue_size = max_concurrent_requests
        super().__init__(address, handler)

    def process_request(self, request, client_address) -> None:
        if not self._slots.acquire(blocking=False):
            try:
                request.settimeout(0.1)
                request.sendall(
                    b"HTTP/1.0 503 Service Unavailable\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\nRetry-After: 1\r\n\r\n"
                )
                request.shutdown(socket.SHUT_WR)
                # Consume already arriving bytes before closing: on Windows,
                # closing with unread request headers can reset the connection
                # and discard the 503 response. Bound both bytes and elapsed time.
                deadline = time.monotonic() + 0.1
                remaining_bytes = 65536
                while remaining_bytes and time.monotonic() < deadline:
                    request.settimeout(max(0.001, deadline - time.monotonic()))
                    chunk = request.recv(min(8192, remaining_bytes))
                    if not chunk:
                        break
                    remaining_bytes -= len(chunk)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def create_server(
    app: Application,
    host: str = "127.0.0.1",
    port: int = 8000,
    *,
    max_body_bytes: int = 1024 * 1024,
    max_concurrent_requests: int = 32,
    connection_timeout: float = 10.0,
    allow_query_tokens: bool = True,
) -> http.server.ThreadingHTTPServer:
    """Bind a server for POST/GET ``/v1/me/feed``; caller owns serving/closing."""
    _positive_integer(max_body_bytes, "max_body_bytes")
    _positive_integer(max_concurrent_requests, "max_concurrent_requests")
    _positive_timeout(connection_timeout, "connection_timeout")
    if not isinstance(allow_query_tokens, bool):
        raise ValueError("allow_query_tokens must be a boolean")

    class Handler(http.server.BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(connection_timeout)

        def log_message(self, format, *args) -> None:
            # BaseHTTPRequestHandler logs full URLs, including query tokens.
            return

        def _respond(self, status: int, payload: Any) -> None:
            body = json.dumps(
                payload, default=_json_default, ensure_ascii=False, allow_nan=False
            ).encode("utf-8")
            self.close_connection = True
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                if status == 401:
                    self.send_header("WWW-Authenticate", "Bearer")
                if status == 405:
                    self.send_header("Allow", "GET, POST")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
            except OSError:
                # The client may disconnect after a deadline or before its reply.
                return

        def _query(self) -> dict[str, str]:
            query = parse_qs(
                urlsplit(self.path).query, keep_blank_values=True, max_num_fields=8
            )
            if any(len(values) != 1 for values in query.values()):
                raise ValidationError("Query parameters must occur only once")
            return {key: values[0] for key, values in query.items()}

        def _token(self, query: dict[str, str]) -> str:
            headers = self.headers.get_all("Authorization", [])
            if len(headers) > 1:
                raise AuthenticationError()
            if headers:
                scheme, separator, token = headers[0].partition(" ")
                if scheme.lower() != "bearer" or not separator or not token.strip():
                    raise AuthenticationError()
                return token.strip()
            if allow_query_tokens and query.get("auth_token"):
                return query["auth_token"]
            raise AuthenticationError()

        def _body(self) -> dict[str, Any]:
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
                raise ValidationError(
                    "Supply one Content-Length and no Transfer-Encoding"
                )
            try:
                length = int(lengths[0])
            except ValueError as error:
                raise ValidationError("Invalid Content-Length") from error
            if length < 0:
                raise ValidationError("Invalid Content-Length")
            if length > max_body_bytes:
                raise OverflowError("Request body exceeds the configured limit")
            content_type = self.headers.get_content_type()
            if self.headers.get("Content-Type") and content_type != "application/json":
                raise ValidationError("Expected application/json")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValidationError("Truncated request body")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValidationError("Expected a JSON object")
            return payload

        def _publish(self, token: str) -> Any:
            payload = self._body()
            unexpected = set(payload) - {
                "content",
                "media_urls",
                "selective_sharing",
                "user_id",
            }
            if unexpected:
                raise ValidationError("Unsupported publication fields")
            content = payload.get("content", "")
            media = payload.get("media_urls", [])
            sharing = payload.get("selective_sharing")
            if not isinstance(content, str):
                raise ValidationError("content must be a string", "content")
            for name, value in (("media_urls", media), ("selective_sharing", sharing)):
                if name == "selective_sharing" and value is None:
                    continue
                if not isinstance(value, list) or any(
                    not isinstance(x, str) for x in value
                ):
                    raise ValidationError(f"{name} must be an array of strings", name)
            # The authenticated token determines the author, never body user_id.
            return app.call(
                app.api.publish(
                    FeedRequest(
                        user_id="",
                        auth_token=token,
                        content=content,
                        media_urls=media,
                        selective_sharing=sharing,
                    )
                )
            )

        def _handle(self) -> None:
            try:
                if urlsplit(self.path).path.rstrip("/") != "/v1/me/feed":
                    self._respond(404, {"error": "Not found"})
                    return
                if self.command not in {"GET", "POST"}:
                    self._respond(405, {"error": "Method not allowed"})
                    return
                query = self._query()
                token = self._token(query)
                if self.command == "POST":
                    self._respond(201, {"post_id": self._publish(token)})
                else:
                    page_size = query.get("page_size")
                    self._respond(
                        200,
                        app.call(
                            app.api.retrieve(
                                token,
                                page_size=int(page_size)
                                if page_size is not None
                                else None,
                                cursor=query.get("cursor"),
                            )
                        ),
                    )
            except AuthenticationError as error:
                self._respond(401, {"error": str(error)})
            except RateLimitError as error:
                self._respond(429, {"error": str(error)})
            except OverflowError as error:
                self._respond(413, {"error": str(error)})
            except (ValueError, TypeError, UnicodeError, ValidationError) as error:
                self._respond(400, {"error": str(error)})
            except concurrent.futures.TimeoutError:
                self._respond(504, {"error": "Request deadline exceeded"})
            except FanoutError:
                self._respond(
                    503, {"error": "Feed delivery is temporarily unavailable"}
                )
            except OSError:
                # A client disconnect or socket deadline also closes the request.
                self.close_connection = True
            except Exception:
                _LOG.exception("Feed request failed")
                self._respond(500, {"error": "Internal error"})

        do_GET = _handle
        do_POST = _handle
        do_PUT = _handle
        do_DELETE = _handle
        do_PATCH = _handle
        do_OPTIONS = _handle
        do_HEAD = _handle
        do_TRACE = _handle
        do_CONNECT = _handle

    return _BoundedHTTPServer((host, port), Handler, max_concurrent_requests)
