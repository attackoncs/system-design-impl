# Requirements Document

## Introduction

A Python URL shortener library implementing the design from System Design Interview Chapter 9 "Design A URL Shortener". The library provides URL shortening via pluggable hash strategies (hash+collision resolution and base-62 conversion), URL redirecting with configurable redirect types (301/302), a storage abstraction with in-memory default implementation, and basic analytics/click tracking. The core is a pure library with zero runtime dependencies; a lightweight stdlib HTTP demo showcases the full shorten/redirect flow.

## Glossary

- **URL_Shortener**: The core service that orchestrates URL shortening and redirecting operations
- **Hash_Strategy**: An abstract base class defining the interface for short URL generation algorithms
- **Hash_Collision_Strategy**: The supplied CRC32 strategy encodes and pads a seven-character candidate, retrying with a predefined suffix on collision. MD5/SHA-1 are book alternatives, not supplied implementations.
- **Base62_Strategy**: Fully encode a unique nonnegative integer, padding to at least 7 digits; uniqueness requires coordinated IDs.
- **Storage_Backend**: An abstract base class defining the interface for persisting URL mappings
- **In_Memory_Storage**: The default storage backend implementation using Python dictionaries
- **Short_Code**: A Base62-alphabet identifier whose length depends on the strategy and ID size.
- **Redirect_Type**: An enumeration of HTTP redirect status codes (301 permanent, 302 temporary)
- **Click_Record**: A data structure capturing analytics information for a single redirect event
- **ID_Generator**: An abstract interface for generating unique numeric IDs, used by the Base62_Strategy

## Requirements

### Requirement 1: URL Shortening

**User Story:** As a developer, I want to shorten long URLs into compact short codes, so that I can store and share shorter references to web resources.

#### Acceptance Criteria

1.1 Codes use `[0-9a-zA-Z]`:7 characters for the hash strategy, at least 7 without truncation for Base62 IDs.

1.2 WHEN a long URL is submitted with a custom domain specified, THE URL_Shortener SHALL construct the short URL by combining the custom domain with the generated Short_Code.

1.3 WHEN a long URL that has already been shortened is submitted again, THE URL_Shortener SHALL return the existing Short_Code for that long URL.

1.4 IF an invalid URL is submitted (empty string, missing scheme, or malformed format), THEN THE URL_Shortener SHALL raise a URLValidationError with a descriptive message.

1.5 THE URL_Shortener SHALL accept an optional redirect type parameter (301 or 302) at creation time, defaulting to 302 (temporary redirect).

1.6 WHEN a long URL is submitted without specifying a redirect type, THE URL_Shortener SHALL store the mapping with a 302 redirect type.

### Requirement 2: URL Redirecting

**User Story:** As a developer, I want to resolve short codes back to their original long URLs, so that I can redirect users to the correct destination.

#### Acceptance Criteria

2.1 WHEN a valid Short_Code is provided, THE URL_Shortener SHALL return the original long URL and the configured Redirect_Type for that mapping.

2.2 IF a Short_Code that does not exist in storage is provided, THEN THE URL_Shortener SHALL raise a ShortCodeNotFoundError.

2.3 WHEN a Short_Code is resolved, THE URL_Shortener SHALL record a Click_Record for analytics purposes.

2.4 THE URL_Shortener SHALL support both 301 (permanent) and 302 (temporary) redirect types as stored per URL mapping.

### Requirement 3: Hash+Collision Resolution Strategy

**User Story:** As a developer, I want a hash-based shortening strategy that handles collisions, so that I can generate short codes without requiring a unique ID generator.

#### Acceptance Criteria

3.1 The supplied hash implementation is CRC32; MD5/SHA-1 are book alternatives, not additional supplied strategies.

3.2 THE Hash_Collision_Strategy SHALL encode the hash output using base-62 encoding and take the first 7 characters as the candidate Short_Code.

3.3 IF the candidate Short_Code already exists in storage mapped to a different long URL, THEN THE Hash_Collision_Strategy SHALL append a predefined string to the original URL and rehash.

