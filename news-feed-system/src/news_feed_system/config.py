from dataclasses import dataclass, field


@dataclass
class RateLimitConfig:
    """Rate limit configuration for post publishing."""
    max_posts: int = 10
    window_seconds: float = 3600.0


@dataclass
class CacheConfig:
    """Configuration for cache layers."""
    news_feed_max_entries: int = 500
    post_cache_hot_capacity: int = 1000
    post_cache_normal_capacity: int = 10000
    user_cache_capacity: int = 5000
    user_cache_ttl_seconds: float = 3600.0
    action_cache_capacity: int = 10000
    social_graph_cache_ttl_seconds: float = 300.0
    hot_cache_access_threshold: int = 5  # accesses to promote to hot


@dataclass
class FanoutConfig:
    """Configuration for the fanout pipeline."""
    celebrity_threshold: int = 5000
    num_workers: int = 4
    queue_max_size: int = 10000
    batch_size: int = 100  # followers per FanoutTask


@dataclass
class NewsFeedConfig:
    """Top-level configuration for the news feed system."""
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    fanout: FanoutConfig = field(default_factory=FanoutConfig)
    default_page_size: int = 20

    def __post_init__(self) -> None:
        """Validate configuration values."""
        if self.rate_limit.max_posts <= 0:
            raise ValueError(
                f"max_posts must be positive, got {self.rate_limit.max_posts}"
            )
        if self.rate_limit.window_seconds <= 0:
            raise ValueError(
                f"window_seconds must be positive, got {self.rate_limit.window_seconds}"
            )
        if self.fanout.celebrity_threshold <= 0:
            raise ValueError(
                f"celebrity_threshold must be positive, got {self.fanout.celebrity_threshold}"
            )
        if self.fanout.num_workers <= 0:
            raise ValueError(
                f"num_workers must be positive, got {self.fanout.num_workers}"
            )
        if self.cache.news_feed_max_entries <= 0:
            raise ValueError(
                f"news_feed_max_entries must be positive, got {self.cache.news_feed_max_entries}"
            )
