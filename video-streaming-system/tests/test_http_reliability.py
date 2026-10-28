"""Real socket regressions for framing, bounded requests and owner cleanup."""

import contextlib
from http.client import HTTPConnection, HTTPResponse
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from video_streaming import VideoService
from video_streaming import http


class TransportService:
    """Small storage boundary; tests exercise the real HTTP request adapter."""

    cache_bytes = 1024

    def __init__(self, path):
        self.path = path
        self.created = []
        self.parts = []

    def authenticate(self, token):
        if token != "owner":
            raise PermissionError("credential rejected")
        return "alice"

    def create(self, token, title, size, part_size):
        self.authenticate(token)
        self.created.append((title, size, part_size))
        return {"video_id": "video"}

    def verify(self, grant, kind):
        if grant != "upload" or kind != "upload":
            raise PermissionError("grant rejected")
        return {"size": 4}

    def put_part(self, grant, data, checksum):
        self.parts.append((grant, data, checksum))
        return True

    def asset(self, grant, resource):
        if grant != "play":
            raise PermissionError("grant rejected")
        if resource != "segment.ts":
            raise ValueError("unknown resource")
        return self.path

    def read_small(self, path):
        return path.read_bytes()


@pytest.fixture
def service(tmp_path):
    path = tmp_path / "segment"
    path.write_bytes(b"0123456789")
    return TransportService(path)


@contextlib.contextmanager
def running(service, **options):
    server = http.server(service, **options)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def raw_request(server, target, headers=(), method="POST", body=b""):
    with socket.create_connection(server.server_address, timeout=2) as client:
        message = f"{method} {target} HTTP/1.1\r\nHost: local\r\n".encode()
        client.sendall(
            message
            + b"".join(
                name.encode() + b": " + value.encode() + b"\r\n"
                for name, value in headers
            )
            + b"\r\n"
            + body
        )
        response = HTTPResponse(client)
        response.begin()
        return response.status, response.read(), dict(response.headers)


@pytest.mark.parametrize(
    "headers",
    [
        [("Content-Length", "2"), ("Content-Length", "2")],
        [("Content-Length", "2"), ("Content-Length", "3")],
        [("Transfer-Encoding", "chunked"), ("Content-Length", "2")],
        [("Transfer-Encoding", "identity")],
        [("Content-Length", "+2")],
        [("Content-Length", "-1")],
        [("Content-Length", "2, 2")],
        [("Content-Length", "100000000000000000000")],
        [("Authorization", "Bearer owner"), ("Authorization", "Bearer owner")],
    ],
)
def test_ambiguous_or_unsupported_framing_never_mutates(service, headers):
    with running(service) as (server, _):
        status, _, _ = raw_request(server, "/uploads", headers, body=b"{}")
        assert status == 400
    assert service.created == []


def test_upload_rejects_bad_credential_before_waiting_for_body(service):
    with running(service, request_timeout=2) as (server, _):
        started = time.monotonic()
        status, _, _ = raw_request(
            server,
            "/uploads",
            [("Content-Length", "50000"), ("Authorization", "Bearer rejected")],
        )
        assert status == 403 and time.monotonic() - started < 1
    assert not service.created


@pytest.mark.parametrize(
    "target",
    [
        "/uploads?unknown=1",
        "/uploads?x=1&x=2",
        "/uploads?x=%ZZ",
        "/uploads?x=%ff",
        "/uploads?x",
        "http://remote/uploads",
    ],
)
def test_exact_route_and_parameter_validation(service, target):
    with running(service) as (server, _):
        status, _, _ = raw_request(
            server,
            target,
            [("Authorization", "Bearer owner"), ("Content-Length", "2")],
            body=b"{}",
        )
        assert status == 400
    assert not service.created


@pytest.mark.parametrize(
    "body",
    [
        b'{"title":"a","title":"b","size":1}',
        b'{"title":"a","size":NaN}',
        b"[]",
        b'{"title":"a","size":1,"unexpected":true}',
    ],
)
def test_strict_json_upload_body(service, body):
    with running(service) as (server, _):
        status, _, _ = raw_request(
            server,
            "/uploads",
            [("Authorization", "Bearer owner"), ("Content-Length", str(len(body)))],
            body=body,
        )
        assert status == 400
    assert not service.created


def test_signed_chunk_size_rejected_before_reading_or_storing(service):
    with running(service) as (server, _):
        status, _, _ = raw_request(
            server,
            "/part?grant=upload",
            [("Content-Length", "3")],
            method="PUT",
            body=b"abc",
        )
        assert status == 400
        status, _, _ = raw_request(
            server,
            "/part?grant=upload",
            [("Content-Length", "4")],
            method="PUT",
            body=b"abcd",
        )
        assert status == 200
    assert service.parts == [("upload", b"abcd", None)]


