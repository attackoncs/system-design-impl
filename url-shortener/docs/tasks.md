# Implementation Plan: URL Shortener

## Overview

Implement a Python URL shortener library following the strategy pattern with pluggable hash strategies (hash+collision resolution and base-62 conversion), a storage abstraction with in-memory default, click tracking analytics, and a stdlib HTTP demo server. The project uses a src layout with zero runtime dependencies.

## Tasks

- [ ] 1. Project scaffolding and foundation
  - [~] 1.1 Create `pyproject.toml` with hatchling build system
    - File: `url-shortener/pyproject.toml`
    - Python >= 3.9, zero runtime dependencies
    - Dev dependencies: `pytest`, `hypothesis`
    - Build target: `packages = ["src/url_shortener"]`
    - Pytest config: `testpaths = ["tests"]`, `pythonpath = ["src"]`
    - _Requirements: NFR-1.1, NFR-1.2, NFR-1.3, NFR-1.4, NFR-1.5_

  - [~] 1.2 Create directory structure and placeholder files
    - Directories: `src/url_shortener/`, `tests/`, `examples/`, `docs/`
    - Create `__init__.py` for `src/url_shortener/` and `tests/`
    - _Requirements: NFR-1.6, NFR-1.7_

  - [~] 1.3 Create `src/url_shortener/exceptions.py` with custom exception hierarchy
    - `URLShortenerError` (base exception)
    - `URLValidationError` (invalid input URLs)
    - `ShortCodeNotFoundError` (non-existent short codes)
    - `CollisionLimitExceededError` (hash collision resolution failures)
    - All exceptions include descriptive error messages
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5_

  - [~] 1.4 Create `src/url_shortener/models.py` with data models
    - `RedirectType` IntEnum: PERMANENT=301, TEMPORARY=302
    - `URLMapping` frozen dataclass: short_code, long_url, redirect_type, created_at
    - `ClickRecord` frozen dataclass: short_code, timestamp, client_id (optional)
    - Use type hints throughout
    - _Requirements: 1.5, 2.4, 6.1_

- [ ] 2. Storage layer
  - [~] 2.1 Implement `StorageBackend` ABC and `InMemoryStorage` in `src/url_shortener/storage.py`
    - Abstract methods: `save(mapping)`, `get_by_short_code(code)`, `get_by_long_url(url)`, `exists(code)`
    - `InMemoryStorage` with dual-dict bidirectional lookup
    - O(1) access in both directions
    - Store `RedirectType` alongside each mapping
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5_

  - [ ]* 2.2 Write unit tests for storage layer
    - File: `tests/test_storage.py`
    - Test save and retrieve by short code
    - Test retrieve by long URL (bidirectional lookup)
    - Test `exists()` for present and absent codes
    - Test storing redirect type alongside mapping
    - Test overwrite behavior
    - _Requirements: 5.1, 5.2, 5.3, 5.4_

- [ ] 3. Hash strategies
  - [~] 3.1 Implement base-62 encoding utilities and `HashStrategy` ABC in `src/url_shortener/strategies.py`
    - `BASE62_CHARS` constant, `SHORT_CODE_LENGTH = 7`
    - `encode_base62(number)` and `decode_base62(encoded)` functions
    - `HashStrategy` ABC with `generate(long_url, storage)` method
    - `IDGenerator` Protocol with `next_id()` method
    - _Requirements: 4.2, 8.1, 8.4_

  - [~] 3.2 Implement `HashCollisionStrategy`
    - CRC32 hash of URL, base-62 encode, take first 7 characters
    - Collision resolution: append suffix and rehash
    - Configurable max retries (default 10)
    - Raise `CollisionLimitExceededError` on exhaustion
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

  - [~] 3.3 Implement `Base62Strategy` and `AutoIncrementIDGenerator`
    - `AutoIncrementIDGenerator` with configurable start value
    - `Base62Strategy` converts unique ID to 7-char base-62 code (right-padded with leading zeros)
    - Accepts any `IDGenerator` conforming to the protocol
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_

  - [ ]* 3.4 Write unit tests for strategies
    - File: `tests/test_strategies.py`
    - Test `encode_base62` / `decode_base62` with known values
    - Test `HashCollisionStrategy` generates 7-char base-62 codes
    - Test collision resolution appends suffix and retries
    - Test `CollisionLimitExceededError` raised after max retries
    - Test `Base62Strategy` generates 7-char codes from sequential IDs
    - Test `AutoIncrementIDGenerator` produces sequential values
    - Test custom `IDGenerator` works with `Base62Strategy`
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 4.1, 4.2, 4.3, 4.4, 4.5_

