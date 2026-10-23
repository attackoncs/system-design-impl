"""Small WSGI adapter; run behind a production WSGI server."""

import json
from .exceptions import ShortCodeNotFoundError, URLValidationError


def create_app(shortener):
    def app(environ, start_response):
        status, headers, payload = "404 Not Found", [], {"error": "not found"}
        try:
            path = environ.get("PATH_INFO", "")
            method = environ.get("REQUEST_METHOD")
            if method == "POST" and path == "/api/v1/data/shorten":
                length = int(environ.get("CONTENT_LENGTH") or 0)
                if not 0 < length <= 16384:
                    raise ValueError("request body must be between 1 and 16384 bytes")
                data = json.loads(environ["wsgi.input"].read(length))
                if not isinstance(data, dict):
                    raise ValueError("expected a JSON object")
                payload = {"shortURL": shortener.shorten(data.get("longUrl"))}
                status = "200 OK"
            elif method == "GET" and path.strip("/"):
                code = (
                    path.removeprefix("/api/v1/")
                    if path.startswith("/api/v1/")
                    else path.lstrip("/")
                )
                location, redirect = shortener.resolve(code)
                status = f"{int(redirect)} Redirect"
                headers = [("Location", location)]
                if int(redirect) == 302:
                    headers.append(("Cache-Control", "no-store"))
                payload = {}
        except ShortCodeNotFoundError:
            pass
        except (ValueError, URLValidationError):
            status, payload = "400 Bad Request", {"error": "invalid request"}
        body = json.dumps(payload).encode()
        start_response(
            status,
            headers
            + [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
            ],
        )
        return [body]

    return app
