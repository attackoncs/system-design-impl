"""Canonical text-message contract shared by clients and coordinator."""

import json

from .common import dumps, name


def freeze_messages(messages, maximum=100):
    if not isinstance(messages, list) or not 1 <= len(messages) <= maximum:
        raise ValueError(f"batch requires 1..{maximum} messages")
    # Capture before queuing/retrying: caller mutation must not change a sequence.
    owned = json.loads(dumps(messages))
    for message in owned:
        if not isinstance(message, dict) or set(message) - {
            "value",
            "key",
            "delay_seconds",
            "deliver_at",
            "tags",
        }:
            raise ValueError("invalid message fields")
        if (
            not isinstance(message.get("value"), str)
            or len(message["value"].encode()) > 16384
        ):
            raise ValueError("text messages must be at most 16 KiB")
        key = message.get("key")
        if key is not None and (not isinstance(key, str) or len(key) > 1000):
            raise ValueError("invalid message key")
        if "delay_seconds" in message and "deliver_at" in message:
            raise ValueError("choose relative or absolute delivery time")
        for field in ("delay_seconds", "deliver_at"):
            if field in message:
                value = message[field]
                if type(value) not in (int, float) or not 0 <= value <= (
                    86400 if field == "delay_seconds" else 2**53 - 1
                ):
                    raise ValueError("invalid delivery time")
        tags = message.get("tags", [])
        if (
            not isinstance(tags, list)
            or len(tags) > 16
            or any(not isinstance(tag, str) for tag in tags)
        ):
            raise ValueError("invalid tags")
        if len(set(tags)) != len(tags):
            raise ValueError("tags must be unique")
        for tag in tags:
            name(tag)
    return owned
