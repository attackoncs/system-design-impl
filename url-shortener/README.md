# URL Shortener

<!-- LEARNING_GUIDE_START -->
## Code reading guide

[Learn through a concrete example: architecture, state changes and annotated source (Chinese)](docs/learning-guide_cn.md).
<!-- LEARNING_GUIDE_END -->

> Reviewed against Chapter 9, GitHub source and CodeGraph. Durable IDs, concurrent duplicate reads and international URL redirects are covered in the [review and validation record (Chinese)](docs/open-source-review_cn.md).

A Python URL shortener library implementing the design from *System Design Interview* Chapter 9 "Design A URL Shortener". Features pluggable hash strategies (hash+collision resolution and base-62 conversion), a storage abstraction with in-memory default, click tracking analytics, and a stdlib HTTP demo server. Zero runtime dependencies.

## Features

- **Pluggable hash strategies** — choose between hash+collision resolution or base-62 conversion
- **Storage abstraction** — in-memory default or SQLite persistence with a transactional ID allocator
- **Click tracking** — built-in analytics with per-URL click counts and timestamped records retained within a bounded window
- **Zero runtime dependencies** — pure Python stdlib; dev tools (pytest, hypothesis) are optional
- **Type-annotated core API** — explicit mapping and redirect types
- **Base62 codes** — hash codes have 7 characters; ID codes have at least 7 and grow without truncation. `62**7` is the seven-digit space, not CRC32 capacity

## Architecture

```text
HTTP → create_app → URLShortener.shorten / resolve
                        ├─ HashCollisionStrategy / Base62Strategy
                        ├─ InMemoryStorage / SQLiteStorage
                        │                     ├─ urls: unique code and URL
                        │                     └─ id_sequence: durable IDs
                        ├─ bounded instance LRU
                        └─ instance click records
```

### Component Responsibilities

| Component | Role |
|-----------|------|
| `URLShortener` | Orchestrates shortening, resolving, and click tracking |
| `HashCollisionStrategy` | CRC32 hash + collision resolution via rehashing |
| `Base62Strategy` | Unique ID → Base62; uniqueness depends on the shared allocator |
| `StorageBackend` | Abstract interface for URL mapping persistence |
| `InMemoryStorage` | Default backend with O(1) bidirectional lookup |
| `SQLiteStorage` | Immutable persistent mappings and `next_id()` for coordinated Base62 writers |

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
print(short_url)

# Resolve back to the original
code = short_url.rsplit("/", 1)[1]
long_url, redirect_type = shortener.resolve(code)
print(long_url)        # https://example.com/very/long/path
print(redirect_type)   # RedirectType.TEMPORARY (302)

# Check click analytics
count = shortener.get_click_count(code)
records = shortener.get_click_records(code)
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
long_url, rtype = shortener.resolve(short_url.rsplit("/", 1)[1])
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
- The same stored URL returns its existing code; occupied candidates can affect the final code

### Base-62 Conversion

Converts a unique numeric ID to Base62, padded to at least 7 characters. IDs beyond `62**7-1` produce longer codes. Uniqueness requires one coordinated allocator; the default counter is local to one process.

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

## SQLite persistence and durable IDs

```python
from url_shortener import URLShortener, SQLiteStorage, Base62Strategy

storage = SQLiteStorage("urls.db")
shortener = URLShortener(
    storage=storage,
    strategy=Base62Strategy(id_generator=storage),
    domain="http://localhost:8000",
)
short_url = shortener.shorten("https://example.org/guide")
assert shortener.resolve(short_url.rsplit("/", 1)[1])[0] == "https://example.org/guide"
```

Pass the **same storage object** as mapping store and ID generator within each instance. Separate processes may create their own objects pointing to one SQLite file. ID allocation and mapping creation share a transaction; duplicates consume no IDs. Existing mappings use a read-only fast path; misses recheck under the writer reservation. Do not mix uncoordinated generators; arbitrary external-ID databases require sequence migration first.

The instance LRU defaults to 1024 entries; 0 disables it. Immutable mappings survive restart with SQLite; click totals are exact per-link counters, while raw click details are bounded instance memory: by default the newest 1000 records per link, none older than 30 days (`click_retention_max_records` / `click_retention_max_age`, `None` disables a bound). Pruning happens on write in amortized O(1); details are not persisted. Library resolution preserves the original URL; HTTP Location uses IDNA and percent encoding. Invalid hosts, ports, whitespace and controls are rejected.