3.4 THE Hash_Collision_Strategy SHALL repeat collision resolution until a unique Short_Code is found or a maximum retry limit is reached.

3.5 IF the maximum retry limit is exceeded during collision resolution, THEN THE Hash_Collision_Strategy SHALL raise a CollisionLimitExceededError.

### Requirement 4: Base-62 Conversion Strategy

**User Story:** As a developer, I want a base-62 conversion strategy that uses unique IDs, so that I can generate short codes with guaranteed uniqueness and no collision handling.

#### Acceptance Criteria

4.1 WHEN a long URL is provided, THE Base62_Strategy SHALL obtain a unique numeric ID from the configured ID_Generator.

4.2 THE Base62_Strategy SHALL convert the numeric ID to a base-62 string using the character set [0-9, a-z, A-Z].

4.3 Left-pad Base62 codes to at least 7 characters; preserve all digits of larger IDs, never truncate.

4.4 THE Base62_Strategy SHALL support any ID_Generator implementation that conforms to the ID_Generator interface.

4.5 THE Base62_Strategy SHALL provide a default auto-incrementing ID_Generator for standalone usage without external dependencies.

### Requirement 5: Storage Abstraction

**User Story:** As a developer, I want a pluggable storage backend, so that I can swap between in-memory storage for testing and persistent storage for production.

#### Acceptance Criteria

5.1 THE Storage_Backend SHALL define an abstract interface with methods for saving a URL mapping, retrieving a long URL by Short_Code, retrieving a Short_Code by long URL, and checking if a Short_Code exists.

5.2 THE In_Memory_Storage SHALL implement the Storage_Backend interface using Python dictionaries.

5.3 THE In_Memory_Storage SHALL maintain bidirectional lookup: Short_Code to long URL and long URL to Short_Code.

5.4 THE Storage_Backend SHALL store the Redirect_Type alongside each URL mapping.

5.5 THE Storage_Backend SHALL be stateless with respect to the hash strategy (any strategy can use any storage backend).

### Requirement 6: Analytics and Click Tracking

**User Story:** As a developer, I want basic click tracking on redirects, so that I can analyze URL usage patterns.

#### Acceptance Criteria

6.1 WHEN a Short_Code is resolved for redirect, THE URL_Shortener SHALL record a Click_Record containing the Short_Code, timestamp, and an optional client identifier.

6.2 THE URL_Shortener SHALL provide a method to retrieve the total click count for a given Short_Code.

6.3 THE URL_Shortener SHALL provide a method to retrieve the list of Click_Records for a given Short_Code.

6.4 IF click tracking is requested for a Short_Code with no recorded clicks, THEN THE URL_Shortener SHALL return a count of zero and an empty list.

### Requirement 7: Custom Exceptions

**User Story:** As a developer, I want clear, specific exceptions for error conditions, so that I can handle failures gracefully in my application code.

#### Acceptance Criteria

7.1 THE URL_Shortener SHALL define a base URLShortenerError exception from which all custom exceptions inherit.

7.2 THE URL_Shortener SHALL define a URLValidationError for invalid input URLs.

7.3 THE URL_Shortener SHALL define a ShortCodeNotFoundError for lookups of non-existent short codes.

7.4 THE URL_Shortener SHALL define a CollisionLimitExceededError for hash collision resolution failures.

7.5 THE URL_Shortener SHALL include descriptive error messages in all custom exceptions.

### Requirement 8: Strategy Pluggability

**User Story:** As a developer, I want to select and swap hash strategies at runtime, so that I can choose the approach best suited to my use case.

#### Acceptance Criteria

8.1 THE Hash_Strategy SHALL define an abstract interface with a method that accepts a long URL and returns a Short_Code.

8.2 THE URL_Shortener SHALL accept any Hash_Strategy implementation at construction time.

8.3 WHEN the URL_Shortener is constructed without specifying a strategy, THE URL_Shortener SHALL default to the Hash_Collision_Strategy.

8.4 THE Hash_Strategy interface SHALL be independent of the Storage_Backend interface, receiving storage as a dependency for collision checking.

