"""Exact external route contracts, independent of handler implementation."""

import re
from urllib.parse import parse_qs, urlsplit

from .common import Error


def target(value):
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
    ):
        raise ValueError("origin-form request target required")
    parsed = urlsplit(value)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or "%" in parsed.path
        or "//" in parsed.path
    ):
        raise ValueError("invalid request path")
    if len(parsed.query) > 8192:
        raise ValueError("query exceeds limit")
    params = parse_qs(
        parsed.query,
        keep_blank_values=True,
        max_num_fields=8,
        encoding="utf-8",
        errors="strict",
    )
    if any(len(values) != 1 for values in params.values()):
        raise ValueError("repeated query parameter")
    return parsed.path, {key: values[0] for key, values in params.items()}


def decimal(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,16}", value):
        raise ValueError("invalid " + name)
    return int(value)


def contract(role, path, method, params):
    allowed = set()
    if path == "/health" and method == "GET":
        kind = None
    elif path == "/internal/status" and method == "GET":
        kind = "internal"
    elif role == "projection":
        if path == "/internal/query" and method == "POST":
            kind = "internal"
        elif path == "/admin/rebuild" and method == "POST":
            kind = "admin"
        else:
            raise Error(404, "unknown projection route or method")
    elif path == "/internal/feed" and method == "GET":
        allowed, kind = {"shard", "after"}, "internal"
        if set(params) != allowed:
            raise ValueError("feed requires shard and after")
    elif re.fullmatch(r"/admin/players/[A-Za-z0-9_-]{1,64}", path) and method == "PUT":
        kind = "admin"
    elif (
        path in ("/admin/close", "/admin/reconcile", "/admin/rebuild")
        and method == "POST"
    ):
        kind = "admin"
    elif path == "/v1/scores" and method == "POST":
        kind = "game"
    elif path == "/v1/scores" and method == "GET":
        allowed, kind = {"month"}, "read"
    elif re.fullmatch(r"/v1/scores/[A-Za-z0-9_-]{1,64}", path) and method == "GET":
        allowed, kind = {"month", "neighbors"}, "read"
    elif re.fullmatch(r"/v1/seasons/[0-9]{4}-[0-9]{2}", path) and method == "GET":
        kind = "read"
    else:
        raise Error(404, "unknown route or method")
    if set(params) - allowed:
        raise ValueError("unknown route parameter")
    return kind
