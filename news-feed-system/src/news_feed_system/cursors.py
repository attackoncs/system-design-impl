"""Validated keyset cursors for stable reverse chronological pagination."""

import base64
import binascii
import datetime as dt
import json

from news_feed_system.exceptions import ValidationError
from news_feed_system.storage import PostKey, timestamp


def encode_cursor(key: PostKey) -> str:
    data = json.dumps([timestamp(key[0]), key[1]], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def decode_cursor(value: str) -> PostKey:
    try:
        if not isinstance(value, str) or not value or len(value) > 1024:
            raise ValueError("Invalid length")
        raw = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
        data = json.loads(raw)
        if not isinstance(data, list) or len(data) != 2:
            raise ValueError("Invalid structure")
        instant = dt.datetime.fromisoformat(data[0])
        timestamp(instant)
        if not isinstance(data[1], str) or not data[1] or len(data[1]) > 256:
            raise ValueError("Invalid post ID")
        return instant, data[1]
    except (ValueError, TypeError, KeyError, binascii.Error, UnicodeError) as exc:
        raise ValidationError("Invalid feed cursor", field="cursor") from exc
