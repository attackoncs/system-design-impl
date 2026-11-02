"""Chapter 9 regressions: durable IDs, HTTP input, and wire-safe redirects."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import io
import json
import sqlite3

import pytest

from url_shortener import (
    Base62Strategy,
    URLShortener,
    URLValidationError,
    encode_base62,
)
from url_shortener.http import create_app
from url_shortener.sqlite_storage import SQLiteStorage
from url_shortener.urls import redirect_uri


def call(app, method, path, data):
    body = data if isinstance(data, bytes) else json.dumps(data).encode("utf-8")
    response = []

    def start_response(status, headers):
        # WSGI servers encode response headers as ISO-8859-1 on the wire.
        for _, value in headers:
            value.encode("latin-1")
        response.append((status, dict(headers)))

    result = b"".join(
        app(
            {
                "REQUEST_METHOD": method,
                "PATH_INFO": path,
                "CONTENT_LENGTH": str(len(body)),
                "wsgi.input": io.BytesIO(body),
            },
            start_response,
        )
    )
    return *response[0], json.loads(result)


def persistent_service(path):
    storage = SQLiteStorage(path)
    return URLShortener(storage=storage, strategy=Base62Strategy(id_generator=storage))


def test_persistent_ids_across_instances_and_restart(tmp_path):
    path = tmp_path / "urls.db"
    services = [persistent_service(path) for _ in range(8)]
    urls = [f"https://example.org/{i}" for i in range(64)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        short = list(pool.map(lambda i: services[i % 8].shorten(urls[i]), range(64)))
    assert len(set(short)) == len(urls)
    reopened = persistent_service(path)
    assert reopened.shorten(urls[0]) == short[0]
    assert reopened.shorten("https://example.org/new") not in short
    for link, url in zip(short, urls):
        assert reopened.resolve(link.rsplit("/", 1)[1])[0] == url


def test_id_allocation_rolls_back_with_failed_mapping(tmp_path):
    storage = SQLiteStorage(tmp_path / "urls.db")

    def failing_factory():
        assert storage.next_id() == 1
        raise RuntimeError("save failed")

    with pytest.raises(RuntimeError, match="save failed"):
        storage.get_or_create("https://example.org", failing_factory)
    assert storage.next_id() == 1
    assert SQLiteStorage(storage.path).next_id() == 2


def test_duplicate_url_does_not_consume_id(tmp_path):
    service = persistent_service(tmp_path / "urls.db")
    assert service.shorten("https://example.org").endswith("/0000001")
    assert service.shorten("https://example.org").endswith("/0000001")
    assert service.shorten("https://example.org/other").endswith("/0000002")


def test_sqlite_sequence_exhaustion_does_not_wrap(tmp_path):
    storage = SQLiteStorage(tmp_path / "urls.db")
    with closing(sqlite3.connect(storage.path, isolation_level=None)) as db:
        db.execute("UPDATE id_sequence SET value=?", (2**63 - 2,))
    assert storage.next_id() == 2**63 - 1
    with pytest.raises(OverflowError, match="exhausted"):
        storage.next_id()


def test_sequence_initializes_from_legacy_row_ids(tmp_path):
    path = tmp_path / "legacy.db"
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        db.execute(
            "CREATE TABLE urls (id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, "
            "url TEXT UNIQUE NOT NULL, redirect INTEGER NOT NULL, created TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO urls VALUES(1, '0000001', 'https://example.org', "
            "302, '2026-10-31T00:00:00+00:00')"
        )
    service = persistent_service(path)
    assert service.shorten("https://example.org") == "http://short.url/0000001"
    assert service.shorten("https://example.org/new").endswith("/0000002")


def test_existing_url_does_not_wait_for_sqlite_writer(tmp_path):
    path = tmp_path / "urls.db"
    service = URLShortener(storage=SQLiteStorage(path))
    expected = service.shorten("https://example.org")
    with closing(sqlite3.connect(path, isolation_level=None)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(service.shorten, "https://example.org")
            try:
                assert result.result(timeout=1) == expected
            finally:
                writer.execute("ROLLBACK")


@pytest.mark.parametrize(
    "url",
    [
        "https://:80",
        "https://user@",
        "https://example.org:invalid",
        "https://example.org:65536",
        "https://[::1",
        "https://exa mple.org/",
        "https://example.org/a b",
        "https://example.org/\\evil",
        "https://example.org/\ud800",
    ],
)
def test_invalid_url_has_public_validation_error_and_is_not_saved(url):
    service = URLShortener()
    with pytest.raises(URLValidationError):
        service.shorten(url)
    assert service._storage.get_by_long_url(url) is None


@pytest.mark.parametrize(
    "url,location",
    [
        (
            "https://例子.中国/你好?q=世界#章节",
            "https://xn--fsqu00a.xn--fiqs8s/%E4%BD%A0%E5%A5%BD?q=%E4%B8%96%E7%95%8C#%E7%AB%A0%E8%8A%82",
        ),
        (
            "https://example.org/a%2Fb?q=%E4%B8%AD",
            "https://example.org/a%2Fb?q=%E4%B8%AD",
        ),
        ("http://[::1]:8080/a", "http://[::1]:8080/a"),
    ],
)
def test_http_redirect_is_ascii_but_library_preserves_original(url, location):
    service = URLShortener()
    app = create_app(service)
    status, _, data = call(app, "POST", "/api/v1/data/shorten", {"longUrl": url})
    assert status == "200 OK"
    code = data["shortURL"].rsplit("/", 1)[1]
    assert service.resolve(code)[0] == url
    status, headers, _ = call(app, "GET", "/" + code, b"")
    assert status.startswith("302")
    assert headers["Location"] == location
    assert headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("value", [0, 999, "301", [], True])
def test_invalid_redirect_configuration_is_rejected(value):
    with pytest.raises(ValueError):
        URLShortener(default_redirect_type=value)
    service = URLShortener()
    with pytest.raises(ValueError):
        service.shorten("https://example.org", redirect_type=value)
    assert service._storage.get_by_long_url("https://example.org") is None


def test_deep_json_is_bad_request():
    status, _, _ = call(
        create_app(URLShortener()),
        "POST",
        "/api/v1/data/shorten",
        b"[" * 5000 + b"0" + b"]" * 5000,
    )
    assert status == "400 Bad Request"


@pytest.mark.parametrize("number,encoded", [(11157, "2TX"), (2009215674938, "zn9edcu")])
def test_book_base62_examples(number, encoded):
    assert encode_base62(number) == encoded


def test_large_id_is_never_truncated():
    class ID:
        def next_id(self):
            return 62**7

    service = URLShortener(strategy=Base62Strategy(ID()))
    assert service.shorten("https://example.org").endswith("/10000000")


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://[v1.fe]/x", "http://[v1.fe]/x"),
        ("https://example.org/#", "https://example.org/#"),
        ("https://example.org/?", "https://example.org/?"),
        ("https://example.org/?#", "https://example.org/?#"),
        ("https://xn--fa-hia.de/", "https://xn--fa-hia.de/"),
        ("https://e\u0301.example/", "https://xn--9ca.example/"),
    ],
)
def test_redirect_preserves_host_and_empty_component_semantics(url, expected):
    assert redirect_uri(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://faß.de/",
        "https://ς.example/",
        "https://ab\u200dcd.example/",
    ],
)
def test_lossy_idna_conversion_is_rejected_before_saving(url):
    service = URLShortener()
    with pytest.raises(URLValidationError):
        service.shorten(url)
    assert service._storage.get_by_long_url(url) is None
