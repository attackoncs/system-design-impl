"""Configuration module for the web crawler."""

from __future__ import annotations

import dataclasses
import math
from typing import Callable, List, Optional


@dataclasses.dataclass
class CrawlerConfig:
    """Configuration for the web crawler.

    All numeric values must be positive. Raises ValueError on invalid input.
    """

    max_pages: int = 100
    max_depth: int = 3
    max_concurrent: int = 10
    per_host_delay: float = 1.0
    request_timeout: float = 30.0
    user_agent: str = "WebCrawler/1.0"
    robots_cache_ttl: float = 3600.0

    # Filter configuration
    extension_blacklist: List[str] = dataclasses.field(
        default_factory=lambda: [
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".svg",
            ".pdf",
            ".zip",
            ".exe",
            ".mp3",
            ".mp4",
            ".avi",
        ]
    )
    domain_whitelist: Optional[List[str]] = None
    domain_blacklist: Optional[List[str]] = None
    max_url_length: int = 2048
    custom_filters: List[Callable[[str], bool]] = dataclasses.field(
        default_factory=list
    )

    # Hook configuration
    content_hooks: List[Callable] = dataclasses.field(default_factory=list)

    # Optional durable scheduling. Each file has exactly one active crawler.
    state_path: Optional[str] = None
    recrawl_interval_seconds: Optional[float] = None
    max_retries: int = 2
    retry_delay_seconds: float = 1.0
    max_response_bytes: int = 10 * 1024 * 1024
    max_redirects: int = 5

    def __post_init__(self) -> None:
        for name in (
            "max_pages",
            "max_depth",
            "max_concurrent",
            "max_url_length",
            "max_response_bytes",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("max_retries", "max_redirects"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        for name in (
            "per_host_delay",
            "request_timeout",
            "robots_cache_ttl",
            "retry_delay_seconds",
        ):
            value = getattr(self, name)
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be finite and positive")
        if self.recrawl_interval_seconds is not None:
            value = self.recrawl_interval_seconds
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError("recrawl_interval_seconds must be finite and positive")
            if not self.state_path:
                raise ValueError("recrawling requires state_path")
