"""Role-specific canonical routes, unique query parameters and bounded values."""

import re
from urllib.parse import parse_qs, urlsplit

from .common import Error, identifier, integer
from .contracts import MAX_TIMESTAMP


def target(raw, role, method):
    parsed = urlsplit(raw)
    if (
        not raw.startswith("/")
        or raw.startswith("//")
        or parsed.netloc
        or parsed.scheme
        or parsed.fragment
        or "%" in parsed.path
        or "\\" in raw
        or "//" in parsed.path
    ):
        raise ValueError("canonical origin-form route required")
    path = parsed.path
    routes = {"/health": "GET"}
    if role == "gateway":
        routes.update(
            {
                "/admin/status": "GET",
                "/admin/clock": "POST",
                "/admin/promote": "POST",
                "/internal/control": "GET",
                "/internal/events": "GET",
                "/v1/orders": "POST",
                "/v1/order": "POST",
                "/v1/cancels": "POST",
                "/v1/account": "GET",
            }
        )
        for prefix in ("/v1/requests/", "/v1/orders/"):
            if path.startswith(prefix):
                identifier(path[len(prefix) :])
                routes[path] = "GET"
    elif role in {"primary", "backup"}:
        routes.update(
            {
                "/admin/snapshot": "POST",
                "/admin/audit": "GET",
                "/internal/raw": "GET",
                "/internal/serve": "GET",
                "/internal/feed": "GET",
                "/internal/query": "GET",
            }
        )
    elif role == "market":
        routes.update(
            {
                "/admin/status": "GET",
                "/v1/report": "GET",
                "/execution": "GET",
                "/v1/executions": "GET",
                "/market/snapshot": "GET",
                "/market/deltas": "GET",
                "/market/book": "GET",
                "/market/candles": "GET",
                "/marketdata/orderBook/L2": "GET",
                "/marketdata/candles": "GET",
            }
        )
    else:
        raise ValueError("unknown service role")
    if path not in routes:
        raise Error(404, "route missing")
    if method != routes[path]:
        raise Error(405, "unsupported route method")
    allowed = {
        "/internal/events": {"after"},
        "/internal/serve": {"epoch"},
        "/internal/feed": {"after", "epoch"},
        "/internal/query": {"epoch", "kind", "key", "owner"},
        "/market/deltas": {"after"},
        "/market/book": {"symbol", "depth"},
        "/marketdata/orderBook/L2": {"symbol", "depth"},
        "/market/candles": {"symbol", "resolution", "startTime", "endTime"},
        "/marketdata/candles": {"symbol", "resolution", "startTime", "endTime"},
        "/execution": {"symbol", "orderId", "startTime", "endTime"},
        "/v1/executions": {"symbol", "orderId", "startTime", "endTime"},
    }.get(path, set())
    values = parse_qs(parsed.query, strict_parsing=True, keep_blank_values=True)
    if any(key not in allowed or len(items) != 1 for key, items in values.items()):
        raise ValueError("unknown/repeated query")
    query = {key: items[0] for key, items in values.items()}
    for key, value in query.items():
        if key in {"symbol", "orderId", "owner", "kind", "key"}:
            identifier(value)
        else:
            if not re.fullmatch(r"[0-9]{1,12}", value):
                raise ValueError("unsigned integer query required")
            minimum, maximum = (
                (1, 100)
                if key == "depth"
                else (1, 86400)
                if key == "resolution"
                else (0, MAX_TIMESTAMP)
                if key in {"startTime", "endTime"}
                else (0, 10**12)
            )
            query[key] = integer(int(value), minimum, maximum)
    if query.get("startTime", 0) > query.get("endTime", MAX_TIMESTAMP):
        raise ValueError("time interval reversed")
    return path, query
