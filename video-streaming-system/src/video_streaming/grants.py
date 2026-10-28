"""Signed upload/playback grants independent of persistence and scheduling."""

import base64
import hashlib
import hmac
import json
import math
import re


def sign(secret, value):
    data = (
        base64.urlsafe_b64encode(
            json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
        )
        .decode()
        .rstrip("=")
    )
    return data + "." + hmac.new(secret, data.encode(), hashlib.sha256).hexdigest()


def _claims_object(pairs):
    claims = {}
    for name, value in pairs:
        if name in claims:
            raise ValueError("duplicate grant claim")
        claims[name] = value
    return claims


def _finite_time(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _reject_constant(value):
    raise ValueError("nonfinite JSON value: " + value)


def verify(secret, clock, token, kind):
    """Validate a signed grant and return its decoded claims.

    Args:
        token: Signed upload or playback grant.
        kind: Required grant kind, either "upload" or "play".

    Returns:
        A claims dictionary whose signature, kind, and expiry were checked.

    Raises:
        PermissionError: The grant is invalid, expired, or of another kind.
    """
    try:
        if (
            not isinstance(token, str)
            or len(token) > 2048
            or kind not in ("upload", "play")
            or re.fullmatch(r"[A-Za-z0-9_-]+\.[0-9a-f]{64}", token) is None
        ):
            raise ValueError()
        data, signature = token.split(".")
        expected = hmac.new(secret, data.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        claims = json.loads(
            base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)),
            object_pairs_hook=_claims_object,
            parse_constant=_reject_constant,
        )
        now = clock()
        if (
            not isinstance(claims, dict)
            or claims["kind"] != kind
            or not _finite_time(claims["expires"])
            or not _finite_time(now)
            or claims["expires"] <= now
        ):
            raise ValueError()
        return claims
    except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
        raise PermissionError("invalid or expired grant") from None
