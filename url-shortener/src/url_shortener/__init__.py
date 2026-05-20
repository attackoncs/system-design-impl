"""URL Shortener - A pluggable URL shortening library."""

from url_shortener.exceptions import (
    CollisionLimitExceededError,
    ShortCodeNotFoundError,
    URLShortenerError,
    URLValidationError,
)
from url_shortener.models import ClickRecord, RedirectType, URLMapping
from url_shortener.shortener import URLShortener
from url_shortener.storage import InMemoryStorage, StorageBackend
from url_shortener.strategies import (
    AutoIncrementIDGenerator,
    Base62Strategy,
    HashCollisionStrategy,
    HashStrategy,
    IDGenerator,
    encode_base62,
    decode_base62,
)

__all__ = [
    # Core
    "URLShortener",
    # Models
    "ClickRecord",
    "RedirectType",
    "URLMapping",
    # Storage
    "InMemoryStorage",
    "StorageBackend",
    # Strategies
    "AutoIncrementIDGenerator",
    "Base62Strategy",
    "HashCollisionStrategy",
    "HashStrategy",
    "IDGenerator",
    "encode_base62",
    "decode_base62",
    # Exceptions
    "CollisionLimitExceededError",
    "ShortCodeNotFoundError",
    "URLShortenerError",
    "URLValidationError",
]
