"""Configuration module for the web crawler."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional


@dataclass
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
    extension_blacklist: List[str] = field(default_factory=lambda: [
        ".jpg", ".jpeg", ".png", ".gif", ".svg",
        ".pdf", ".zip", ".exe", ".mp3", ".mp4", ".avi",
    ])
    domain_whitelist: Optional[List[str]] = None
    domain_blacklist: Optional[List[str]] = None
    max_url_length: int = 2048
    custom_filters: List[Callable[[str], bool]] = field(default_factory=list)

    # Hook configuration
    content_hooks: List[Callable] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Validate configuration values."""
        if self.max_pages <= 0:
            raise ValueError(f"max_pages must be positive, got {self.max_pages}")
        if self.max_depth <= 0:
            raise ValueError(f"max_depth must be positive, got {self.max_depth}")
        if self.max_concurrent <= 0:
            raise ValueError(f"max_concurrent must be positive, got {self.max_concurrent}")
        if self.per_host_delay <= 0:
            raise ValueError(f"per_host_delay must be positive, got {self.per_host_delay}")
        if self.request_timeout <= 0:
            raise ValueError(f"request_timeout must be positive, got {self.request_timeout}")
        if self.max_url_length <= 0:
            raise ValueError(f"max_url_length must be positive, got {self.max_url_length}")
        if self.robots_cache_ttl <= 0:
            raise ValueError(f"robots_cache_ttl must be positive, got {self.robots_cache_ttl}")
