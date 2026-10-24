"""Immutable public chat models."""

import dataclasses
import math
from typing import Tuple


@dataclasses.dataclass(frozen=True)
class ChatConfig:
    """Validated limits for group membership, messages, and live sessions.

    Attributes:
        max_group_members: Maximum group size, including its owner (2 to 100).
        max_message_length: Maximum number of characters in a message.
        heartbeat_timeout: Seconds before a silent session expires.
        session_queue_size: Maximum number of queued events per device.
    """

    max_group_members: int = 100
    max_message_length: int = 99_999
    heartbeat_timeout: float = 30.0
    session_queue_size: int = 100
    push_timeout: float = 10.0
    push_concurrency: int = 8
    failure_retention: int = 1000

    def __post_init__(self):
        require_integer(self.max_group_members, "max_group_members", 2, 100)
        require_integer(self.max_message_length, "max_message_length", 1, 99_999)
        require_integer(self.session_queue_size, "session_queue_size")
        require_integer(self.push_concurrency, "push_concurrency")
        require_integer(self.failure_retention, "failure_retention")
        require_positive_number(self.heartbeat_timeout, "heartbeat_timeout")
        require_positive_number(self.push_timeout, "push_timeout")


def require_integer(value, name, minimum=1, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")


def require_positive_number(value, name):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be finite and positive")


def validate_user_id(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ChatError("invalid user")
    return value


@dataclasses.dataclass(frozen=True)
class Message:
    """An immutable persisted text message.

    Attributes:
        message_id: Monotonically allocated message identifier.
        channel_id: Destination channel identifier.
        sender_id: Authenticated sender identifier.
        content: Message text.
        created_at: Creation timestamp in epoch seconds.
        client_message_id: Sender-provided idempotency key.
    """

    message_id: int
    channel_id: str
    sender_id: str
    content: str
    created_at: float
    client_message_id: str


@dataclasses.dataclass(frozen=True)
class Channel:
    """An immutable channel membership snapshot.

    Attributes:
        channel_id: Unique channel identifier.
        owner_id: Group creator or direct-channel creator.
        kind: Either "direct" or "group".
        members: Member user IDs.
    """

    channel_id: str
    owner_id: str
    kind: str
    members: Tuple[str, ...]


class ChatError(Exception):
    """Invalid request or inaccessible resource."""


class AuthenticationError(ChatError):
    """Invalid credentials or expired session."""
