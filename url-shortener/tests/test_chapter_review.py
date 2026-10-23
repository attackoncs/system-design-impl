import io
import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from url_shortener.shortener import URLShortener
from url_shortener.sqlite_storage import SQLiteStorage
from url_shortener.storage import InMemoryStorage
from url_shortener.models import URLMapping
from url_shortener.http import create_app


def test_shared_database_atomic_creation_and_restart(tmp_path):
    path = tmp_path / "urls.db"
    clients = [URLShortener(storage=SQLiteStorage(path)) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda i: clients[i % 8].shorten("https://example.org/a"), range(32)
            )
        )
    assert len(set(results)) == 1
    reopened = URLShortener(storage=SQLiteStorage(path))
    assert reopened.resolve(results[0].rsplit("/", 1)[1])[0] == "https://example.org/a"


@pytest.mark.parametrize("persistent", [False, True])
def test_collision_never_overwrites_old_link(tmp_path, persistent):
    storage = SQLiteStorage(tmp_path / "urls.db") if persistent else InMemoryStorage()
    storage.save(URLMapping("abc", "https://one.org"))
    with pytest.raises(ValueError):
        storage.save(URLMapping("abc", "https://two.org"))
    assert storage.get_by_short_code("abc").long_url == "https://one.org"


def test_http_create_redirect_and_cache():
    storage = InMemoryStorage()
    service = URLShortener(storage=storage, cache_capacity=1)
    app = create_app(service)

    def call(method, path, body=b""):
        result = []
        payload = b"".join(
            app(
                {
                    "REQUEST_METHOD": method,
                    "PATH_INFO": path,
                    "CONTENT_LENGTH": str(len(body)),
                    "wsgi.input": io.BytesIO(body),
                },
                lambda status, headers: result.append((status, dict(headers))),
            )
        )
        return result[0], json.loads(payload)

    response, data = call(
        "POST", "/api/v1/data/shorten", b'{"longUrl":"https://example.org"}'
    )
    assert response[0] == "200 OK"
    path = "/" + data["shortURL"].rsplit("/", 1)[1]
    response, _ = call("GET", path)
    assert (
        response[0].startswith("302")
        and response[1]["Location"] == "https://example.org"
    )
    storage.get_by_short_code = lambda code: pytest.fail("cache miss")
    call("GET", path)
    assert service.get_click_count(path[1:]) == 2
