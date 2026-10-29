# Requirements Document

## Introduction

A Python URL shortener library implementing the design from System Design Interview Chapter 9 "Design A URL Shortener". The library provides URL shortening via pluggable hash strategies (hash+collision resolution and base-62 conversion), URL redirecting with configurable redirect types (301/302), a storage abstraction with in-memory default implementation, and basic analytics/click tracking. The core is a pure library with zero runtime dependencies; a lightweight stdlib HTTP demo showcases the full shorten/redirect flow.

## Glossary

- **URL_Shortener**: The core service that orchestrates URL shortening and redirecting operations
- **Hash_Strategy**: An abstract base class defining the interface for short URL generation algorithms
- **Hash_Collision_Strategy**: A hash strategy that applies CRC32/MD5/SHA-1 to the long URL, takes the first 7 characters of the base-62 encoded result, and resolves collisions by appending a predefined string and rehashing
- **Base62_Strategy**: A hash strategy that converts a unique numeric ID into a base-62 encoded string of 7 characters using the character set [0-9, a-z, A-Z]
- **Storage_Backend**: An abstract base class defining the interface for persisting URL mappings
- **In_Memory_Storage**: The default storage backend implementation using Python dictionaries
- **Short_Code**: A 7-character string from the alphabet [0-9, a-z, A-Z] that uniquely identifies a shortened URL
- **Redirect_Type**: An enumeration of HTTP redirect status codes (301 permanent, 302 temporary)
- **Click_Record**: A data structure capturing analytics information for a single redirect event
- **ID_Generator**: An abstract interface for generating unique numeric IDs, used by the Base62_Strategy

## Requirements

### Requirement 1: URL Shortening

**User Story:** As a developer, I want to shorten long URLs into compact short codes, so that I can store and share shorter references to web resources.

#### Acceptance Criteria

1.1 WHEN a valid long URL is submitted, THE URL_Shortener SHALL return a Short_Code of exactly 7 characters from the alphabet [0-9, a-z, A-Z].

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

3.1 WHEN a long URL is provided, THE Hash_Collision_Strategy SHALL compute a hash of the URL using a well-known hash function (CRC32, MD5, or SHA-1).

3.2 THE Hash_Collision_Strategy SHALL encode the hash output using base-62 encoding and take the first 7 characters as the candidate Short_Code.

3.3 IF the candidate Short_Code already exists in storage mapped to a different long URL, THEN THE Hash_Collision_Strategy SHALL append a predefined string to the original URL and rehash.

3.4 THE Hash_Collision_Strategy SHALL repeat collision resolution until a unique Short_Code is found or a maximum retry limit is reached.

3.5 IF the maximum retry limit is exceeded during collision resolution, THEN THE Hash_Collision_Strategy SHALL raise a CollisionLimitExceededError.

### Requirement 4: Base-62 Conversion Strategy

**User Story:** As a developer, I want a base-62 conversion strategy that uses unique IDs, so that I can generate short codes with guaranteed uniqueness and no collision handling.

#### Acceptance Criteria

4.1 WHEN a long URL is provided, THE Base62_Strategy SHALL obtain a unique numeric ID from the configured ID_Generator.

4.2 THE Base62_Strategy SHALL convert the numeric ID to a base-62 string using the character set [0-9, a-z, A-Z].

4.3 THE Base62_Strategy SHALL pad or truncate the base-62 string to produce a Short_Code of exactly 7 characters.

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

### Requirement 9: HTTP Demo Server

**User Story:** As a developer, I want a lightweight HTTP demo using stdlib, so that I can see the full shorten/redirect flow without adding runtime dependencies.

#### Acceptance Criteria

9.1 THE HTTP demo SHALL use only Python standard library modules (http.server).

9.2 THE HTTP demo SHALL expose an endpoint to shorten a long URL and return the short URL.

9.3 THE HTTP demo SHALL expose an endpoint that accepts a Short_Code and performs an HTTP redirect to the original long URL.

9.4 THE HTTP demo SHALL use the URL_Shortener library as its core logic without duplicating shortening or storage functionality.

9.5 THE HTTP demo SHALL return appropriate HTTP status codes: 201 for successful creation, 301 or 302 for redirects (per stored configuration), and 404 for unknown short codes.

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

- **NFR-3.1**: Single URL shortening SHALL complete in under 10 milliseconds for the in-memory storage backend.
- **NFR-3.2**: URL redirect resolution SHALL complete in under 1 millisecond for the in-memory storage backend.
- **NFR-3.3**: The Hash_Collision_Strategy SHALL resolve collisions within 10 retry attempts by default.

### NFR-4: Short Code Properties

- **NFR-4.1**: Short codes SHALL be exactly 7 characters in length.
- **NFR-4.2**: Short codes SHALL use only characters from the set [0-9, a-z, A-Z] (62 characters total).
- **NFR-4.3**: The 7-character base-62 space SHALL support approximately 3.5 trillion unique short codes (62^7).
