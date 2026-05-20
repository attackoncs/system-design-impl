# URL Shortener

A Python URL shortener library implementing the design from *System Design Interview* Chapter 9 "Design A URL Shortener". Features pluggable hash strategies (hash+collision resolution and base-62 conversion), a storage abstraction with in-memory default, click tracking analytics, and a stdlib HTTP demo server. Zero runtime dependencies.

## Features

- **Pluggable hash strategies** — choose between hash+collision resolution or base-62 conversion
- **Storage abstraction** — swap between in-memory (default) and custom persistent backends
- **Click tracking** — built-in analytics with per-URL click counts and timestamped records
- **Zero runtime dependencies** — pure Python stdlib; dev tools (pytest, hypothesis) are optional
- **Type-annotated** — full type hints on all public interfaces
- **7-character short codes** — base-62 alphabet [0-9, a-z, A-Z] supporting ~3.5 trillion unique codes

## Architecture

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

### Component Responsibilities

| Component | Role |
|-----------|------|
| `URLShortener` | Orchestrates shortening, resolving, and click tracking |
| `HashCollisionStrategy` | CRC32 hash + collision resolution via rehashing |
| `Base62Strategy` | Unique ID → base-62 conversion, guaranteed collision-free |
| `StorageBackend` | Abstract interface for URL mapping persistence |
| `InMemoryStorage` | Default backend with O(1) bidirectional lookup |

## Installation

```bash
cd url-shortener
pip install -e .
```

With development dependencies (pytest + hypothesis):

```bash
pip install -e ".[dev]"
```

### Requirements

- Python >= 3.9

## Quick Start

```python
from url_shortener import URLShortener, Base62Strategy, RedirectType

# Create a shortener with default settings (hash+collision strategy)
shortener = URLShortener(domain="https://short.io")

# Shorten a URL
short_url = shortener.shorten("https://example.com/very/long/path")
print(short_url)  # https://short.io/a3Bf92k

# Resolve back to the original
long_url, redirect_type = shortener.resolve("a3Bf92k")
print(long_url)        # https://example.com/very/long/path
print(redirect_type)   # RedirectType.TEMPORARY (302)

# Check click analytics
count = shortener.get_click_count("a3Bf92k")
records = shortener.get_click_records("a3Bf92k")
print(f"Clicks: {count}")

# Use base-62 strategy instead
shortener = URLShortener(
    strategy=Base62Strategy(),
    domain="https://short.io",
    default_redirect_type=RedirectType.PERMANENT,
)

short_url = shortener.shorten("https://example.com/another/page")
print(short_url)  # https://short.io/0000001
```

### Idempotent Shortening

Shortening the same URL multiple times returns the same short URL:

```python
url = "https://example.com/page"
result1 = shortener.shorten(url)
result2 = shortener.shorten(url)
assert result1 == result2
```

### Custom Redirect Types

```python
# Store with 301 (permanent) redirect
short_url = shortener.shorten(
    "https://example.com/moved",
    redirect_type=RedirectType.PERMANENT,
)

# Resolve returns the stored redirect type
long_url, rtype = shortener.resolve("abc1234")
# rtype == RedirectType.PERMANENT (301)
```

## Strategies

### Hash+Collision Resolution (default)

Uses CRC32 to hash the long URL, encodes the result in base-62, and takes the first 7 characters. If a collision occurs (different URL maps to the same code), appends a suffix and rehashes up to a configurable retry limit.

```python
from url_shortener import URLShortener, HashCollisionStrategy

shortener = URLShortener(
    strategy=HashCollisionStrategy(max_retries=10),
)
```

**When to use:**
- No external ID generator available
- Standalone usage without coordination
- URLs are the primary input (no sequential IDs needed)

**Trade-offs:**
- Collision resolution adds retries under high load
- Same URL always produces the same code (deterministic)

### Base-62 Conversion

Converts a unique numeric ID to a 7-character base-62 string. Guarantees uniqueness without collision handling.

```python
from url_shortener import URLShortener, Base62Strategy, AutoIncrementIDGenerator

shortener = URLShortener(
    strategy=Base62Strategy(id_generator=AutoIncrementIDGenerator(start=1000)),
)
```

**When to use:**
- You have a unique ID source (database sequence, distributed ID generator)
- High throughput with zero collision overhead
- Predictable, sequential short codes are acceptable

**Trade-offs:**
- Requires an external or internal ID generator
- Sequential codes may be guessable

### Custom ID Generator

Implement the `IDGenerator` protocol to plug in any ID source:

