"""Data models for the news feed system."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional


class ActionType(Enum):
    """Types of user interactions with posts."""
    LIKED = "liked"
    REPLIED = "replied"
    SHARED = "shared"
    OTHER = "other"


class CounterType(Enum):
    """Types of counters tracked by the system."""
    LIKE = "like"
    REPLY = "reply"
    FOLLOWER = "follower"
    FOLLOWING = "following"


@dataclass(frozen=True)
class Post:
    """A user-published content item."""
    post_id: str
    author_id: str
    content: str
    media_urls: List[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    selective_sharing: Optional[List[str]] = None  # None = public to all followers


@dataclass(frozen=True)
class FeedEntry:
    """A single item in a user's news feed cache (<post_id, user_id> mapping)."""
    post_id: str
    author_id: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class UserProfile:
    """User profile information for feed hydration."""
    user_id: str
    username: str
    profile_picture_url: str = ""
    follower_count: int = 0


@dataclass(frozen=True)
class ActionState:
    """User interaction state for a specific post."""
    liked: bool = False
    replied: bool = False
    other_actions: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class PostCounters:
    """Engagement counters for a post."""
    like_count: int = 0
    reply_count: int = 0


@dataclass(frozen=True)
class HydratedFeedEntry:
    """A fully assembled feed entry with all display data."""
    post: Post
    author: UserProfile
    actions: ActionState
    counters: PostCounters


@dataclass(frozen=True)
class HydratedFeed:
    """A complete hydrated news feed response."""
    entries: List[HydratedFeedEntry] = field(default_factory=list)
    has_more: bool = False
    next_cursor: Optional[str] = None


@dataclass(frozen=True)
class FeedRequest:
    """A request to publish or retrieve a news feed."""
    user_id: str
    auth_token: str
    content: str = ""  # Only for publish requests
    media_urls: List[str] = field(default_factory=list)
    selective_sharing: Optional[List[str]] = None


@dataclass(frozen=True)
class FanoutTask:
    """A message placed on the Message_Queue for async fanout processing."""
    post_id: str
    author_id: str
    target_user_ids: List[str]
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
