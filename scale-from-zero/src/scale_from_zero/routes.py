"""Canonical paths and unique, bounded query values for every lab role."""

import re
from urllib.parse import parse_qs, urlsplit

from .common import Error, integer
from .assets import ASSETS


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
    routes = {"/health": {"GET"}, "/admin/status": {"GET"}}
    if role in {"web", "single", "lb"}:
        routes.update({"/v1/login": {"POST"}, "/v1/profile": {"GET", "POST"}})
    if role in {"web", "single", "lb", "edge"}:
        routes.update({"/assets/" + asset: {"GET"} for asset in ASSETS})
    if role == "lb":
        routes["/admin/backends"] = {"GET", "POST"}
    if role == "edge":
        routes["/admin/invalidate"] = {"POST"}
    if role == "worker":
        routes["/admin/control"] = {"POST"}
    if role in {"primary", "replica"}:
        routes["/internal/changes"] = {"GET"}
        routes.update(
            {
                "/internal/" + operation: {"POST"}
                for operation in ("get", "update", "session_create", "session_check")
            }
        )
    if role == "primary":
        routes.update({"/internal/claim": {"POST"}, "/internal/ack": {"POST"}})
    if role == "cache":
        routes.update(
            {
                "/internal/" + operation: {"POST"}
                for operation in ("get", "put", "delete")
            }
        )
    if path not in routes:
        raise Error(404, "route missing")
    if method not in routes[path]:
        raise Error(405, "unsupported route method")
    values = parse_qs(parsed.query, strict_parsing=True, keep_blank_values=True)
    allowed = (
        {"consistency", "min_version"}
        if path == "/v1/profile" and method == "GET"
        else {"after"}
        if path == "/internal/changes"
        else set()
    )
    if any(key not in allowed or len(items) != 1 for key, items in values.items()):
        raise ValueError("unknown or repeated query")
    query = {key: items[0] for key, items in values.items()}
    for key, value in query.items():
        if key == "consistency":
            if value not in {"strong", "eventual"}:
                raise ValueError("consistency must be strong or eventual")
        else:
            if not re.fullmatch(r"[0-9]{1,13}", value):
                raise ValueError("unsigned integer query required")
            query[key] = integer(int(value))
    return path, query