Redirects preserve explicit empty `?` / `#` components and IPvFuture brackets. If stdlib IDNA would fold a Unicode hostname (for example `faß.de`), supply its explicit ASCII ACE name (such as `xn--fa-hia.de`); lossy conversion is rejected. See the [review](docs/open-source-review_cn.md).

## API Reference

### URLShortener

```python
URLShortener(
    strategy: HashStrategy | None = None,        # Default: HashCollisionStrategy
    storage: StorageBackend | None = None,        # Default: InMemoryStorage
    default_redirect_type: RedirectType = RedirectType.TEMPORARY,
    domain: str = "http://short.url",
    cache_capacity: int = 1024,
    click_retention_max_records: int | None = 1000,       # per link; 0 keeps no details
    click_retention_max_age: timedelta | None = timedelta(days=30),
)
```

**Methods:**

| Method | Description |
|--------|-------------|
| `shorten(long_url, redirect_type=None) -> str` | Shorten a URL, returns full short URL |
| `resolve(short_code, client_id=None) -> tuple[str, RedirectType]` | Resolve code to original URL |
| `get_click_count(short_code) -> int` | Exact total clicks for a short code (unaffected by retention) |
| `get_click_records(short_code) -> list[ClickRecord]` | Retained click records (retention window, oldest first) |

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
| `StorageBackend` | ABC — implement `save`, `get_by_short_code`, `get_by_long_url`, `exists`; override `get_or_create` atomically for concurrency |
| `InMemoryStorage` | Default backend with dual-dict bidirectional lookup |
| `SQLiteStorage(path)` | Persistent mappings; `next_id()` joins the same-object mapping transaction |

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

The single-threaded stdlib WSGI demo reuses `create_app`; use a production WSGI server for concurrent HTTP. Without `--database` it uses in-memory/hash defaults; with it, it uses durable Base62 IDs.

The old demo-only `/shorten` and `/stats` endpoints have been removed. Use the chapter API below and the library click methods for statistics.

```bash
python -m examples.demo_server --port 8000 --database urls.db
curl -X POST http://localhost:8000/api/v1/data/shorten -H "Content-Type: application/json" -d '{"longUrl":"https://example.com/long/path"}'
```

| Method | Path | Result |
|---|---|---|
| POST | `/api/v1/data/shorten` | 200, `{"shortURL":"http://localhost:8000/0000001"}` on a fresh Base62 database |
| GET | `/<code>` or `/api/v1/<code>` | 301/302 + Location;302 includes `Cache-Control: no-store` |

Request bodies are limited to 16 KiB. Invalid URL/JSON returns 400; unknown codes return 404. Set redirect type through the library constructor or `shorten`. Use the returned short URL rather than guessing its code.

## Testing

```bash
pip install -e ".[dev]"
pytest                              # All tests
pytest tests/test_shortener.py      # Orchestrator tests
pytest tests/test_strategies.py     # Strategy tests
pytest tests/test_storage.py        # Storage tests
pytest tests/test_properties.py     # Property-based tests (Hypothesis)
pytest --hypothesis-show-statistics # Detailed PBT stats
python tools/verify_concurrency.py --output benchmarks/local.json
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
│       ├── sqlite_storage.py    # SQLite + durable IDs
│       ├── urls.py              # URL validation / ASCII URI
│       ├── http.py              # WSGI API
│       ├── strategies.py        # Hash strategies (ABC + implementations)
│       └── shortener.py         # Main URLShortener orchestrator
├── tools/verify_concurrency.py  # Multiprocess + actual HTTP verification
├── benchmarks/                 # Raw verification output
├── tests/
│   ├── test_exceptions.py       # Unit tests for exceptions
│   ├── test_models.py           # Unit tests for data models
│   ├── test_storage.py          # Unit tests for storage
│   ├── test_strategies.py       # Unit tests for strategies
│   ├── test_shortener.py        # Unit tests for orchestrator
│   └── test_properties.py       # Property-based tests (Hypothesis)
└── examples/
    └── demo_server.py           # WSGI demo using stdlib wsgiref
```

## License

MIT
