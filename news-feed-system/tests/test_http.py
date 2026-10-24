"""Exercise the authenticated wire adapter and its request/worker lifecycle."""

import asyncio
import concurrent.futures
import http.client
import json
import threading
import time

import pytest

from news_feed_system.exceptions import (
    AuthenticationError,
    RateLimitError,
    ValidationError,
)
from news_feed_system.http import Application, create_server
from news_feed_system.models import (
    ActionState,
    HydratedFeed,
    HydratedFeedEntry,
    Post,
    PostCounters,
    UserProfile,
)


class StubAPI:
    """Keep wire/error tests independent of feed ranking and cache policy."""

    def __init__(self):
        self.request = None
        self.thread_ids = set()
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.stopped = False

    async def start(self):
        self.thread_ids.add(threading.get_ident())
        self.release = asyncio.Event()

    async def stop(self):
        self.stopped = True
        self.thread_ids.add(threading.get_ident())

    def authenticate(self, token):
        if token != "valid-token":
            raise AuthenticationError()

    async def publish(self, request):
        self.thread_ids.add(threading.get_ident())
        self.authenticate(request.auth_token)
        if request.content == "rate-limit":
            raise RateLimitError("author", 1, 60)
        self.request = request
        return "post_1"

    async def retrieve(self, token, *, page_size=None, cursor=None):
        self.thread_ids.add(threading.get_ident())
        self.authenticate(token)
        if page_size is not None and not 1 <= page_size <= 100:
            raise ValidationError("page_size is outside bounds")
        if cursor == "invalid":
            raise ValidationError("Invalid cursor")
        if cursor == "slow":
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        return HydratedFeed(
            entries=[
                HydratedFeedEntry(
                    post=Post(
                        "post_1",
                        "author",
                        "正文",
                        media_urls=["https://cdn.test/image"],
                    ),
                    author=UserProfile("author", "Author"),
                    actions=ActionState(liked=True),
                    counters=PostCounters(like_count=2, reply_count=1),
                )
            ],
            has_more=True,
            next_cursor="next-page",
        )


def request(
    server,
    method="GET",
    path="/v1/me/feed",
    *,
    token="valid-token",
    body=None,
    headers=None,
):
    supplied = dict(headers or {})
    if token is not None:
        supplied["Authorization"] = "Bearer " + token
    if body is not None and not isinstance(body, (bytes, str)):
        body = json.dumps(body).encode("utf-8")
        supplied.setdefault("Content-Type", "application/json")
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    try:
        connection.request(method, path, body=body, headers=supplied)
        response = connection.getresponse()
        raw = response.read()
        return response.status, json.loads(raw) if raw else {}, dict(response.headers)
    finally:
        connection.close()


@pytest.fixture
def served():
    api = StubAPI()
    with Application(api) as app:
        with create_server(app, port=0) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield app, api, server
            finally:
                server.shutdown()
                thread.join(timeout=3)
    assert api.stopped
    assert len(api.thread_ids) == 1
    assert not app._thread.is_alive()


def test_authenticated_publish_discards_body_user_and_hydrates_get(served):
    _, api, server = served
    status, body, _ = request(
        server,
        "POST",
        body={
            "user_id": "impersonated-user",
            "content": "hello",
            "media_urls": ["https://cdn.test/image"],
            "selective_sharing": ["viewer"],
        },
    )
    assert status == 201 and body == {"post_id": "post_1"}
    assert api.request.user_id == ""
    assert api.request.media_urls == ["https://cdn.test/image"]
    assert api.request.selective_sharing == ["viewer"]
    status, body, headers = request(server)
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    entry = body["entries"][0]
    assert entry["post"]["content"] == "正文"
    assert entry["post"]["media_urls"] == ["https://cdn.test/image"]
    assert entry["post"]["created_at"].endswith("+00:00")
    assert entry["author"]["username"] == "Author"
    assert entry["actions"]["liked"]
    assert entry["counters"] == {"like_count": 2, "reply_count": 1}
    assert body["has_more"] and body["next_cursor"] == "next-page"


