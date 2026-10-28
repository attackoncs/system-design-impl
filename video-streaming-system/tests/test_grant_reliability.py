"""Malformed signed claims cannot bypass expiry or crash request handling."""

import base64
import hashlib
import hmac

import pytest

from video_streaming import grants

SECRET = b"test-secret"


def signed_json(raw):
    data = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    return data + "." + hmac.new(SECRET, data.encode(), hashlib.sha256).hexdigest()


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        "null",
        '"a"',
        "3",
        '{"kind":"play","expires":NaN}',
        '{"kind":"play","expires":Infinity}',
        '{"kind":"play","expires":true}',
        '{"kind":"play","expires":"20"}',
        '{"kind":"play","expires":null}',
        '{"kind":"play","expires":{}}',
        '{"kind":"play","expires":20,"expires":100}',
        '{"kind":"upload","kind":"play","expires":20}',
        '{"kind":"play","expires":20,"ignored":NaN}',
    ],
)
def test_invalid_signed_claims_are_permission_errors(raw):
    with pytest.raises(PermissionError):
        grants.verify(SECRET, lambda: 10, signed_json(raw), "play")


@pytest.mark.parametrize("clock", [True, float("nan"), float("inf"), "10", None])
def test_invalid_clock_cannot_disable_expiry(clock):
    token = grants.sign(SECRET, {"kind": "play", "expires": 20})
    with pytest.raises(PermissionError):
        grants.verify(SECRET, lambda: clock, token, "play")


def test_sign_does_not_emit_nonstandard_json():
    with pytest.raises(ValueError):
        grants.sign(SECRET, {"kind": "play", "expires": float("nan")})
