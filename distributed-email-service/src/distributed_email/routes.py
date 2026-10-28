"""Validate public route/query contracts before executing or forwarding work."""

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
        max_num_fields=16,
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


def public_route(path, method, params, *, gateway=False):
    allowed = set()
    if path == "/v1/uploads" and method == "POST":
        pass
    elif path == "/v1/messages" and method in ("GET", "POST"):
        if method == "GET":
            allowed = {"folder", "read", "before", "q", "field"}
    elif path == "/v1/folders" and method == "GET":
        pass
    elif (
        re.fullmatch(r"/v1/folders/(inbox|sent|spam|quarantine)/messages", path)
        and method == "GET"
    ):
        allowed = {"read", "before", "q", "field"}
    elif path == "/v1/changes" and method == "GET":
        allowed = {"after", "lineage", "wait"}
    elif re.fullmatch(r"/v1/messages/[a-zA-Z0-9_-]{1,80}", path) and method in (
        "GET",
        "PUT",
        "DELETE",
    ):
        if method == "DELETE":
            allowed = {"version"}
            if "version" not in params:
                raise ValueError("delete requires version")
    elif (
        re.fullmatch(r"/v1/messages/[a-zA-Z0-9_-]{1,80}/attachments/[0-9]{1,2}", path)
        and method == "GET"
    ):
        pass
    else:
        raise Error(404, "unknown route or method")
    if method == "GET":
        allowed.update({"min_position", "lineage"})
    if gateway:
        allowed.add("replica")
    if set(params) - allowed:
        raise ValueError("unknown route parameter")
