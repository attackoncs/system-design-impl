"""Immutable public chat models."""
from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class ChatConfig:
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


@dataclass(frozen=True)
class Message:
    message_id: int
    channel_id: str
    sender_id: str
    content: str
    created_at: float
    client_message_id: str


@dataclass(frozen=True)
class Channel:
    channel_id: str
    owner_id: str
    kind: str
    members: Tuple[str, ...]


class ChatError(Exception):
    """Invalid request or inaccessible resource."""


class AuthenticationError(ChatError):
    """Invalid credentials or expired session."""
