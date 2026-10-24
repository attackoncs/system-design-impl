"""Immutable, validated limits for caches, fanout and bounded feed reads."""

import dataclasses
import math


def _positive_int(name, value, *, zero=False):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < (0 if zero else 1)
    ):
        raise ValueError(
            f"{name} must be a {'nonnegative' if zero else 'positive'} integer"
        )


def _duration(name, value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be finite and positive")


@dataclasses.dataclass(frozen=True)
class RateLimitConfig:
    max_posts: int = 10
    window_seconds: float = 3600.0

    def __post_init__(self):
        _positive_int("max_posts", self.max_posts)
        _duration("window_seconds", self.window_seconds)


@dataclasses.dataclass(frozen=True)
class CacheConfig:
    news_feed_max_entries: int = 500
    post_cache_hot_capacity: int = 1000
    post_cache_normal_capacity: int = 10000
    user_cache_capacity: int = 5000
    user_cache_ttl_seconds: float = 3600.0
    action_cache_capacity: int = 10000
    social_graph_cache_ttl_seconds: float = 300.0
    hot_cache_access_threshold: int = 5

    def __post_init__(self):
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if field.name.endswith("seconds"):
                _duration(field.name, value)
            else:
                _positive_int(field.name, value)


@dataclasses.dataclass(frozen=True)
class FanoutConfig:
    celebrity_threshold: int = 5000
    num_workers: int = 4
    queue_max_size: int = 10000
    batch_size: int = 100

    def __post_init__(self):
        for field in dataclasses.fields(self):
            _positive_int(
                field.name,
                getattr(self, field.name),
                zero=field.name == "queue_max_size",
            )


@dataclasses.dataclass(frozen=True)
class NewsFeedConfig:
    rate_limit: RateLimitConfig = dataclasses.field(default_factory=RateLimitConfig)
    cache: CacheConfig = dataclasses.field(default_factory=CacheConfig)
    fanout: FanoutConfig = dataclasses.field(default_factory=FanoutConfig)
    default_page_size: int = 20
    max_page_size: int = 100
    read_concurrency: int = 16
    max_following: int = 5000
    read_repair_seconds: float = 30.0
    coverage_max_users: int = 5000

    def __post_init__(self):
        _duration("read_repair_seconds", self.read_repair_seconds)
        for name in (
            "default_page_size",
            "max_page_size",
            "read_concurrency",
            "max_following",
            "coverage_max_users",
        ):
            _positive_int(name, getattr(self, name))
        if self.default_page_size > self.max_page_size:
            raise ValueError("default_page_size exceeds max_page_size")
