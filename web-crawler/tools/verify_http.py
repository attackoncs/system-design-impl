"""Verify actual loopback HTTP, robots, document base and durable resumption."""

import argparse
import asyncio
from collections import Counter
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from web_crawler import Crawler, CrawlerConfig  # noqa: E402


async def crawl(path, origin, requests):
    first = Crawler(
        CrawlerConfig(state_path=str(path), max_pages=1, per_host_delay=0.01)
    )
    results = await first.crawl([origin + "/index"])
    resumed = Crawler(
        CrawlerConfig(state_path=str(path), max_pages=2, per_host_delay=0.01)
    )
    results += await resumed.crawl([])
    counts = Counter(result.status.value for result in results)
    assert counts == {"success": 2, "disallowed": 1}
    assert requests.count("/index") == requests.count("/assets/child") == 1
    assert "/blocked" not in requests
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 2
        assert (
            db.execute("SELECT count(*) FROM jobs WHERE state!='done'").fetchone()[0]
            == 0
        )
    return {
        "results": dict(counts),
        "http_requests": requests,
        "snapshots": 2,
        "resumed_new_crawler": True,
    }


def verify():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            if self.path == "/robots.txt":
                body, content_type = (
                    b"User-agent: *\nDisallow: /blocked\n",
                    "text/plain",
                )
            elif self.path == "/index":
                body = (
                    b'<html><base href="/assets/"><a href="http://[bad">bad</a>'
                    b'<a href="child">child</a><a href="/blocked">blocked</a></html>'
                )
                content_type = "text/html"
            elif self.path == "/assets/child":
                body, content_type = b"<html>child</html>", "text/html"
            else:
                raise AssertionError("unexpected request: " + self.path)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory(prefix="crawler-http-") as directory:
                return asyncio.run(
                    crawl(
                        Path(directory) / "crawl.db",
                        f"http://127.0.0.1:{server.server_port}",
                        requests,
                    )
                )
        finally:
            server.shutdown()
            worker.join(timeout=5)
            assert not worker.is_alive()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
