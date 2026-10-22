"""Data models for the web crawler."""

from __future__ import annotations

import dataclasses
import enum
import urllib.parse
from typing import Dict, List, Optional


class CrawlStatus(enum.Enum):
    """Status of a single URL crawl attempt."""

    SUCCESS = "success"
    ERROR = "error"
    DUPLICATE = "duplicate"
    DISALLOWED = "disallowed"
    FILTERED = "filtered"


class Priority(enum.Enum):
    """URL crawl priority levels for the front queue."""

    HIGH = 0
    MEDIUM = 1
    LOW = 2


@dataclasses.dataclass
class FrontierEntry:
    """An entry in the URL frontier with metadata."""

    url: str
    priority: Priority = Priority.MEDIUM
    depth: int = 0
    host: str = ""

    def __post_init__(self) -> None:
        if not self.host:
            self.host = urllib.parse.urlparse(self.url).netloc.lower()

    def __lt__(self, other: "FrontierEntry") -> bool:
        """Compare by priority value for heapq ordering."""
        return self.priority.value < other.priority.value


@dataclasses.dataclass
class CrawlResult:
    """Result of crawling a single URL."""

    url: str
    status: CrawlStatus
    http_status: Optional[int] = None
    content_length: Optional[int] = None
    content_fingerprint: Optional[str] = None
    links: List[str] = dataclasses.field(default_factory=list)
    depth: int = 0
    error_message: Optional[str] = None
    headers: Dict[str, str] = dataclasses.field(default_factory=dict)