```python
from url_shortener import Base62Strategy, URLShortener

class SnowflakeIDGenerator:
    def next_id(self) -> int:
        # Your distributed ID logic here
        ...

shortener = URLShortener(strategy=Base62Strategy(id_generator=SnowflakeIDGenerator()))
```

## API Reference

### URLShortener

```python
URLShortener(
    strategy: HashStrategy | None = None,        # Default: HashCollisionStrategy
    storage: StorageBackend | None = None,        # Default: InMemoryStorage
    default_redirect_type: RedirectType = RedirectType.TEMPORARY,
    domain: str = "http://short.url",
)
```

**Methods:**

| Method | Description |
|--------|-------------|
| `shorten(long_url, redirect_type=None) -> str` | Shorten a URL, returns full short URL |
| `resolve(short_code, client_id=None) -> tuple[str, RedirectType]` | Resolve code to original URL |
| `get_click_count(short_code) -> int` | Total clicks for a short code |
| `get_click_records(short_code) -> list[ClickRecord]` | All click records for a short code |

### Strategies

| Class | Description |
|-------|-------------|
| `HashStrategy` | ABC — implement `generate(long_url, storage) -> str` |
| `HashCollisionStrategy(max_retries=10)` | CRC32 hash with collision resolution |
| `Base62Strategy(id_generator=None)` | Unique ID to base-62 conversion |
| `AutoIncrementIDGenerator(start=1)` | Default sequential ID generator |

### Storage

| Class | Description |
|-------|-------------|
| `StorageBackend` | ABC — implement `save`, `get_by_short_code`, `get_by_long_url`, `exists` |
| `InMemoryStorage` | Default backend with dual-dict bidirectional lookup |

### Models

| Class | Description |
|-------|-------------|
| `RedirectType` | IntEnum: `PERMANENT` (301), `TEMPORARY` (302) |
| `URLMapping` | Frozen dataclass: `short_code`, `long_url`, `redirect_type`, `created_at` |
| `ClickRecord` | Frozen dataclass: `short_code`, `timestamp`, `client_id` (optional) |

### Exceptions

| Exception | When Raised |
|-----------|-------------|
| `URLShortenerError` | Base exception for all URL shortener errors |
| `URLValidationError` | Invalid URL (empty, missing scheme, malformed) |
| `ShortCodeNotFoundError` | Short code not found in storage |
| `CollisionLimitExceededError` | Hash collision retries exhausted |

### Utility Functions

| Function | Description |
|----------|-------------|
| `encode_base62(number: int) -> str` | Encode non-negative integer to base-62 string |
| `decode_base62(encoded: str) -> int` | Decode base-62 string back to integer |

## Demo Server

A lightweight HTTP demo using only Python stdlib (`http.server`) that showcases the full shorten/redirect flow.

### Running the Server

```bash
python examples/demo_server.py --port 8000
```

### Endpoints

| Method | Path | Description | Status |
|--------|------|-------------|--------|
| POST | `/shorten` | Shorten a URL | 201 Created |
| GET | `/<short_code>` | Redirect to original URL | 301/302 Redirect |
| GET | `/stats/<code>` | Click statistics | 200 OK |

### Usage Examples

**Shorten a URL:**

```bash
curl -X POST http://localhost:8000/shorten \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/long/path", "redirect_type": 302}'

# Response: {"short_url": "http://localhost:8000/a3Bf92k"}
```

**Redirect:**

```bash
curl -L http://localhost:8000/a3Bf92k
# Follows redirect to https://example.com/long/path
```

**Check stats:**

```bash
curl http://localhost:8000/stats/a3Bf92k
# Response: {"short_code": "a3Bf92k", "click_count": 5}
```

## Testing

```bash
pip install -e ".[dev]"
pytest                              # All tests
pytest tests/test_shortener.py      # Orchestrator tests
pytest tests/test_strategies.py     # Strategy tests
pytest tests/test_storage.py        # Storage tests
pytest tests/test_properties.py     # Property-based tests (Hypothesis)
pytest --hypothesis-show-statistics # Detailed PBT stats
```

### Test Coverage

- **Unit tests**: Core operations, edge cases, error handling, idempotence
- **Property-based tests**: Format invariants, round-trip correctness, collision uniqueness, click tracking accuracy

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
│   ├── test_exceptions.py       # Unit tests for exceptions
│   ├── test_models.py           # Unit tests for data models
│   ├── test_storage.py          # Unit tests for storage
│   ├── test_strategies.py       # Unit tests for strategies
│   ├── test_shortener.py        # Unit tests for orchestrator
│   └── test_properties.py       # Property-based tests (Hypothesis)
└── examples/
    └── demo_server.py           # HTTP demo using stdlib http.server
```

## License

MIT
