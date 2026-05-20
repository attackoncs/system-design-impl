from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import Optional


class RedirectType(IntEnum):
    """HTTP redirect status codes."""
    PERMANENT = 301
    TEMPORARY = 302


@dataclass(frozen=True)
class URLMapping:
    """A mapping between a short code and a long URL."""
    short_code: str
    long_url: str
    redirect_type: RedirectType = RedirectType.TEMPORARY
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class ClickRecord:
    """A single click/redirect event for analytics."""
    short_code: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    client_id: Optional[str] = None
