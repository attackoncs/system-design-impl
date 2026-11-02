"""Exercise separate SQLite writers and real WSGI redirects using only stdlib.

Run from the project root: python tools/verify_concurrency.py --output benchmarks/review.json
This checks correctness under a small local workload, not production capacity.
"""

import argparse
from concurrent.futures import ProcessPoolExecutor
from http.client import HTTPConnection
import json
from pathlib import Path
import platform
import sys
import threading
import time
from tempfile import TemporaryDirectory
from wsgiref.simple_server import WSGIRequestHandler, make_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from url_shortener import Base62Strategy, SQLiteStorage, URLShortener  # noqa: E402
from url_shortener.http import create_app  # noqa: E402


def writer(args):
    path, worker, count = args
    storage = SQLiteStorage(path)
    service = URLShortener(
        storage=storage, strategy=Base62Strategy(id_generator=storage)
    )
    links = []
    for index in range(count):
        url = f"https://example.org/{worker}/{index}"
        links.append((service.shorten(url), url))
        links.append(
            (
                service.shorten("https://example.org/shared"),
                "https://example.org/shared",
            )
        )
    return links


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        pass


def verify_http(service):
    with make_server(
        "127.0.0.1", 0, create_app(service), handler_class=QuietHandler
    ) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            url = "https://例子.中国/你好?q=世界"
            body = json.dumps({"longUrl": url}).encode("utf-8")
            connection.request(
                "POST",
                "/api/v1/data/shorten",
                body,
                {"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            assert response.status == 200
            link = json.loads(response.read())["shortURL"]
            connection.request("GET", "/" + link.rsplit("/", 1)[1])
            response = connection.getresponse()
            assert response.status == 302
            assert response.getheader("Location") == (
                "https://xn--fsqu00a.xn--fiqs8s/%E4%BD%A0%E5%A5%BD?q=%E4%B8%96%E7%95%8C"
            )
            assert response.getheader("Cache-Control") == "no-store"
            response.read()
            connection.request(
                "POST", "/api/v1/data/shorten", b'{"longUrl":"https://:80"}'
            )
            response = connection.getresponse()
            assert response.status == 400
            response.read()
            connection.request("GET", "/missing")
            response = connection.getresponse()
            assert response.status == 404
            response.read()
        finally:
            connection.close()
            server.shutdown()
            thread.join(timeout=5)
        assert not thread.is_alive()
    return {"create": 200, "unicode_redirect": 302, "invalid_url": 400, "missing": 404}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--processes", type=int, default=4)
    parser.add_argument("--links-per-process", type=int, default=125)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.processes < 1 or args.links_per_process < 1:
        parser.error("processes and links-per-process must be positive")
    with TemporaryDirectory(prefix="shortener-review-") as directory:
        path = str(Path(directory) / "urls.db")
        SQLiteStorage(path)
        start = time.perf_counter()
        with ProcessPoolExecutor(max_workers=args.processes) as pool:
            batches = list(
                pool.map(
                    writer,
                    [
                        (path, worker, args.links_per_process)
                        for worker in range(args.processes)
                    ],
                )
            )
        elapsed = time.perf_counter() - start
        by_url, by_code = {}, {}
        for batch in batches:
            for link, url in batch:
                code = link.rsplit("/", 1)[1]
                assert by_url.setdefault(url, code) == code, "deduplication failed"
                assert by_code.setdefault(code, url) == url, "ID collision"
        expected = args.processes * args.links_per_process + 1
        assert len(by_code) == len(by_url) == expected
        storage = SQLiteStorage(path)
        reopened = URLShortener(
            storage=storage, strategy=Base62Strategy(id_generator=storage)
        )
        for code, url in by_code.items():
            assert reopened.resolve(code)[0] == url
        new_code = reopened.shorten("https://example.org/restarted").rsplit("/", 1)[1]
        assert new_code not in by_code
        result = {
            "python": platform.python_version(),
            "platform": platform.system(),
            "processes": args.processes,
            "links_per_process": args.links_per_process,
            "shorten_calls": args.processes * args.links_per_process * 2,
            "unique_links": expected,
            "deduplication_verified": True,
            "all_mappings_resolved_after_restart": True,
            "new_id_after_restart_verified": True,
            "seconds_including_process_startup": elapsed,
            "http": verify_http(reopened),
        }
    encoded = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
