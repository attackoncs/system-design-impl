"""Immutable public chat models."""

import dataclasses
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

    def __post_init__(self):
        if not 2 <= self.max_group_members <= 100:
            raise ValueError("group limit must be between 2 and 100")
        if not 1 <= self.max_message_length < 100_000:
            raise ValueError("message limit must be less than 100000")
        if self.heartbeat_timeout <= 0 or self.session_queue_size < 1:
            raise ValueError("timeouts and queue sizes must be positive")


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
