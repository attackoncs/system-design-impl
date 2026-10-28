"""Exact role paths; keys and tokens remain plain bounded ASCII identifiers."""

import re
from urllib.parse import parse_qs, urlsplit
from .common import Error


def target(value):
    if not value.startswith("/") or value.startswith("//"):
        raise ValueError("origin-form target required")
    result = urlsplit(value)
    if (
        result.scheme
        or result.netloc
        or result.fragment
        or "%" in result.path
        or "//" in result.path
        or len(result.query) > 8192
    ):
        raise ValueError("invalid request path")
    params = parse_qs(
        result.query,
        keep_blank_values=True,
        max_num_fields=16,
        encoding="utf-8",
        errors="strict",
    )
    if any(len(values) != 1 for values in params.values()):
        raise ValueError("repeated query parameter")
    return result.path, params


def validate_route(role, path, method):
    key = r"[A-Za-z0-9_-]{1,80}"
    patterns = {
        "receiver": {
            "GET": [r"/admin/status"],
            "POST": [r"/events", r"/admin/control"],
        },
        "provider": {
            "GET": [
                r"/checkout/" + key,
                r"/operations/" + key,
                r"/statements",
                r"/admin/status",
            ],
            "POST": [r"/checkout/" + key, r"/operations", r"/admin/control"],
        },
        "api": {
            "GET": [
                r"/v1/(wallet|ledger)",
                r"/v1/(operations|payments)/" + key,
                r"/internal/operation/" + key,
                r"/internal/checkouts/" + key,
                r"/admin/status",
                r"/admin/reconciliations/" + key,
            ],
            "POST": [
                r"/webhooks/mock",
                r"/internal/(claim|finish|apply|reconcile|checkouts)",
                r"/admin/(reconcile|retry-job)",
                r"/admin/retry/" + key,
                r"/v1/(payments|payouts|refunds)",
            ],
        },
    }
    if path == "/health" and method == "GET":
        return
    if not any(
        re.fullmatch(pattern, path) for pattern in patterns[role].get(method, [])
    ):
        raise Error(404, "unknown role route or method")
