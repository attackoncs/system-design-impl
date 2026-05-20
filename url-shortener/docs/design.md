# Design: URL Shortener

## Architecture Overview

The URL shortener library follows a strategy pattern with pluggable hash strategies and storage backends. The main orchestrator (`URLShortener`) delegates code generation to a `HashStrategy` and persistence to a `StorageBackend`, keeping concerns cleanly separated.

```
┌─────────────────────────────────────────────────┐
│           Public API                             │
│   (URLShortener orchestrator)                   │
├─────────────────────────────────────────────────┤
│       Hash Strategy Layer                        │
│   (HashCollisionStrategy, Base62Strategy)        │
├─────────────────────────────────────────────────┤
│       Storage Layer                              │
│   (StorageBackend ABC + InMemoryStorage)         │
├─────────────────────────────────────────────────┤
│       Models & Exceptions                        │
│   (URLMapping, ClickRecord, RedirectType, etc.)  │
└─────────────────────────────────────────────────┘
```

## Project Structure

```
url-shortener/
├── pyproject.toml
├── README.md
├── docs/
│   ├── requirements.md
│   ├── design.md
│   └── tasks.md
├── src/
│   └── url_shortener/
│       ├── __init__.py          # Public API exports
│       ├── exceptions.py        # Custom exception hierarchy
│       ├── models.py            # Data models (ClickRecord, URLMapping, RedirectType)
│       ├── storage.py           # Storage abstraction (ABC + InMemoryStorage)
│       ├── strategies.py        # Hash strategies (ABC + implementations)
│       └── shortener.py         # Main URLShortener orchestrator
├── tests/
│   ├── __init__.py
│   ├── test_exceptions.py       # Unit tests for exceptions
│   ├── test_models.py           # Unit tests for data models
│   ├── test_storage.py          # Unit tests for storage
│   ├── test_strategies.py       # Unit tests for strategies
│   ├── test_shortener.py        # Unit tests for orchestrator
│   └── test_properties.py       # Property-based tests (Hypothesis)
└── examples/
    └── demo_server.py           # HTTP demo using stdlib http.server
```

## Component Design

### 1. Custom Exceptions (`exceptions.py`)

```python
class URLShortenerError(Exception):
    """Base exception for all URL shortener errors."""
    pass


class URLValidationError(URLShortenerError):
    """Raised when an invalid URL is submitted for shortening."""
    pass


class ShortCodeNotFoundError(URLShortenerError):
    """Raised when a short code does not exist in storage."""
    pass


class CollisionLimitExceededError(URLShortenerError):
    """Raised when hash collision resolution exceeds the maximum retry limit."""
    pass
```

### 2. Data Models (`models.py`)

```python
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
```

### 3. Storage Abstraction (`storage.py`)

```python
from abc import ABC, abstractmethod
from typing import Optional

from url_shortener.models import URLMapping, RedirectType


class StorageBackend(ABC):
    """Abstract interface for URL mapping persistence."""

    @abstractmethod
    def save(self, mapping: URLMapping) -> None:
        """Save a URL mapping to storage.

        Args:
            mapping: The URLMapping to persist.
        """
        ...

    @abstractmethod
    def get_by_short_code(self, short_code: str) -> Optional[URLMapping]:
        """Retrieve a URL mapping by its short code.

        Args:
            short_code: The 7-character short code.

        Returns:
            The URLMapping if found, None otherwise.
        """
        ...

    @abstractmethod
    def get_by_long_url(self, long_url: str) -> Optional[URLMapping]:
        """Retrieve a URL mapping by the original long URL.

        Args:
            long_url: The original long URL.

        Returns:
            The URLMapping if found, None otherwise.
        """
        ...

    @abstractmethod
    def exists(self, short_code: str) -> bool:
        """Check if a short code exists in storage.

        Args:
            short_code: The 7-character short code.

        Returns:
            True if the short code exists, False otherwise.
        """
        ...


class InMemoryStorage(StorageBackend):
    """In-memory storage backend using Python dictionaries.

    Maintains bidirectional lookup for O(1) access in both directions.
    """

    def __init__(self) -> None:
        self._by_short_code: dict[str, URLMapping] = {}
        self._by_long_url: dict[str, URLMapping] = {}

    def save(self, mapping: URLMapping) -> None:
        """Save mapping with bidirectional indexing."""
        self._by_short_code[mapping.short_code] = mapping
        self._by_long_url[mapping.long_url] = mapping

    def get_by_short_code(self, short_code: str) -> Optional[URLMapping]:
        """O(1) lookup by short code."""
        return self._by_short_code.get(short_code)

    def get_by_long_url(self, long_url: str) -> Optional[URLMapping]:
        """O(1) lookup by long URL."""
        return self._by_long_url.get(long_url)

    def exists(self, short_code: str) -> bool:
        """O(1) existence check."""
        return short_code in self._by_short_code
```