def test_authentication_bearer_precedence_and_query_compatibility(served):
    _, _, server = served
    assert request(server, token=None)[0] == 401
    assert request(server, token="invalid")[0] == 401
    path = "/v1/me/feed?auth_token=valid-token"
    assert request(server, path=path, token=None)[0] == 200
    assert request(server, path=path, token="invalid")[0] == 401
    status, _, headers = request(server, token=None)
    assert status == 401 and headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "payload",
    [
        b"{invalid",
        ["array"],
        {"content": 123},
        {"content": "text", "media_urls": "url"},
        {"content": "text", "media_urls": [5]},
        {"content": "text", "selective_sharing": "viewer"},
        {"content": "text", "unknown": True},
    ],
)
def test_malformed_publication_returns_400(served, payload):
    _, api, server = served
    assert request(server, "POST", body=payload)[0] == 400
    assert api.request is None


@pytest.mark.parametrize(
    "query",
    [
        "page_size=not-a-number",
        "page_size=0",
        "page_size=101",
        "cursor=invalid",
        "page_size=1&page_size=2",
    ],
)
def test_invalid_page_parameters_return_400(served, query):
    assert request(served[2], path="/v1/me/feed?" + query)[0] == 400


def test_unknown_route_method_and_rate_limit_statuses(served):
    server = served[2]
    assert request(server, path="/missing")[0] == 404
    status, _, headers = request(server, "DELETE")
    assert status == 405 and headers["Allow"] == "GET, POST"
    assert request(server, "POST", body={"content": "rate-limit"})[0] == 429


def test_body_limit_rejects_without_reading_the_announced_body(served):
    server = served[2]
    status, _, _ = request(
        server, "POST", body=b"", headers={"Content-Length": str(1024 * 1024 + 1)}
    )
    assert status == 413


def test_request_timeout_cancels_the_application_operation():
    api = StubAPI()
    with Application(api, request_timeout=0.05) as app:
        with create_server(app, port=0) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                assert request(server, path="/v1/me/feed?cursor=slow")[0] == 504
                assert api.cancelled.wait(1)
            finally:
                server.shutdown()
                thread.join(timeout=3)


def test_concurrent_request_admission_is_bounded_and_recovers():
    api = StubAPI()
    with Application(api) as app:
        with create_server(app, port=0, max_concurrent_requests=1) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    first = pool.submit(request, server, path="/v1/me/feed?cursor=slow")
                    assert api.started.wait(1)
                    assert request(server)[0] == 503

                    async def release():
                        api.release.set()

                    app.call(release())
                    assert first.result(timeout=2)[0] == 200
                assert request(server)[0] == 200
            finally:
                server.shutdown()
                thread.join(timeout=3)


def test_real_http_publish_reaches_live_workers_and_authenticated_viewer(tmp_path):
    from news_feed_system.api import NewsFeedAPI

    api = NewsFeedAPI(database_path=str(tmp_path / "feed.sqlite3"))
    with Application(api) as app:

        async def bootstrap():
            api.auth.register_token("author-token", "author")
            api.auth.register_token("viewer-token", "viewer")
            api.users.put(UserProfile("author", "Author"))
            api.users.put(UserProfile("viewer", "Viewer"))
            await api.graph.add_follow("viewer", "author")

        app.call(bootstrap())
        with create_server(app, port=0) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                status, result, _ = request(
                    server,
                    "POST",
                    token="author-token",
                    body={"content": "real pipeline"},
                )
                assert status == 201
                deadline = time.monotonic() + 2
                while True:
                    status, feed, _ = request(server, token="viewer-token")
                    assert status == 200
                    if feed["entries"] or time.monotonic() >= deadline:
                        break
                    time.sleep(0.01)
                assert feed["entries"][0]["post"]["post_id"] == result["post_id"]
                assert feed["entries"][0]["author"]["username"] == "Author"
            finally:
                server.shutdown()
                thread.join(timeout=3)