- [~] 4. Checkpoint - Verify foundation
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 5. URL Shortener orchestrator
  - [~] 5.1 Implement `URLShortener` class in `src/url_shortener/shortener.py`
    - Constructor: optional strategy (default HashCollisionStrategy), storage (default InMemoryStorage), redirect_type (default 302), domain
    - `shorten(long_url, redirect_type)`: validate URL, check idempotence, generate code, store mapping, return full short URL
    - `resolve(short_code, client_id)`: lookup mapping, record click, return (long_url, redirect_type)
    - `get_click_count(short_code)`: return total clicks
    - `get_click_records(short_code)`: return list of ClickRecords
    - URL validation: check non-empty, has scheme, has netloc
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.1, 2.2, 2.3, 2.4, 6.1, 6.2, 6.3, 6.4, 8.2, 8.3_

  - [ ]* 5.2 Write unit tests for URLShortener
    - File: `tests/test_shortener.py`
    - Test shorten returns valid short URL with domain prefix
    - Test shorten same URL twice returns same result (idempotence)
    - Test shorten with custom redirect type stores correctly
    - Test shorten with invalid URL raises URLValidationError
    - Test resolve returns correct long URL and redirect type
    - Test resolve unknown code raises ShortCodeNotFoundError
    - Test resolve records click analytics
    - Test get_click_count returns correct count
    - Test get_click_records returns correct records
    - Test default strategy is HashCollisionStrategy
    - Test custom strategy is accepted
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.1, 2.2, 2.3, 6.1, 6.2, 6.3, 6.4, 8.2, 8.3_

- [ ] 6. Public API and wiring
  - [~] 6.1 Complete `src/url_shortener/__init__.py` with all public exports
    - Export all public classes, functions, and exceptions
    - Define `__all__` list for explicit public API
    - _Requirements: NFR-1.8_

  - [ ]* 6.2 Write unit tests for exceptions module
    - File: `tests/test_exceptions.py`
    - Test all exceptions inherit from `URLShortenerError`
    - Test exceptions include descriptive messages
    - Test exception hierarchy is correct
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5_

  - [ ]* 6.3 Write unit tests for data models
    - File: `tests/test_models.py`
    - Test `RedirectType` enum values (301, 302)
    - Test `URLMapping` frozen dataclass creation and defaults
    - Test `ClickRecord` frozen dataclass creation with optional client_id
    - _Requirements: 1.5, 2.4, 6.1_