### 4. Hash Strategies (`strategies.py`)

```python
from abc import ABC, abstractmethod
from typing import Protocol
import hashlib
import struct

from url_shortener.storage import StorageBackend
from url_shortener.exceptions import CollisionLimitExceededError

# Base-62 character set: 0-9, a-z, A-Z
BASE62_CHARS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
SHORT_CODE_LENGTH = 7


class HashStrategy(ABC):
    """Abstract interface for short code generation strategies."""

    @abstractmethod
    def generate(self, long_url: str, storage: StorageBackend) -> str:
        """Generate a short code for the given long URL.

        Args:
            long_url: The URL to shorten.
            storage: Storage backend for collision checking.

        Returns:
            A 7-character short code from the base-62 alphabet.
        """
        ...


class IDGenerator(Protocol):
    """Protocol for unique numeric ID generation."""

    def next_id(self) -> int:
        """Return the next unique numeric ID."""
        ...


class AutoIncrementIDGenerator:
    """Default auto-incrementing ID generator.

    Thread-unsafe; suitable for single-threaded usage and testing.
    """

    def __init__(self, start: int = 1) -> None:
        self._counter = start

    def next_id(self) -> int:
        """Return the next sequential ID."""
        current = self._counter
        self._counter += 1
        return current


def encode_base62(number: int) -> str:
    """Encode a non-negative integer to a base-62 string.

    Args:
        number: Non-negative integer to encode.

    Returns:
        Base-62 encoded string (variable length).
    """
    if number == 0:
        return BASE62_CHARS[0]

    result = []
    while number > 0:
        number, remainder = divmod(number, 62)
        result.append(BASE62_CHARS[remainder])
    return "".join(reversed(result))


def decode_base62(encoded: str) -> int:
    """Decode a base-62 string back to an integer.

    Args:
        encoded: Base-62 encoded string.

    Returns:
        The decoded non-negative integer.

    Raises:
        ValueError: If the string contains invalid characters.
    """
    result = 0
    for char in encoded:
        index = BASE62_CHARS.index(char)
        if index == -1:
            raise ValueError(f"Invalid base-62 character: {char}")
        result = result * 62 + index
    return result


class HashCollisionStrategy(HashStrategy):
    """Hash-based strategy with collision resolution.

    Applies CRC32 to the long URL, encodes the result in base-62,
    takes the first 7 characters. On collision, appends a predefined
    string and rehashes.
    """

    DEFAULT_MAX_RETRIES = 10
    COLLISION_SUFFIX = "~rehash"

    def __init__(self, max_retries: int = DEFAULT_MAX_RETRIES) -> None:
        self._max_retries = max_retries

    def generate(self, long_url: str, storage: StorageBackend) -> str:
        """Generate short code with collision resolution.

        Args:
            long_url: The URL to shorten.
            storage: Storage backend for collision checking.

        Returns:
            A unique 7-character short code.

        Raises:
            CollisionLimitExceededError: If max retries exceeded.
        """
        url_to_hash = long_url

        for attempt in range(self._max_retries + 1):
            candidate = self._compute_code(url_to_hash)

            # Check if this code is free or already maps to our URL
            existing = storage.get_by_short_code(candidate)
            if existing is None or existing.long_url == long_url:
                return candidate

            # Collision: append suffix and retry
            url_to_hash = url_to_hash + self.COLLISION_SUFFIX

        raise CollisionLimitExceededError(
            f"Failed to generate unique short code for '{long_url}' "
            f"after {self._max_retries} retries"
        )

    def _compute_code(self, text: str) -> str:
        """Compute a 7-character base-62 code from text using CRC32."""
        # Use CRC32 for speed; produces a 32-bit unsigned integer
        crc = struct.unpack("I", struct.pack("i", hash_crc32(text)))[0]
        # Encode to base-62 and pad/truncate to 7 characters
        encoded = encode_base62(crc)
        return encoded[:SHORT_CODE_LENGTH].ljust(SHORT_CODE_LENGTH, "0")


def hash_crc32(text: str) -> int:
    """Compute CRC32 hash of a string, returning a signed 32-bit integer."""
    import zlib
    return zlib.crc32(text.encode("utf-8"))


class Base62Strategy(HashStrategy):
    """Base-62 conversion strategy using unique numeric IDs.

    Converts a unique ID to base-62, producing a guaranteed-unique
    short code without collision handling.
    """

    def __init__(self, id_generator: IDGenerator | None = None) -> None:
        self._id_generator = id_generator or AutoIncrementIDGenerator()

    def generate(self, long_url: str, storage: StorageBackend) -> str:
        """Generate short code from a unique numeric ID.

        Args:
            long_url: The URL to shorten (used for dedup check only).
            storage: Storage backend (used for dedup check only).

        Returns:
            A 7-character base-62 short code.
        """
        unique_id = self._id_generator.next_id()
        encoded = encode_base62(unique_id)
        # Pad to 7 characters with leading zeros
        return encoded.rjust(SHORT_CODE_LENGTH, "0")
```

