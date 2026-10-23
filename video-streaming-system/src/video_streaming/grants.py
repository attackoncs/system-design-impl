"""Signed upload/playback grants independent of persistence and scheduling."""

import base64
import hashlib
import hmac
import json


def sign(secret, value):
    data = (
        base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    return data + "." + hmac.new(secret, data.encode(), hashlib.sha256).hexdigest()


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
        if not isinstance(token, str) or len(token) > 2048:
            raise ValueError()
        data, signature = token.split(".")
        expected = hmac.new(secret, data.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        claims = json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))
        if claims["kind"] != kind or claims["expires"] <= clock():
            raise ValueError()
        return claims
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise PermissionError("invalid or expired grant") from None