- [~] 7. Checkpoint - Ensure core library works
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 8. Property-based tests
  - [ ]* 8.1 Write property test for short code format invariant
    - File: `tests/test_properties.py`
    - **Property 1: Short code format invariant**
    - For any valid URL, both strategies produce codes of exactly 7 chars from [0-9, a-z, A-Z]
    - Use `hypothesis.given` with URL strategies
    - **Validates: Requirements 1.1, 3.2, 4.2, 4.3**

  - [ ]* 8.2 Write property test for shorten/resolve round-trip
    - File: `tests/test_properties.py`
    - **Property 2: Shorten/resolve round-trip**
    - For any valid URL shortened, resolving the code returns the original URL
    - **Validates: Requirements 2.1, 5.3**

  - [ ]* 8.3 Write property test for shortening idempotence
    - File: `tests/test_properties.py`
    - **Property 3: Shortening idempotence**
    - Shortening the same URL multiple times returns the same short URL
    - **Validates: Requirements 1.3**

  - [ ]* 8.4 Write property test for invalid URL rejection
    - File: `tests/test_properties.py`
    - **Property 4: Invalid URL rejection**
    - Empty strings, whitespace-only, missing scheme, missing netloc all raise URLValidationError
    - **Validates: Requirements 1.4**

  - [ ]* 8.5 Write property test for non-existent code error
    - File: `tests/test_properties.py`
    - **Property 5: Non-existent code error**
    - Any 7-char base-62 string not in storage raises ShortCodeNotFoundError on resolve
    - **Validates: Requirements 2.2**

  - [ ]* 8.6 Write property test for redirect type preservation
    - File: `tests/test_properties.py`
    - **Property 6: Redirect type preservation**
    - Shortening with a specified redirect type preserves it on resolve; default is 302
    - **Validates: Requirements 1.5, 1.6, 2.4, 5.4**

  - [ ]* 8.7 Write property test for collision resolution uniqueness
    - File: `tests/test_properties.py`
    - **Property 7: Collision resolution uniqueness**
    - Distinct URLs shortened via HashCollisionStrategy receive distinct short codes
    - **Validates: Requirements 3.3, 3.4**

  - [ ]* 8.8 Write property test for click tracking accuracy
    - File: `tests/test_properties.py`
    - **Property 8: Click tracking accuracy**
    - Resolving a code N times yields click count == N and N click records
    - **Validates: Requirements 2.3, 6.1, 6.2, 6.3**

  - [ ]* 8.9 Write property test for base-62 encoding round-trip
    - File: `tests/test_properties.py`
    - **Property 9: Base-62 encoding round-trip**
    - For any integer in [0, 62^7 - 1], encode then decode returns the original
    - **Validates: Requirements 4.2**

- [ ] 9. HTTP demo server and documentation
  - [~] 9.1 Create `examples/demo_server.py` HTTP demo using stdlib
    - Use `http.server.BaseHTTPRequestHandler` and `HTTPServer`
    - POST `/shorten` endpoint: accepts JSON body, returns 201 with short URL
    - GET `/<short_code>` endpoint: performs 301/302 redirect
    - GET `/stats/<code>` endpoint: returns click stats as JSON
    - Return 404 for unknown short codes
    - Use `URLShortener` as core logic (no duplication)
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5_

  - [~] 9.2 Create `README.md` with project documentation
    - Project overview and architecture description
    - Installation instructions (`pip install -e ".[dev]"`)
    - Quick start guide with code examples
    - API reference for all public classes
    - Strategy comparison (hash+collision vs base-62)
    - Demo server usage instructions
    - _Requirements: NFR-1.6_

  - [~] 9.3 Copy spec documents to `docs/` directory
    - Copy requirements.md, design.md, tasks.md to `url-shortener/docs/`
    - _Requirements: NFR-1.6_

- [~] 10. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- The project uses Python with pytest + hypothesis (no language selection needed)
- Zero runtime dependencies — only stdlib modules used in production code

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["1.3", "1.4"] },
    { "id": 2, "tasks": ["2.1", "3.1"] },
    { "id": 3, "tasks": ["2.2", "3.2", "3.3"] },
    { "id": 4, "tasks": ["3.4", "5.1"] },
    { "id": 5, "tasks": ["5.2", "6.1"] },
    { "id": 6, "tasks": ["6.2", "6.3"] },
    { "id": 7, "tasks": ["8.1", "8.2", "8.3", "8.4", "8.5", "8.6", "8.7", "8.8", "8.9"] },
    { "id": 8, "tasks": ["9.1", "9.2"] },
    { "id": 9, "tasks": ["9.3"] }
  ]
}
```