### 5. URL Shortener Orchestrator (`shortener.py`)

```python
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from url_shortener.exceptions import (
    ShortCodeNotFoundError,
    URLValidationError,
)
from url_shortener.models import ClickRecord, RedirectType, URLMapping
from url_shortener.storage import InMemoryStorage, StorageBackend
from url_shortener.strategies import HashCollisionStrategy, HashStrategy


class URLShortener:
    """Main orchestrator for URL shortening and redirecting.

    Coordinates between hash strategies, storage backends, and
    click tracking to provide the full shorten/redirect flow.
    """

    def __init__(
        self,
        strategy: Optional[HashStrategy] = None,
        storage: Optional[StorageBackend] = None,
        default_redirect_type: RedirectType = RedirectType.TEMPORARY,
        domain: str = "http://short.url",
    ) -> None:
        """Initialize the URL shortener.

        Args:
            strategy: Hash strategy for code generation. Defaults to HashCollisionStrategy.
            storage: Storage backend for persistence. Defaults to InMemoryStorage.
            default_redirect_type: Default redirect type for new mappings. Defaults to 302.
            domain: Custom domain for constructing short URLs.
        """
        self._strategy = strategy or HashCollisionStrategy()
        self._storage = storage or InMemoryStorage()
        self._default_redirect_type = default_redirect_type
        self._domain = domain.rstrip("/")
        self._clicks: dict[str, list[ClickRecord]] = {}

    def shorten(
        self,
        long_url: str,
        redirect_type: Optional[RedirectType] = None,
    ) -> str:
        """Shorten a long URL to a short code.

        Args:
            long_url: The URL to shorten.
            redirect_type: Override redirect type for this mapping.

        Returns:
            The full short URL (domain + "/" + short_code).

        Raises:
            URLValidationError: If the URL is invalid.
        """
        self._validate_url(long_url)

        # Check if already shortened (idempotence)
        existing = self._storage.get_by_long_url(long_url)
        if existing is not None:
            return f"{self._domain}/{existing.short_code}"

        # Generate short code via strategy
        short_code = self._strategy.generate(long_url, self._storage)

        # Store the mapping
        mapping = URLMapping(
            short_code=short_code,
            long_url=long_url,
            redirect_type=redirect_type or self._default_redirect_type,
        )
        self._storage.save(mapping)

        return f"{self._domain}/{short_code}"

    def resolve(
        self,
        short_code: str,
        client_id: Optional[str] = None,
    ) -> tuple[str, RedirectType]:
        """Resolve a short code to the original long URL.

        Args:
            short_code: The 7-character short code to resolve.
            client_id: Optional client identifier for analytics.

        Returns:
            Tuple of (long_url, redirect_type).

        Raises:
            ShortCodeNotFoundError: If the short code doesn't exist.
        """
        mapping = self._storage.get_by_short_code(short_code)
        if mapping is None:
            raise ShortCodeNotFoundError(
                f"Short code '{short_code}' not found"
            )

        # Record click for analytics
        click = ClickRecord(
            short_code=short_code,
            timestamp=datetime.now(timezone.utc),
            client_id=client_id,
        )
        self._clicks.setdefault(short_code, []).append(click)

        return mapping.long_url, mapping.redirect_type

    def get_click_count(self, short_code: str) -> int:
        """Get the total number of clicks for a short code.

        Args:
            short_code: The short code to query.

        Returns:
            Total click count (0 if no clicks recorded).
        """
        return len(self._clicks.get(short_code, []))

    def get_click_records(self, short_code: str) -> list[ClickRecord]:
        """Get all click records for a short code.

        Args:
            short_code: The short code to query.

        Returns:
            List of ClickRecord objects (empty if no clicks).
        """
        return list(self._clicks.get(short_code, []))

    def _validate_url(self, url: str) -> None:
        """Validate that a URL is well-formed.

        Args:
            url: The URL to validate.

        Raises:
            URLValidationError: If the URL is invalid.
        """
        if not url or not url.strip():
            raise URLValidationError("URL cannot be empty")

        parsed = urlparse(url)
        if not parsed.scheme:
            raise URLValidationError(
                f"URL missing scheme (http/https): '{url}'"
            )
        if not parsed.netloc:
            raise URLValidationError(
                f"URL missing network location: '{url}'"
            )
```

