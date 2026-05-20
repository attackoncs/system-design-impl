"""Lightweight HTTP demo server using only Python stdlib.

Demonstrates the full URL shorten/redirect flow using the url_shortener library.

Endpoints:
    POST /shorten         - Body: {"url": "...", "redirect_type": 301|302}
                            Returns: 201 with {"short_url": "..."}
    GET  /<short_code>    - Redirects (301/302) to the original URL
    GET  /stats/<code>    - Returns click stats as JSON

Usage:
    python -m examples.demo_server --port 8000

Example requests (using curl):
    # Shorten a URL
    curl -X POST http://localhost:8000/shorten \\
         -H "Content-Type: application/json" \\
         -d '{"url": "https://example.com/very/long/path", "redirect_type": 302}'

    # Follow a short URL redirect
    curl -L http://localhost:8000/<short_code>

    # Check click stats
    curl http://localhost:8000/stats/<short_code>
"""

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer

from url_shortener import RedirectType, ShortCodeNotFoundError, URLShortener


class URLShortenerHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the URL shortener demo."""

    shortener: URLShortener  # Class-level shared instance

    def do_POST(self) -> None:
        """Handle POST /shorten requests."""
        if self.path != "/shorten":
            self._send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON")
            return

        long_url = data.get("url", "")
        redirect_type_code = data.get("redirect_type", 302)

        try:
            redirect_type = RedirectType(redirect_type_code)
        except ValueError:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                f"Invalid redirect_type: {redirect_type_code}. Must be 301 or 302.",
            )
            return

        try:
            short_url = self.shortener.shorten(long_url, redirect_type)
        except Exception as e:
            self._send_error(HTTPStatus.BAD_REQUEST, str(e))
            return

        self._send_json(HTTPStatus.CREATED, {"short_url": short_url})

    def do_GET(self) -> None:
        """Handle GET /<short_code> and GET /stats/<code> requests."""
        path = self.path.lstrip("/")

        if path.startswith("stats/"):
            short_code = path[len("stats/"):]
            count = self.shortener.get_click_count(short_code)
            self._send_json(HTTPStatus.OK, {
                "short_code": short_code,
                "click_count": count,
            })
            return

        # Treat as short code redirect
        short_code = path
        try:
            long_url, redirect_type = self.shortener.resolve(short_code)
        except ShortCodeNotFoundError:
            self._send_error(HTTPStatus.NOT_FOUND, "Short code not found")
            return

        self.send_response(redirect_type.value)
        self.send_header("Location", long_url)
        self.end_headers()

    def _send_json(self, status: HTTPStatus, data: dict) -> None:
        """Send a JSON response."""
        body = json.dumps(data).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        """Send an error JSON response."""
        self._send_json(status, {"error": message})


def run_server(port: int = 8000, domain: str = "http://localhost:8000") -> None:
    """Start the demo HTTP server.

    Args:
        port: Port number to listen on. Defaults to 8000.
        domain: Base domain for constructing short URLs. Defaults to http://localhost:8000.
    """
    shortener = URLShortener(domain=domain)
    URLShortenerHandler.shortener = shortener

    server = HTTPServer(("", port), URLShortenerHandler)
    print(f"URL Shortener demo running on http://localhost:{port}")
    print("Endpoints:")
    print(f"  POST /shorten     - Shorten a URL")
    print(f"  GET /<code>       - Redirect to original URL")
    print(f"  GET /stats/<code> - View click stats")
    print("\nPress Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="URL Shortener Demo Server")
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to listen on (default: 8000)",
    )
    args = parser.parse_args()
    run_server(port=args.port, domain=f"http://localhost:{args.port}")
