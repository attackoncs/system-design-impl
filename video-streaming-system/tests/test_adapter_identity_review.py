"""Adapter identity regressions for verified media sizes and metadata DSNs."""

import sqlite3
import urllib.parse
from http.client import HTTPConnection

import pytest
from test_distributed import postgres_dsn as postgres_dsn
from test_http_reliability import running
from test_storage_reliability import S3Client
from test_video import drain, uploaded
from video_streaming import VideoService
from video_streaming.database import MetadataUnavailable, Postgres
from video_streaming.remote_storage import S3Objects


@pytest.fixture
def remote_video(tmp_path):
    client = S3Client([])
    store = S3Objects(tmp_path / "objects", "private", client=client)
    service = VideoService(tmp_path / "metadata.sqlite3", store, profiles=(144,))
    try:
        token, upload = uploaded(service)
        drain(service)
        grant = service.playback(token, upload["video_id"])
        key = service.asset(grant, "144/segment_00000.ts").name
        yield service, store, client, grant, key
    finally:
        service.close()


@pytest.mark.parametrize(
    "method,range_header,status,length,content_range,body",
    [
        ("GET", None, 200, "22", None, b"fake-segment-not-media"),
        ("HEAD", None, 200, "22", None, b""),
        ("GET", "bytes=5-9", 206, "5", "bytes 5-9/22", b"segme"),
        ("HEAD", "bytes=5-9", 206, "5", "bytes 5-9/22", b""),
    ],
)
def test_warm_remote_media_size_matches_verified_bytes(
    remote_video, method, range_header, status, length, content_range, body
):
    service, store, client, grant, key = remote_video
    assert store.file(key).read_bytes() == b"fake-segment-not-media"
    client.values["video/" + key] = b"bad"
    target = "/media?" + urllib.parse.urlencode(
        {"grant": grant, "resource": "144/segment_00000.ts"}
    )
    with running(service) as (server, _):
        connection = HTTPConnection(*server.server_address, timeout=2)
        try:
            headers = {"Range": range_header} if range_header else {}
            connection.request(method, target, headers=headers)
            response = connection.getresponse()
            assert response.status == status
            assert response.headers["Content-Length"] == length
            assert response.headers.get("Content-Range") == content_range
            assert response.read() == body
        finally:
            connection.close()


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_cold_corrupt_remote_media_fails_before_success_headers(remote_video, method):
    service, store, client, grant, key = remote_video
    (store.root / key).unlink()
    client.values["video/" + key] = b"bad"
    target = "/media?" + urllib.parse.urlencode(
        {"grant": grant, "resource": "144/segment_00000.ts"}
    )
    with running(service) as (server, _):
        connection = HTTPConnection(*server.server_address, timeout=2)
        try:
            connection.request(method, target)
            response = connection.getresponse()
            assert response.status == 503
            data = response.read()
            if method == "HEAD":
                assert data == b""
            else:
                assert b"object unavailable" in data
        finally:
            connection.close()


def test_conninfo_without_host_uses_owned_postgres(postgres_dsn, tmp_path, monkeypatch):
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    parameters = conninfo_to_dict(postgres_dsn)
    monkeypatch.setenv("PGHOST", parameters.pop("host"))
    monkeypatch.chdir(tmp_path)
    dsn = make_conninfo(**parameters)
    service = VideoService(dsn, tmp_path / "objects")
    try:
        assert isinstance(service.db, Postgres)
        token = service.issue("alice")
        upload = service.create(token, "PostgreSQL upload", 1)
        assert service.status(token, upload["video_id"])["state"] == "UPLOADING"
        assert not (tmp_path / dsn).exists()
    finally:
        service.close()


@pytest.mark.parametrize("dsn", ["dbname='unterminated", "unknown_parameter=review"])
def test_invalid_conninfo_never_creates_sqlite_file(tmp_path, monkeypatch, dsn):
    pytest.importorskip("psycopg")
    monkeypatch.chdir(tmp_path)
    service = None
    try:
        with pytest.raises(MetadataUnavailable):
            service = VideoService(dsn, tmp_path / "objects")
    finally:
        if service is not None:
            service.close()
    assert not (tmp_path / dsn).exists()


def test_sqlite_string_path_with_embedded_host_is_a_file(tmp_path):
    path = tmp_path / "host=archive" / "metadata.sqlite3"
    path.parent.mkdir()
    service = VideoService(str(path), tmp_path / "objects")
    try:
        assert isinstance(service.db, sqlite3.Connection)
        assert service.authenticate(service.issue("alice")) == "alice"
        assert path.is_file()
    finally:
        service.close()


def test_pathlike_forces_sqlite_for_conninfo_shaped_filename(tmp_path):
    path = tmp_path / "dbname=archive.sqlite3"
    service = VideoService(path, tmp_path / "objects")
    try:
        assert isinstance(service.db, sqlite3.Connection)
        assert service.authenticate(service.issue("alice")) == "alice"
        assert path.is_file()
    finally:
        service.close()