### 6. Public API (`__init__.py`)

```python
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
```

### 7. HTTP Demo Server (`examples/demo_server.py`)

```python
"""Lightweight HTTP demo server using only Python stdlib.

Endpoints:
    POST /shorten         - Body: {"url": "...", "redirect_type": 301|302}
                            Returns: 201 with {"short_url": "..."}
    GET  /<short_code>    - Redirects (301/302) to the original URL
    GET  /stats/<code>    - Returns click stats as JSON

Usage:
    python -m examples.demo_server --port 8000
"""

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from urllib.parse import urlparse

from url_shortener import URLShortener, RedirectType, ShortCodeNotFoundError


class URLShortenerHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the URL shortener demo."""

    shortener: URLShortener  # Class-level shared instance

    def do_POST(self) -> None:
        """Handle POST /shorten requests."""
        if self.path != "/shorten":
            self._send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON")
            return

        long_url = data.get("url", "")
        redirect_type_code = data.get("redirect_type", 302)
        redirect_type = RedirectType(redirect_type_code)

        try:
            short_url = self.shortener.shorten(long_url, redirect_type)
        except Exception as e:
            self._send_error(HTTPStatus.BAD_REQUEST, str(e))
            return

        self._send_json(HTTPStatus.CREATED, {"short_url": short_url})

    def do_GET(self) -> None:
        """Handle GET /<short_code> and GET /stats/<code> requests."""
        path = self.path.lstrip("/")

        if path.startswith("stats/"):
            short_code = path[len("stats/"):]
            count = self.shortener.get_click_count(short_code)
            self._send_json(HTTPStatus.OK, {
                "short_code": short_code,
                "click_count": count,
            })
            return

        # Treat as short code redirect
        short_code = path
        try:
            long_url, redirect_type = self.shortener.resolve(short_code)
        except ShortCodeNotFoundError:
            self._send_error(HTTPStatus.NOT_FOUND, "Short code not found")
            return

        self.send_response(redirect_type.value)
        self.send_header("Location", long_url)
        self.end_headers()

    def _send_json(self, status: HTTPStatus, data: dict) -> None:
        """Send a JSON response."""
        body = json.dumps(data).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        """Send an error JSON response."""
        self._send_json(status, {"error": message})


def run_server(port: int = 8000, domain: str = "http://localhost:8000") -> None:
    """Start the demo HTTP server."""
    shortener = URLShortener(domain=domain)
    URLShortenerHandler.shortener = shortener

    server = HTTPServer(("", port), URLShortenerHandler)
    print(f"URL Shortener demo running on http://localhost:{port}")
    print("Endpoints:")
    print(f"  POST /shorten  - Shorten a URL")
    print(f"  GET /<code>    - Redirect to original URL")
    print(f"  GET /stats/<code> - View click stats")
    server.serve_forever()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="URL Shortener Demo Server")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    run_server(port=args.port, domain=f"http://localhost:{args.port}")
```

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Strategy pattern | ABC-based | Clean separation of hash algorithms; easy to add new strategies |
| Default strategy | HashCollisionStrategy | No external ID generator needed; works standalone |
| Hash function | CRC32 | Fast, stdlib-available; sufficient for 7-char codes |
| Collision resolution | Append suffix + rehash | Simple, deterministic; matches System Design Interview approach |
| Max retries | 10 | Practical limit; 62^7 space makes exhaustion extremely unlikely |
| Base-62 charset | `[0-9, a-z, A-Z]` | Standard ordering; 62 chars × 7 positions = ~3.5T codes |
| Storage interface | ABC with 4 methods | Minimal surface area; bidirectional lookup enables idempotence |
| In-memory storage | Dual dict | O(1) both directions; simple and fast for testing/demos |
| Click tracking | In-memory list per code | Simple analytics; production would use persistent storage |
| URL validation | `urllib.parse.urlparse` | Stdlib; checks scheme and netloc presence |
| Data models | Frozen dataclasses | Immutable value objects; hashable and safe |
| ID generator | Protocol (structural typing) | Maximum flexibility; any callable with `next_id()` works |
| Demo server | `http.server` | Zero dependencies; demonstrates full flow |
| Redirect default | 302 (temporary) | Safer default; allows analytics and URL updates |