def test_expect_continue_is_sent_after_authorization(service):
    with running(service) as (server, _):
        with socket.create_connection(server.server_address, timeout=2) as client:
            client.sendall(
                b"PUT /part?grant=upload HTTP/1.1\r\nHost: local\r\nContent-Length: 4\r\nExpect: 100-continue\r\n\r\n"
            )
            assert b"100 Continue" in client.recv(4096)
            client.sendall(b"abcd")
            response = HTTPResponse(client)
            response.begin()
            assert response.status == 200
            response.read()


def test_head_media_has_same_headers_and_no_body(service):
    with running(service) as (server, _):
        connection = HTTPConnection(*server.server_address, timeout=2)
        try:
            connection.request(
                "HEAD",
                "/media?grant=play&resource=segment.ts",
                headers={"Range": "bytes=2-4"},
            )
            response = connection.getresponse()
            assert response.status == 206
            assert response.headers["Content-Length"] == "3"
            assert response.headers["Content-Range"] == "bytes 2-4/10"
            assert response.headers["Content-Type"] == "video/mp2t"
            assert response.read() == b""
        finally:
            connection.close()


def test_overload_returns_503_and_recovers_slot(service):
    with running(service, concurrency=1) as (server, _):
        assert server.slots.acquire(False)
        try:
            status, _, headers = raw_request(server, "/missing", method="GET")
            assert status == 503 and headers["Cache-Control"] == "no-store"
        finally:
            server.slots.release()
        assert raw_request(server, "/missing", method="GET")[0] == 404


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_absolute_deadline_closes_dripping_input_and_releases_slot(service, phase):
    with running(service, concurrency=1, request_timeout=0.15) as (server, _):
        with socket.create_connection(server.server_address, timeout=1) as client:
            if phase == "headers":
                initial = b"POST /uploads HTTP/1.1\r\nHost: local\r\nX-Slow: "
            else:
                initial = b"POST /uploads HTTP/1.1\r\nHost: local\r\nAuthorization: Bearer owner\r\nContent-Length: 100\r\n\r\n"
            client.sendall(initial)
            started = time.monotonic()
            for _ in range(8):
                try:
                    client.sendall(b"a")
                except OSError:
                    break
                time.sleep(0.04)
            with contextlib.suppress(OSError):
                assert client.recv(1024) == b""
            assert time.monotonic() - started < 0.8
        assert raw_request(server, "/missing", method="GET")[0] == 404
    assert not service.created


def test_server_close_joins_handler_before_service_owner_can_close(
    service, monkeypatch
):
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    original = service.asset

    def waiting(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    monkeypatch.setattr(service, "asset", waiting)
    with running(service) as (server, url):
        responses = []

        def get():
            with urllib.request.urlopen(
                url + "/media?grant=play&resource=segment.ts", timeout=3
            ) as response:
                responses.append(response.read())

        request = threading.Thread(target=get)
        request.start()
        assert entered.wait(1)
        server.shutdown()

        def close():
            server.server_close()
            closed.set()

        closing = threading.Thread(target=close)
        closing.start()
        try:
            assert not closed.wait(0.05)
        finally:
            release.set()
        request.join(2)
        closing.join(2)
        assert closed.is_set() and responses == [b"0123456789"]


def test_metadata_update_during_upload_uses_owner_authorization(tmp_path):
    service = VideoService(tmp_path / "metadata.sqlite3", tmp_path / "objects")
    owner, other = service.issue("alice"), service.issue("bob")
    upload = service.create(owner, "Original", 4)
    target = "/metadata?video=" + upload["video_id"]
    try:
        with running(service) as (server, _):
            body = b'{"title":"Edited while uploading"}'
            status, data, _ = raw_request(
                server,
                target,
                [
                    ("Authorization", "Bearer " + owner),
                    ("Content-Length", str(len(body))),
                ],
                body=body,
            )
            assert status == 200
            assert json.loads(data)["title"] == "Edited while uploading"
            assert service.status(owner, upload["video_id"])["state"] == "UPLOADING"
            assert (
                raw_request(
                    server,
                    target,
                    [
                        ("Authorization", "Bearer " + other),
                        ("Content-Length", str(len(body))),
                    ],
                    body=body,
                )[0]
                == 403
            )
    finally:
        service.close()


@pytest.mark.parametrize("value", ["bytes=１-２", "bytes=0-1,3-4", 0, True])
def test_byte_range_is_single_ascii_header(value):
    with pytest.raises(ValueError):
        http.byte_range(value, 10)
