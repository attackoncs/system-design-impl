"""Serve the chapter's WSGI API with the single-threaded stdlib demo server.

Run: python -m examples.demo_server --port 8000 --database urls.db
POST /api/v1/data/shorten with {"longUrl": "https://example.org"}; GET /<code>.
Use a production WSGI server for concurrent HTTP serving.
"""

import argparse
from typing import Optional
from wsgiref.simple_server import make_server

from url_shortener import Base62Strategy, SQLiteStorage, URLShortener
from url_shortener.http import create_app


def run_server(
    port: int = 8000,
    domain: Optional[str] = None,
    database: Optional[str] = None,
) -> None:
    """Use durable Base62 IDs with --database; otherwise use in-memory defaults."""
    domain = domain or f"http://localhost:{port}"
    if database:
        storage = SQLiteStorage(database)
        shortener = URLShortener(
            storage=storage,
            strategy=Base62Strategy(id_generator=storage),
            domain=domain,
        )
    else:
        shortener = URLShortener(domain=domain)
    with make_server("127.0.0.1", port, create_app(shortener)) as server:
        print(f"URL Shortener demo: {domain}")
        print('POST /api/v1/data/shorten {"longUrl":"https://example.org"}')
        print("GET /<code> or /api/v1/<code>; Ctrl+C to stop")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("Shutting down...")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="URL Shortener WSGI Demo")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--database", help="SQLite file; omitted for in-memory storage")
    args = parser.parse_args()
    run_server(port=args.port, database=args.database)
