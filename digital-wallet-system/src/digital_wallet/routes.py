"""Strict role-specific routes and bounded completion/history query values."""

import re
from urllib.parse import parse_qs, urlsplit

from .common import Error, identifier, integer


def target(raw, role, method):
    parsed = urlsplit(raw)
    if (
        not raw.startswith("/")
        or raw.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or "%" in parsed.path
        or "\\" in raw
        or "//" in parsed.path
    ):
        raise ValueError("canonical origin-form route required")
    path = parsed.path
    routes = {"/health": "GET", "/admin/fence": "POST", "/admin/control": "POST"}
    dynamic = None
    if role == "coordinator":
        routes = {
            "/health": "GET",
            "/admin/status": "GET",
            "/admin/audit": "GET",
            "/admin/sync": "POST",
            "/internal/step": "POST",
            "/v1/wallet/balance_transfer": "POST",
        }
        for prefix, verb in (
            ("/admin/retry/", "POST"),
            ("/admin/cancel/", "POST"),
            ("/v1/transfers/", "GET"),
            ("/v1/accounts/", "GET"),
        ):
            if path.startswith(prefix):
                identifier(path[len(prefix) :])
                routes[path], dynamic = verb, prefix
                break
    elif role == "shard":
        routes.update(
            {
                "/admin/snapshot": "POST",
                "/internal/view": "GET",
                "/internal/audit": "GET",
                "/internal/action": "POST",
            }
        )
        for prefix in ("/internal/participants/", "/internal/accounts/"):
            if path.startswith(prefix):
                identifier(path[len(prefix) :])
                routes[path], dynamic = "GET", prefix
    elif role == "replica":
        routes.update({"/internal/status": "POST", "/internal/append": "POST"})
    else:
        raise ValueError("unknown HTTP role")
    if path not in routes:
        raise Error(404, "unknown route")
    if method != routes[path]:
        raise Error(405, "method unsupported for route")
    query = parse_qs(parsed.query, strict_parsing=True, keep_blank_values=True)
    allowed = (
        "wait_ms"
        if path == "/v1/wallet/balance_transfer" or dynamic == "/v1/transfers/"
        else "at"
        if path == "/internal/view"
        or dynamic in {"/v1/accounts/", "/internal/accounts/"}
        else None
    )
    if any(key != allowed or len(values) != 1 for key, values in query.items()):
        raise ValueError("unknown or repeated query parameter")
    if allowed in query:
        value = query[allowed][0]
        if not re.fullmatch(r"[0-9]{1,12}", value):
            raise ValueError("unsigned integer query required")
        query[allowed] = integer(
            int(value),
            0 if allowed == "wait_ms" else 1,
            2000 if allowed == "wait_ms" else 10**12,
        )
    return path, query
