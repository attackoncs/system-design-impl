"""URL mappings, access statistics, and shortening configuration."""

import dataclasses
import datetime
import enum
from typing import Optional


class RedirectType(enum.IntEnum):
    """HTTP redirect status codes."""

    PERMANENT = 301
    TEMPORARY = 302


@dataclasses.dataclass(frozen=True)
class URLMapping:
    """A mapping between a short code and a long URL."""

    short_code: str
    long_url: str
    redirect_type: RedirectType = RedirectType.TEMPORARY
    created_at: datetime.datetime = dataclasses.field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )


@dataclasses.dataclass(frozen=True)
class ClickRecord:
    """A single click/redirect event for analytics."""

    short_code: str
    timestamp: datetime.datetime = dataclasses.field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    client_id: Optional[str] = None
