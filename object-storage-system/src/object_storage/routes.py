"""HTTP route and query contracts, validated before touching state."""

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
    result = urlsplit(value)
    if (
        result.scheme
        or result.netloc
        or result.fragment
        or "%" in result.path
        or "//" in result.path
        or result.path.endswith("/")
    ):
        raise ValueError("invalid request path")
    if len(result.query) > 8192:
        raise ValueError("query exceeds limit")
    params = parse_qs(
        result.query,
        keep_blank_values=True,
        max_num_fields=16,
        encoding="utf-8",
        errors="strict",
    )
    if any(len(values) != 1 for values in params.values()):
        raise ValueError("repeated query parameter")
    return result.path, {key: values[0] for key, values in params.items()}


def decimal(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,16}", value):
        raise ValueError("invalid " + name)
    return int(value)


def public_route(path, method, params):
    allowed = set()
    if path == "/v1/buckets" and method == "GET":
        pass
    elif re.fullmatch(
        r"/v1/buckets/[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", path
    ) and method in ("PUT", "GET", "DELETE"):
        pass
    elif (
        re.fullmatch(r"/v1/buckets/[a-z0-9][a-z0-9-]{1,61}[a-z0-9]/objects", path)
        and method == "GET"
    ):
        allowed = {"prefix", "versions", "limit", "token", "delimiter"}
    elif re.fullmatch(
        r"/v1/buckets/[a-z0-9][a-z0-9-]{1,61}[a-z0-9]/object", path
    ) and method in ("PUT", "GET", "DELETE"):
        allowed = {"key"} if method == "PUT" else {"key", "version"}
        if "key" not in params:
            raise ValueError("object key required")
    elif (
        re.fullmatch(
            r"/v1/buckets/[a-z0-9][a-z0-9-]{1,61}[a-z0-9]/(uploads|retention)", path
        )
        and method == "POST"
    ):
        pass
    elif re.fullmatch(r"/v1/uploads/[A-Za-z0-9_-]{1,80}", path) and method in (
        "GET",
        "DELETE",
    ):
        pass
    elif (
        re.fullmatch(r"/v1/uploads/[A-Za-z0-9_-]{1,80}/parts/[0-9]{1,2}", path)
        and method == "PUT"
    ):
        pass
    elif (
        re.fullmatch(r"/v1/uploads/[A-Za-z0-9_-]{1,80}/complete", path)
        and method == "POST"
    ):
        pass
    else:
        raise Error(404, "unknown route or method")
    if set(params) - allowed:
        raise ValueError("unknown route parameter")
