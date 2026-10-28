"""Framework-independent rule configuration shared by HTTP adapters."""

from __future__ import annotations

from typing import Optional

from rate_limiter import config, keys


def build_rule(
    limit: int,
    window: int,
    algorithm: str = "token_bucket",
    key_type: str = "ip",
    path_pattern: Optional[str] = None,
) -> config.RateLimitRule:
    """Resolve a named algorithm and request key function consistently."""
    try:
        selected = config.Algorithm(algorithm.lower())
    except ValueError as error:
        raise ValueError(
            f"Unknown algorithm: {algorithm}. Valid options:"
            f" {', '.join(item.value for item in config.Algorithm)}"
        ) from error
    functions = {
        "ip": keys.ip_key,
        "user_id": keys.user_id_key,
        "path": keys.path_key,
        "method": keys.method_key,
    }
    if key_type not in functions:
        raise ValueError(f"Unknown key type: {key_type}")
    return config.RateLimitRule(
        limit=limit,
        window=window,
        algorithm=selected,
        key_func=None,
        key_type=key_type,
        path_pattern=path_pattern,
    )


class RateLimitConfig:
    """Build rule lists for Flask or FastAPI without importing either framework."""

    def __init__(self) -> None:
        self.rules: list[config.RateLimitRule] = []

    def add_rule(
        self,
        limit: int,
        window: int,
        algorithm: str = "token_bucket",
        key_type: str = "ip",
        path_pattern: Optional[str] = None,
    ) -> None:
        """Append a rule; path_pattern is a case-sensitive glob over the URL path."""
        self.rules.append(build_rule(limit, window, algorithm, key_type, path_pattern))

    def get_config(self) -> config.RateLimiterConfig:
        """Return a configuration with its own rule-list container."""
        return config.RateLimiterConfig(rules=list(self.rules))
