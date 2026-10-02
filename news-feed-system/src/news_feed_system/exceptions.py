class NewsFeedError(Exception):
    """Base exception for all news feed system errors."""
    pass


class ValidationError(NewsFeedError):
    """Raised when a feed request is invalid (empty content, missing fields)."""

    def __init__(self, message: str, field: str = "") -> None:
        self.field = field
        super().__init__(message)


class AuthenticationError(NewsFeedError):
    """Raised when an auth token is invalid or missing."""

    def __init__(self, message: str = "Invalid or missing authentication token") -> None:
        super().__init__(message)


class RateLimitError(NewsFeedError):
    """Raised when post publishing rate limit is exceeded."""

    def __init__(self, user_id: str, limit: int, window_seconds: float) -> None:
        self.user_id = user_id
        self.limit = limit
        self.window_seconds = window_seconds
        super().__init__(
            f"Rate limit exceeded for user '{user_id}': "
            f"{limit} posts per {window_seconds}s"
        )


class CacheMissError(NewsFeedError):
    """Raised when a cache lookup fails and requires fallback handling."""

    def __init__(self, cache_name: str, key: str) -> None:
        self.cache_name = cache_name
        self.key = key
        super().__init__(f"Cache miss in '{cache_name}' for key '{key}'")


class FanoutError(NewsFeedError):
    """Raised when the fanout process fails."""

    def __init__(self, post_id: str, reason: str) -> None:
        self.post_id = post_id
        self.reason = reason
        super().__init__(f"Fanout failed for post '{post_id}': {reason}")


class GraphError(NewsFeedError):
    """Raised when a social graph operation fails."""

    def __init__(self, operation: str, reason: str) -> None:
        self.operation = operation
        self.reason = reason
        super().__init__(f"Graph operation '{operation}' failed: {reason}")