### Requirement 9: HTTP Adapter and Input Boundaries

9.1 The demo reuses `create_app`. POST `/api/v1/data/shorten` accepts `longUrl`, returning 200 with `shortURL`.
9.2 GET `/<code>` or `/api/v1/<code>` returns the stored 301/302 and Location;302 includes `Cache-Control: no-store`.
9.3 Bodies contain 1..16384 bytes. Invalid/deep JSON or invalid URLs return 400; unknown codes return 404.
9.4 Preserve original URLs in storage/library resolution. Encode HTTP Location with IDNA/percent encoding without double-escaping. Reject missing hosts, invalid ports, whitespace, controls, backslashes and unencodable Unicode.
9.5 Only 301/302 redirect configuration is accepted; invalid configuration raises ValueError before persistence. The demo server is single-threaded.

9.6 Preserve IPvFuture brackets and explicit empty query/fragment components. Reject Unicode hosts that fail an NFC IDNA round trip; require explicit ASCII ACE input instead of lossy conversion.

### Requirement 10: Persistence and Concurrency

10.1 SQLite code/url constraints are unique; immutable mappings cannot be overwritten on conflict.
10.2 Existing URLs return through a read-only path. Misses recheck, generate and save under `BEGIN IMMEDIATE`.
10.3 `SQLiteStorage.next_id` persists IDs. The same object used as storage and allocator shares the mapping transaction and rollback. Instances/processes coordinate through one file; this is not cross-database global allocation.
10.4 Duplicates consume no ID. Exhaustion after `2**63-1` raises OverflowError without wrapping. Arbitrary existing external IDs need explicit sequence migration.
10.5 The instance LRU defaults to 1024 entries; 0 disables it. Database reads occur outside its lock.
10.6 Click details are unbounded, nonpersistent instance memory. Cluster analytics, replication, sharding, load balancing and rate-limiter deployment are outside the running implementation.

## Non-Functional Requirements

### NFR-1: Project Structure

- **NFR-1.1**: The project SHALL use `pyproject.toml` with hatchling build system.
- **NFR-1.2**: The project SHALL use src layout (`src/url_shortener/`).
- **NFR-1.3**: The project SHALL require Python >= 3.9.
- **NFR-1.4**: The project SHALL have zero runtime dependencies (stdlib only).
- **NFR-1.5**: The project SHALL include dev dependencies: pytest and hypothesis.
- **NFR-1.6**: The project SHALL include a `docs/` directory for documentation.
- **NFR-1.7**: The project SHALL include an `examples/` directory with the HTTP demo server.
- **NFR-1.8**: The project SHALL provide a clean `__init__.py` with public API exports.

### NFR-2: Code Quality

- **NFR-2.1**: The project SHALL include type hints throughout all public interfaces.
- **NFR-2.2**: The project SHALL include unit tests using pytest.
- **NFR-2.3**: The project SHALL include property-based tests using Hypothesis for hash strategy correctness.
- **NFR-2.4**: The project SHALL use abstract base classes (ABC) for all pluggable strategy interfaces.
- **NFR-2.5**: The project SHALL define a custom exceptions module for all error types.

### NFR-3: Performance

The following are local targets, not portable latency or capacity guarantees. This review validates concurrency correctness; see `benchmarks/REPORT.md`.

- **NFR-3.1**: Single URL shortening SHALL complete in under 10 milliseconds for the in-memory storage backend.
- **NFR-3.2**: URL redirect resolution SHALL complete in under 1 millisecond for the in-memory storage backend.
- **NFR-3.3**: The Hash_Collision_Strategy SHALL resolve collisions within 10 retry attempts by default.

### NFR-4: Short Code Properties

- **NFR-4.1**: Hash codes have 7 characters; Base62 codes have at least 7 and grow to 8 at `62**7`.
- **NFR-4.2**: Short codes SHALL use only characters from the set [0-9, a-z, A-Z] (62 characters total).
- **NFR-4.3**: `62**7` describes the seven-digit space; CRC32 still has only 32 bits and cannot fill that space.
