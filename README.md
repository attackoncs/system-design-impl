# System Design Interview - Python Implementations

Production-quality Python implementations of system design concepts from the book *System Design Interview* by Alex Xu. Each chapter is a self-contained sub-project with full documentation, source code, tests, and usage examples.

> 中文版本请参阅 [README_CN.md](./README_CN.md)

## Completed

| Chapter | Topic | Status | Description |
|---------|-------|--------|-------------|
| 05 | [Rate Limiter](./rate-limiter/) | ✅ Done | 5 algorithms, Redis/memory backends, FastAPI/Flask integration |
| 06 | [Consistent Hashing](./consistent-hashing/) | ✅ Done | Virtual nodes, O(log N) lookup, distribution statistics |
| 07 | [Key-Value Store](./key-value-store/) | ✅ Done | Dynamo-style, LSM-tree storage, quorum consensus, gossip protocol, gRPC |
| 08 | [Unique ID Generator](./unique-id-generator/) | ✅ Done | Twitter Snowflake algorithm, configurable bit layout, thread-safe, ID parsing |
| 09 | [URL Shortener](./url-shortener/) | ✅ Done | Pluggable hash strategies, base-62 conversion, click tracking, stdlib HTTP demo |
| 10 | [Web Crawler](./web-crawler/) | ✅ Done | Async crawling, priority/politeness frontier, robots.txt compliance, URL/content deduplication |
| 11 | [Notification System](./notification-system/) | ✅ Done | Multi-channel async pipeline, rate limiting, deduplication, exponential-backoff retry, lifecycle tracking |
| 12 | [News Feed System](./news-feed-system/) | ✅ Done | Hybrid push/pull fanout, async workers, five-layer cache architecture, pluggable social graph |
| 13 | [Chat System](./chat-system/) | ✅ Done | Direct/group chat, Redis multi-node routing, Sentinel failover, shared presence, durable push retries, reconnect sync, measured load/fault tests |

## Planned

| Chapter | Topic | Status |
|---------|-------|--------|
| 14 | Search Autocomplete | 📋 Planned |

## Project Structure

```
sdi-implement/
├── README.md                    # This file
├── README_CN.md                 # Chinese version
├── System Design Interview.md   # Book content reference
├── rate-limiter/                # Ch.05: Rate Limiter
│   ├── README.md               # Usage documentation
│   ├── README_CN.md            # Chinese Usage documentation
│   ├── pyproject.toml           # Package configuration
│   ├── src/rate_limiter/       # Source code
│   ├── tests/                  # Tests (180 tests)
│   ├── examples/               # Usage examples
│   └── docs/                   # Design spec documents
├── consistent-hashing/          # Ch.06: Consistent Hashing
│   ├── README.md               # Usage documentation
│   ├── README_CN.md            # Chinese documentation
│   ├── pyproject.toml          # Package configuration
│   ├── src/consistent_hashing/ # Source code
│   ├── tests/                  # Tests (77 tests)
│   ├── examples/               # Usage examples
│   └── docs/                   # Design spec documents
├── key-value-store/             # Ch.07: Key-Value Store
│   ├── README.md               # Usage documentation
│   ├── README_CN.md            # Chinese documentation
│   ├── pyproject.toml          # Package configuration
│   ├── proto/                  # Protobuf service definitions
│   ├── src/kv_store/           # Source code
│   ├── tests/                  # Tests (unit, integration, property-based)
│   ├── examples/               # Usage examples
│   └── docs/                   # Design spec documents
├── unique-id-generator/         # Ch.08: Unique ID Generator
│   ├── README.md               # Usage documentation
│   ├── README_CN.md            # Chinese documentation
│   ├── pyproject.toml          # Package configuration
│   ├── src/unique_id/          # Source code
│   ├── tests/                  # Tests (75 tests)
│   ├── examples/               # Usage examples
│   └── docs/                   # Design spec documents
├── url-shortener/               # Ch.09: URL Shortener
│   ├── README.md               # Usage documentation
│   ├── README_CN.md            # Chinese documentation
│   ├── pyproject.toml          # Package configuration
│   ├── src/url_shortener/      # Source code
│   ├── tests/                  # Tests (unit + property-based)
│   ├── examples/               # Demo server
│   └── docs/                   # Design spec documents
├── web-crawler/                 # Ch.10: Web Crawler
│   ├── README.md                # Usage documentation
│   ├── pyproject.toml           # Package configuration
│   ├── src/web_crawler/         # Source code
│   ├── tests/                   # Unit and property-based tests
│   ├── examples/basic_crawl.py  # Crawl example
│   └── docs/design.md           # Design document
├── notification-system/         # Ch.11: Notification System
│   ├── README.md                # Usage documentation
│   ├── pyproject.toml           # Package configuration
│   ├── src/notification_system/ # Source code
│   ├── tests/                   # Unit and property-based tests
│   ├── examples/demo_server.py  # Demo server
│   └── docs/design.md           # Design document
├── news-feed-system/            # Ch.12: News Feed System
│   ├── README.md                # Usage documentation
│   ├── README_CN.md             # Chinese documentation
│   ├── pyproject.toml           # Package configuration
│   ├── src/news_feed_system/    # Source code
│   ├── tests/                   # Tests
│   └── examples/                # Placeholder for future examples
├── chat-system/                 # Ch.13: Chat System
│   ├── README.md                # Usage documentation
│   ├── README_CN.md             # Chinese documentation
│   ├── pyproject.toml           # Package configuration
│   ├── src/chat_system/         # Source code and optional WebSocket adapter
│   ├── compose.yaml             # Redis, chat nodes, discovery, push worker
│   ├── tests/                   # Core, WebSocket and real multi-process tests
│   ├── examples/                # Local demos and reconnecting client
│   └── docs/design.md           # Design document
└── .gitignore
```

## Design Principles

Each sub-project follows these principles:

1. **Self-contained** — Each has its own `pyproject.toml` and can be installed/tested independently
2. **Fully tested** — pytest with unit and integration tests
3. **Well documented** — README, architecture docs, design specs
4. **Production quality** — Type hints, error handling, performance considerations
5. **Faithful to the book** — Implements the core concepts and algorithms described in each chapter

## Quick Start

```bash
# Enter a sub-project
cd rate-limiter

# Install dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run examples
python examples/basic_usage.py
```

## Requirements

- Python >= 3.9
- Additional dependencies vary by sub-project (see each `pyproject.toml`)

## License

MIT
