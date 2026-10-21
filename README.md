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
| 14 | [Search Autocomplete](./search-autocomplete-system/) | ✅ Done | Cached top-five trie, batch snapshots, filtering, range shards, HTTP replica recovery |
| 15 | [Video Streaming](./video-streaming-system/) | ✅ Done | Resumable uploads, real FFmpeg/encrypted HLS, PostgreSQL failover, S3 shared storage, measured recovery |
| 16 | [File Sync](./file-sync-system/) | ✅ Reference done | Resumable encrypted blocks, immutable revisions, conflict copies, ACLs, durable long polling and device delta sync |
| 17 | [Proximity Service](./proximity-service/) | ✅ Reference done | Geohash search, owner CRUD, atomic daily snapshots, bounded caches, independent HTTP replicas and regional read failover |
| 18 | [Nearby Friends](./nearby-friends-system/) | ✅ Reference done | Live WebSockets, Redis TTL/PubSub, mutual consent, ordered reports, ring changes and snapshot recovery; 12 tests |
| 19 | [Maps System](./maps-system/) | ✅ Reference done | Lazy tiled routing/ETA, traffic reports, GeoJSON/PNG, versioned HTTP catalogs and regional read recovery; 14 tests |
| 20 | [Distributed Message Queue](./distributed-message-queue/) | ✅ Reference done | Segmented replicated logs, producer fencing, consumer groups, queue-scoped transactions and real broker/transport faults; 15 tests |
| 21 | [Metrics Monitoring and Alerting](./metrics-monitoring-system/) | ✅ Reference done | Durable collection through the message queue, tiered time series storage, grouped alerts and webhook retries; 15 tests |
| 22 | [Ad Click Event Aggregation](./ad-click-aggregation-system/) | ✅ Reference done | Partitioned durable event log, salted hot ads, exact global Top N, late corrections, atomic checkpoints and replay reconciliation; 11 tests |
| 23 | [Hotel Reservation](./hotel-reservation-system/) | ✅ Reference done | Hotel shards, atomic nightly inventory, bounded overbooking, price snapshots, idempotent bookings and simulated payment/refund recovery; 13 tests |
| 24 | [Distributed Email](./distributed-email-service/) | ✅ Reference done | Local HTTP/SMTP delivery, immutable attachments, user shards, asynchronous metadata replicas, versioned read flags and fielded search; 9 tests |
| 25 | [S3-like Object Storage](./object-storage-system/) | ✅ Reference done | Versioned buckets, resumable multipart uploads, packed chunks, sharded listing snapshots, three data replicas and repair/GC; 10 tests |

## Planned

| Chapter | Topic | Status |
|---------|-------|--------|
| 26–29 | Chapter tasks | 📋 Planned |

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
├── search-autocomplete-system/  # Ch.14: Search Autocomplete
│   ├── README.md                # Usage and HTTP deployment
│   ├── README_CN.md             # Chinese documentation
│   ├── src/search_autocomplete/ # Batch snapshots, trie, shard replicas
│   ├── tests/                  # Ranking, persistence and process recovery
│   ├── examples/               # Local and real HTTP demonstrations
│   ├── benchmarks/             # Measured latency and scope
│   └── docs/design.md          # Architecture and limitations
├── video-streaming-system/      # Ch.15: Video Streaming
│   ├── README.md                # Usage and distributed deployment
│   ├── README_CN.md             # Chinese documentation
│   ├── src/video_streaming/     # Upload, DAG workers, PostgreSQL and S3
│   ├── compose.ha.yaml          # Patroni/etcd cluster and private S3 lab
│   ├── tests/                  # 40 tests, including real remote services
│   ├── benchmarks/             # Media measurements and failover evidence
│   └── docs/design.md          # Architecture and deployment boundaries
├── file-sync-system/            # Ch.16: File Sync
│   ├── README.md                # Usage and protocol
│   ├── README_CN.md             # Chinese documentation
│   ├── src/file_sync/           # Encrypted storage, revisions and device client
│   ├── tests/                  # 33 correctness and fault tests
│   ├── examples/               # Independent process/network demo
│   ├── benchmarks/             # Delta bandwidth and sync measurements
│   └── docs/design.md          # Architecture and boundaries
├── proximity-service/          # Ch.17: Nearby business search
├── nearby-friends-system/       # Ch.18: Live authorized nearby friends
├── maps-system/                 # Ch.19: Routing, traffic and map tiles
├── distributed-message-queue/   # Ch.20: Replicated logs and consumer groups
├── metrics-monitoring-system/   # Ch.21: Metrics, queries and alerts
├── ad-click-aggregation-system/  # Ch.22: Event-time click aggregation and reconciliation
├── hotel-reservation-system/    # Ch.23: Atomic hotel inventory and payment compensation
├── distributed-email-service/   # Ch.24: Local SMTP, metadata replicas and mailbox search
├── object-storage-system/       # Ch.25: Versions, multipart uploads and data replica repair
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
