"""Framing, overload isolation, long-poll ownership and small upload protocol."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.client import HTTPResponse
import json
import socket
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from file_sync import Drive
from file_sync.http import server


@pytest.fixture
def drive(tmp_path):
    instance = Drive(tmp_path / "metadata.sqlite3", tmp_path / "objects", 4)
    instance.token = instance.issue("alice")
    instance.space = instance.create_space(instance.token, "Personal")
    try:
        yield instance
    finally:
        instance.close()


@contextmanager
def running(drive, **bounds):
    http = server(drive, **bounds)
    thread = threading.Thread(target=http.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield http, f"http://127.0.0.1:{http.server_address[1]}"
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=2)


def request(url, route="/config", token=None, method="GET", data=None):
    headers = {} if token is None else {"Authorization": "Bearer " + token}
    if isinstance(data, dict):
        data = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    return urlopen(
        Request(url + route, data=data, headers=headers, method=method), timeout=3
    )


def raw(http, target, headers=(), body=b"", method="GET"):
    with socket.create_connection(http.server_address, timeout=2) as stream:
        data = f"{method} {target} HTTP/1.0\r\nHost: localhost\r\n".encode()
        data += b"".join(
            (name + ": " + value + "\r\n").encode() for name, value in headers
        )
        stream.sendall(data + b"\r\n" + body)
        response = HTTPResponse(stream)
        response.begin()
        value = response.read()
        return response.status, value


@pytest.mark.parametrize(
    "headers",
    [
        [("Content-Length", "0"), ("Content-Length", "0")],
        [("Content-Length", "0"), ("Content-Length", "1")],
        [("Transfer-Encoding", "chunked")],
        [("Content-Length", "+0")],
        [("Content-Length", "-1")],
        [("Content-Type", "application/json"), ("Content-Type", "text/plain")],
    ],
)
def test_ambiguous_framing_is_rejected_before_body(drive, headers):
    with running(drive) as (http, _url):
        status, _ = raw(
            http,
            "/space",
            [("Authorization", "Bearer " + drive.token), *headers],
            method="POST",
        )
        assert status == 400


def test_duplicate_authorization_is_rejected(drive):
    with running(drive) as (http, _url):
        assert (
            raw(http, "/config", [("Authorization", "Bearer " + drive.token)] * 2)[0]
            == 400
        )


def test_upload_authentication_precedes_body_read(drive):
    with running(drive) as (http, _url):
        # Deliberately send no declared body: invalid credentials must fail now.
        before = time.monotonic()
        status, _ = raw(
            http,
            "/begin",
            [("Authorization", "Bearer invalid"), ("Content-Length", "100")],
            method="POST",
        )
        assert status == 403 and time.monotonic() - before < 1


@pytest.mark.parametrize(
    "target",
    [
        "/config?unknown=1",
        "/config?x=1&x=2",
        "/config?%ZZ=1",
        "/config#fragment",
        "http://example.invalid/config",
        "/config?missing_equals",
        "/changes?device=d&cursor=-1",
        "/changes?device=d&cursor=%EF%BC%91",
        "/changes?device=d&wait=nan",
        "/changes?device=d&wait=inf",
        "/changes?device=d&wait=26",
        "/changes?device=d&limit=0",
        "/changes?device=d&limit=1001",
    ],
)
def test_strict_query_parameters(drive, target):
    with running(drive) as (http, _url):
        assert raw(http, target, [("Authorization", "Bearer " + drive.token)])[0] == 400


@pytest.mark.parametrize(
    "data",
    [b'{"name":"a","name":"b"}', b'{"name":NaN}', b"[]", b'{"name":"a","unknown":1}'],
)
def test_strict_json_objects(drive, data):
    with running(drive) as (http, _url):
        status, _ = raw(
            http,
            "/space",
            [
                ("Authorization", "Bearer " + drive.token),
                ("Content-Length", str(len(data))),
            ],
            data,
            "POST",
        )
        assert status == 400


def test_get_with_body_rejected(drive):
    with running(drive) as (http, _url):
        assert (
            raw(
                http,
                "/config",
                [("Authorization", "Bearer " + drive.token), ("Content-Length", "1")],
                b"x",
            )[0]
            == 400
        )


def test_simple_upload_real_protocol_and_idempotent_retry(drive):
    with running(drive) as (_http, url):
        route = "/file?" + urlencode(
            {"space": drive.space, "path": "small.txt", "request": "simple-request"}
        )
        with request(url, route, drive.token, "PUT", b"abcd") as response:
            first = json.load(response)
            assert response.headers["Cache-Control"] == "private, no-store"
        with request(url, route, drive.token, "PUT", b"abcd") as response:
            assert json.load(response) == first
        meta = drive.metadata(drive.token, first["file"])
        assert (
            drive.get_block(drive.token, first["file"], meta["revision"], 0) == b"abcd"
        )
        with pytest.raises(HTTPError) as error:
            request(url, route, drive.token, "PUT", b"abcde")
        assert error.value.code == 400


def test_reserved_foreground_capacity_when_long_poll_slots_full(drive, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def waiting(_token, _device, _cursor, _wait, _limit, *, cancel=None):
        entered.set()
        assert release.wait(2)
        return {"events": [], "cursor": 0}

    monkeypatch.setattr(drive, "changes", waiting)
    with (
        running(drive, concurrency=2, poll_concurrency=1) as (_http, url),
        ThreadPoolExecutor(1) as pool,
    ):
        pending = pool.submit(request, url, "/changes?device=d&wait=1", drive.token)
        try:
            assert entered.wait(1)
            with pytest.raises(HTTPError) as error:
                request(url, "/changes?device=e&wait=1", drive.token)
            assert error.value.code == 503
            with request(url, token=drive.token) as response:
                assert json.load(response)["block_size"] == 4
        finally:
            release.set()
        pending.result(2).close()


def test_revision_limit_returns_latest_and_rejects_invalid_bounds(drive):
    first = drive.upload(
        drive.token, drive.space, "history.txt", b"one", request="history-one"
    )
    second = drive.upload(
        drive.token,
        drive.space,
        "history.txt",
        b"two",
        base=first["revision"],
        request="history-two",
    )
    with running(drive) as (_http, url):
        route = "/revisions?" + urlencode({"file": first["file"], "limit": 1})
        with request(url, route, drive.token) as response:
            assert [item["id"] for item in json.load(response)] == [second["revision"]]
        for limit in (0, 1001):
            with pytest.raises(HTTPError) as error:
                request(
                    url,
                    "/revisions?" + urlencode({"file": first["file"], "limit": limit}),
                    drive.token,
                )
            assert error.value.code == 400


def test_total_overload_preserves_503_and_recovers_slot(drive):
    with running(drive, concurrency=1) as (http, url):
        assert http.slots.acquire(False)
        try:
            status, _ = raw(
                http, "/config", [("Authorization", "Bearer " + drive.token)]
            )
            assert status == 503
        finally:
            http.slots.release()
        with request(url, token=drive.token) as response:
            assert response.status == 200


def test_socket_deadline_stops_slow_body_without_metadata_mutation(drive):
    with running(drive, request_timeout=0.15) as (http, url):
        with socket.create_connection(http.server_address, timeout=2) as stream:
            stream.sendall(
                (
                    "POST /space HTTP/1.0\r\nHost: localhost\r\nAuthorization: Bearer "
                    + drive.token
                    + "\r\nContent-Length: 100\r\n\r\n"
                ).encode()
            )
            stop = time.monotonic() + 1
            while time.monotonic() < stop:
                try:
                    stream.sendall(b" ")
                    time.sleep(0.03)
                except OSError:
                    break
            assert time.monotonic() < stop
        # A socket deadline does not release handler capacity prematurely. Wait
        # until its owned cleanup has actually completed before the next call.
        assert http.slots.acquire(timeout=1)
        http.slots.release()
        with request(url, token=drive.token) as response:
            assert response.status == 200
        assert drive.db.execute("SELECT COUNT(*) FROM spaces").fetchone()[0] == 1


def test_socket_deadline_interrupts_slow_header_stream(drive):
    with running(drive, concurrency=1, request_timeout=0.15) as (http, url):
        with socket.create_connection(http.server_address, timeout=2) as stream:
            stream.sendall(b"GET /config HTTP/1.0\r\nX-Slow: ")
            # Individual bytes arrive within the old per-read timeout.
            stop = time.monotonic() + 1
            while time.monotonic() < stop:
                try:
                    stream.sendall(b"x")
                    time.sleep(0.03)
                except OSError:
                    break
            assert time.monotonic() < stop
        assert http.slots.acquire(timeout=1)
        http.slots.release()
        with request(url, token=drive.token) as response:
            assert response.status == 200


def test_server_close_cancels_and_drains_long_polls(drive, monkeypatch):
    entered, finished = threading.Event(), threading.Event()

    def waiting(_token, _device, _cursor, _wait, _limit, *, cancel=None):
        entered.set()
        assert cancel.wait(2)
        finished.set()
        return {"events": [], "cursor": 0}

    monkeypatch.setattr(drive, "changes", waiting)
    with running(drive) as (http, url), ThreadPoolExecutor(1) as pool:
        pending = pool.submit(request, url, "/changes?device=d&wait=25", drive.token)
        assert entered.wait(1)
        http.shutdown()
        http.server_close()
        assert finished.is_set()
        with pytest.raises(OSError):
            pending.result(1)


@pytest.mark.parametrize(
    "bounds",
    [
        {"concurrency": True},
        {"concurrency": 0},
        {"poll_concurrency": True},
        {"concurrency": 2, "poll_concurrency": 2},
        {"request_timeout": float("nan")},
        {"request_timeout": True},
        {"request_timeout": 0},
    ],
)
def test_invalid_admission_configuration(drive, bounds):
    with pytest.raises(ValueError):
        server(drive, **bounds)