## Error Handling

| Error Condition | Exception | When Raised |
|----------------|-----------|-------------|
| Empty/malformed URL | `URLValidationError` | `shorten()` with invalid input |
| Missing scheme | `URLValidationError` | `shorten()` with no http/https |
| Unknown short code | `ShortCodeNotFoundError` | `resolve()` with non-existent code |
| Collision limit hit | `CollisionLimitExceededError` | `HashCollisionStrategy.generate()` after max retries |
| Invalid base-62 char | `ValueError` | `decode_base62()` with bad input |

All custom exceptions inherit from `URLShortenerError` for catch-all handling.

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system—essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Short code format invariant

*For any* valid long URL submitted to any hash strategy (HashCollisionStrategy or Base62Strategy), the generated short code SHALL be exactly 7 characters in length and contain only characters from the alphabet [0-9, a-z, A-Z].

**Validates: Requirements 1.1, 3.2, 4.2, 4.3**

### Property 2: Shorten/resolve round-trip

*For any* valid long URL that is successfully shortened, resolving the resulting short code SHALL return the original long URL unchanged.

**Validates: Requirements 2.1, 5.3**

### Property 3: Shortening idempotence

*For any* valid long URL, shortening it multiple times SHALL always return the same short URL.

**Validates: Requirements 1.3**

### Property 4: Invalid URL rejection

*For any* string that is empty, contains only whitespace, lacks a URL scheme, or lacks a network location, the shortener SHALL raise a URLValidationError.

**Validates: Requirements 1.4**

### Property 5: Non-existent code error

*For any* 7-character base-62 string that has not been stored as a mapping, resolving it SHALL raise a ShortCodeNotFoundError.

**Validates: Requirements 2.2**

### Property 6: Redirect type preservation

*For any* valid long URL shortened with a specified redirect type (301 or 302), resolving the short code SHALL return that same redirect type. When no redirect type is specified, the stored type SHALL be 302.

**Validates: Requirements 1.5, 1.6, 2.4, 5.4**

### Property 7: Collision resolution uniqueness

*For any* set of distinct long URLs shortened using the HashCollisionStrategy, each SHALL receive a distinct short code (no two different URLs map to the same code).

**Validates: Requirements 3.3, 3.4**

### Property 8: Click tracking accuracy

*For any* short code that is resolved N times, the click count SHALL equal N and the click records list SHALL contain exactly N entries, each with the correct short code and a valid timestamp.

**Validates: Requirements 2.3, 6.1, 6.2, 6.3**

### Property 9: Base-62 encoding round-trip

*For any* non-negative integer within the representable range (0 to 62^7 - 1), encoding to base-62 and then decoding back SHALL produce the original integer.

**Validates: Requirements 4.2**
