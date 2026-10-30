# System Design Interview - Python Implementations

Production-quality Python implementations of system design concepts from the book *System Design Interview* by Alex Xu. Each chapter is a self-contained sub-project with full documentation, source code, tests, and usage examples.

> 中文版本请参阅 [README_CN.md](./README_CN.md)

## Completed

| Chapter | Topic | Status | Description |
|---------|-------|--------|-------------|
| 02 | [Scale From Zero](./scale-from-zero/) | ✅ Core reviewed | Source-bound replication, cache fences, live Web pool, leased outbox, local GeoDNS failover and fenced profile promotion; 73 tests; production multi-region HA remains open |
| 03 | [Capacity Estimation](./capacity-estimation/) | ✅ Reference done | Explicit-unit Decimal QPS/storage/bandwidth/cache/node/SLO budgets, scenario CLI and sensitivity analysis; 14 tests |
| 05 | [Rate Limiter](./rate-limiter/) | ✅ Core reviewed | 249 tests; file rules, stable identities, clock fencing, bounded FIFO; real4-process Redis and2HTTP validation |
| 06 | [Consistent Hashing](./consistent-hashing/) | ✅ Core reviewed | 154 tests; streaming migration, atomic weights, bounded entries; four-process and build/plan measurements |
| 07 | [Key-Value Store](./key-value-store/) | Core implemented; [review ongoing](key-value-store/docs/chapter07-review.md) | Dynamo-style, LSM-tree storage, quorum consensus, gossip protocol, gRPC |
| 08 | [Unique ID Generator](./unique-id-generator/) | ✅ Done | Twitter Snowflake algorithm, configurable bit layout, thread-safe, ID parsing |
| 09 | [URL Shortener](./url-shortener/) | ✅ Done | Pluggable hash strategies, base-62 conversion, click tracking, stdlib HTTP demo |
| 10 | [Web Crawler](./web-crawler/) | ✅ Done | Durable crawl recovery, adaptive recrawling, host partition routing, DNS cache and bounded downloads |
| 11 | [Notification System](./notification-system/) | ✅ Done | Durable per-channel queues, event deduplication, lease recovery, scheduled retry and authenticated demo (simulated providers) |
| 12 | [News Feed System](./news-feed-system/) | ✅ Core reviewed | 282 tests; bilingual requirements/tasks, bounded concurrent hydration, request metrics, hybrid reads and recoverable publication outbox |
| 13 | [Chat System](./chat-system/) | ✅ Done | Transactional inboxes, fenced Redis routing/push, bounded WebSockets, authorized message lookup and safe device cursor recovery |
| 14 | [Search Autocomplete](./search-autocomplete-system/) | ✅ Core reviewed | Sampled logs, durable aggregates/periodic worker, immutable top-five trie, nonblocking publication, bounded HTTP failover; 170 tests |
| 15 | [Video Streaming](./video-streaming-system/) | ✅ Core reviewed | Configurable GOP DAG, renewable bounded workers, metadata API, real encrypted HLS/PostgreSQL/S3 protocol tests; 160 tests |
| 16 | [File Sync](./file-sync-system/) | ✅ Core reviewed | Bounded automatic sync, simple/resumable encrypted uploads, conflict/ACL/GC fences and durable notifications; 116 tests |
| 17 | [Proximity Service](./proximity-service/) | ✅ Core reviewed | Daily publication with CAS, isolated/coalesced caches, bounded HTTP and regional failover, independent replicas; 76 tests |
| 18 | [Nearby Friends](./nearby-friends-system/) | ✅ Core reviewed | Bounded WebSocket sessions/work, receipt TTL, batch cache repair, leased broker clients, safe deltas/draining; 76 tests |
| 19 | [Maps System](./maps-system/) | ✅ Core reviewed | Directions/polyline, classified routing tiles, persistent precomputed maps, adaptive navigation/SSE and bounded HTTP; 113 tests |
| 20 | [Distributed Message Queue](./distributed-message-queue/) | ✅ Core reviewed | Bounded batching, partition concurrency, tag filtering, leave/expansion, scheduled long polling and durable replication; 116 tests; production gaps documented |
| 21 | [Metrics Monitoring and Alerting](./metrics-monitoring-system/) | ✅ Core reviewed | Partitioned collection, static discovery ownership, weighted gauge queries, incremental rollups and fenced alerts; 108 tests; production gaps documented |
| 22 | [Ad Click Event Aggregation](./ad-click-aggregation-system/) | ✅ Core reviewed | Bounded query fan-out, exact Top N, verified replay/checkpoints, late corrections and independent audit; 76 tests; production gaps documented |
| 23 | [Hotel Reservation](./hotel-reservation-system/) | ✅ Core reviewed | Rolling date inventory, room-type availability, atomic booking, frozen prices and fenced simulated payment compensation; 99 tests; production gaps documented |
| 24 | [Distributed Email](./distributed-email-service/) | ✅ Core reviewed | Durable mailbox notifications, folder counts, read barriers, fenced delivery/replica replay and bounded transport; 96 tests; production gaps documented |
| 25 | [S3-like Object Storage](./object-storage-system/) | ✅ Core reviewed | Owner/delimiter listing, bounded concurrent I/O, durable publication recovery and fenced GC/pack recovery; 137 tests; production gaps documented |
| 26 | [Gaming Leaderboard](./gaming-leaderboard-system/) | ✅ Core reviewed | Coherent concurrent rank reads, exact shard/epoch barriers, bounded Redis/HTTP and revision profile cache; 72 tests |
| 27 | [Payment System](./payment-system/) | ✅ Core reviewed | Trusted multi-order checkout, strict ledger/provider identity, fenced concurrent executors and reconciliation; 48 tests; simulated funds only |
| 28 | [Digital Wallet](./digital-wallet-system/) | ✅ Core reviewed | Frozen TCC intents/receipts, coherent replay snapshots, parallel exact-prefix quorum, bounded completion waits and HTTP lifetimes; 54 tests; Raft/participant replication remain open |
| 29 | [Stock Exchange](./stock-exchange-system/) | ✅ Core reviewed | Price-level/FIFO index, dense bilateral fills, execution/time APIs, full-state promotion and strict market ingestion; 56 tests; sequencer HA/mmap remain open |

## Planned

Chapters 1 (community introduction) and 4 (interview framework) are reading chapters, not standalone system implementations.

## Chapter Documentation Index

Each sub-project's `docs/` directory holds design, requirements, and task specifications. Chinese versions use the `_cn` suffix (e.g. `design_cn.md`). The table below is the per-chapter entry point for all of them.

| Chapter | English docs | Chinese docs |
|---------|--------------|--------------|
| 02 | [design](./scale-from-zero/docs/design.md) · [requirements](./scale-from-zero/docs/requirements.md) · [tasks](./scale-from-zero/docs/tasks.md) | [design](./scale-from-zero/docs/design_cn.md) · [requirements](./scale-from-zero/docs/requirements_cn.md) · [tasks](./scale-from-zero/docs/tasks_cn.md) |
| 03 | [design](./capacity-estimation/docs/design.md) · [requirements](./capacity-estimation/docs/requirements.md) · [tasks](./capacity-estimation/docs/tasks.md) | [design](./capacity-estimation/docs/design_cn.md) · [requirements](./capacity-estimation/docs/requirements_cn.md) · [tasks](./capacity-estimation/docs/tasks_cn.md) |
| 05 | [design](./rate-limiter/docs/design.md) · [requirements](./rate-limiter/docs/requirements.md) · [tasks](./rate-limiter/docs/tasks.md) | [design](./rate-limiter/docs/design_cn.md) · [requirements](./rate-limiter/docs/requirements_cn.md) · [tasks](./rate-limiter/docs/tasks_cn.md) |
| 06 | [design](./consistent-hashing/docs/design.md) · [requirements](./consistent-hashing/docs/requirements.md) · [tasks](./consistent-hashing/docs/tasks.md) | [design](./consistent-hashing/docs/design_cn.md) · [requirements](./consistent-hashing/docs/requirements_cn.md) · [tasks](./consistent-hashing/docs/tasks_cn.md) |
| 07 | [design](./key-value-store/docs/design.md) · [requirements](./key-value-store/docs/requirements.md) · [tasks](./key-value-store/docs/tasks.md) | [design](./key-value-store/docs/design_cn.md) · [requirements](./key-value-store/docs/requirements_cn.md) · [tasks](./key-value-store/docs/tasks_cn.md) |
| 08 | [design](./unique-id-generator/docs/design.md) · [requirements](./unique-id-generator/docs/requirements.md) · [tasks](./unique-id-generator/docs/tasks.md) | [design](./unique-id-generator/docs/design_cn.md) · [requirements](./unique-id-generator/docs/requirements_cn.md) · [tasks](./unique-id-generator/docs/tasks_cn.md) |
| 09 | [design](./url-shortener/docs/design.md) · [requirements](./url-shortener/docs/requirements.md) · [tasks](./url-shortener/docs/tasks.md) | [design](./url-shortener/docs/design_cn.md) · [requirements](./url-shortener/docs/requirements_cn.md) · [tasks](./url-shortener/docs/tasks_cn.md) |
| 10 | [design](./web-crawler/docs/design.md) · [requirements](./web-crawler/docs/requirements.md) · [tasks](./web-crawler/docs/tasks.md) | [design](./web-crawler/docs/design_cn.md) · [requirements](./web-crawler/docs/requirements_cn.md) · [tasks](./web-crawler/docs/tasks_cn.md) |
| 11 | [design](./notification-system/docs/design.md) · [requirements](./notification-system/docs/requirements.md) · [tasks](./notification-system/docs/tasks.md) | [design](./notification-system/docs/design_cn.md) · [requirements](./notification-system/docs/requirements_cn.md) · [tasks](./notification-system/docs/tasks_cn.md) |
| 12 | [design](./news-feed-system/docs/design.md) | [design](./news-feed-system/docs/design_cn.md) |
| 13 | [design](./chat-system/docs/design.md) · [requirements](./chat-system/docs/requirements.md) · [tasks](./chat-system/docs/tasks.md) | [design](./chat-system/docs/design_cn.md) · [requirements](./chat-system/docs/requirements_cn.md) · [tasks](./chat-system/docs/tasks_cn.md) |
| 14 | [design](./search-autocomplete-system/docs/design.md) · [requirements](./search-autocomplete-system/docs/requirements.md) · [tasks](./search-autocomplete-system/docs/tasks.md) | [design](./search-autocomplete-system/docs/design_cn.md) · [requirements](./search-autocomplete-system/docs/requirements_cn.md) · [tasks](./search-autocomplete-system/docs/tasks_cn.md) |
| 15 | [design](./video-streaming-system/docs/design.md) · [requirements](./video-streaming-system/docs/requirements.md) · [tasks](./video-streaming-system/docs/tasks.md) | [design](./video-streaming-system/docs/design_cn.md) · [requirements](./video-streaming-system/docs/requirements_cn.md) · [tasks](./video-streaming-system/docs/tasks_cn.md) |
| 16 | [design](./file-sync-system/docs/design.md) · [requirements](./file-sync-system/docs/requirements.md) · [tasks](./file-sync-system/docs/tasks.md) | [design](./file-sync-system/docs/design_cn.md) · [requirements](./file-sync-system/docs/requirements_cn.md) · [tasks](./file-sync-system/docs/tasks_cn.md) |
| 17 | [design](./proximity-service/docs/design.md) · [requirements](./proximity-service/docs/requirements.md) · [tasks](./proximity-service/docs/tasks.md) | [design](./proximity-service/docs/design_cn.md) · [requirements](./proximity-service/docs/requirements_cn.md) · [tasks](./proximity-service/docs/tasks_cn.md) |
| 18 | [design](./nearby-friends-system/docs/design.md) · [requirements](./nearby-friends-system/docs/requirements.md) · [tasks](./nearby-friends-system/docs/tasks.md) | [design](./nearby-friends-system/docs/design_cn.md) · [requirements](./nearby-friends-system/docs/requirements_cn.md) · [tasks](./nearby-friends-system/docs/tasks_cn.md) |
| 19 | [design](./maps-system/docs/design.md) · [requirements](./maps-system/docs/requirements.md) · [tasks](./maps-system/docs/tasks.md) | [design](./maps-system/docs/design_cn.md) · [requirements](./maps-system/docs/requirements_cn.md) · [tasks](./maps-system/docs/tasks_cn.md) |
| 20 | [design](./distributed-message-queue/docs/design.md) · [requirements](./distributed-message-queue/docs/requirements.md) · [tasks](./distributed-message-queue/docs/tasks.md) | [design](./distributed-message-queue/docs/design_cn.md) · [requirements](./distributed-message-queue/docs/requirements_cn.md) · [tasks](./distributed-message-queue/docs/tasks_cn.md) |
| 21 | [design](./metrics-monitoring-system/docs/design.md) · [requirements](./metrics-monitoring-system/docs/requirements.md) · [tasks](./metrics-monitoring-system/docs/tasks.md) | [design](./metrics-monitoring-system/docs/design_cn.md) · [requirements](./metrics-monitoring-system/docs/requirements_cn.md) · [tasks](./metrics-monitoring-system/docs/tasks_cn.md) |
| 22 | [design](./ad-click-aggregation-system/docs/design.md) · [requirements](./ad-click-aggregation-system/docs/requirements.md) · [tasks](./ad-click-aggregation-system/docs/tasks.md) | [design](./ad-click-aggregation-system/docs/design_cn.md) · [requirements](./ad-click-aggregation-system/docs/requirements_cn.md) · [tasks](./ad-click-aggregation-system/docs/tasks_cn.md) |
| 23 | [design](./hotel-reservation-system/docs/design.md) · [requirements](./hotel-reservation-system/docs/requirements.md) · [tasks](./hotel-reservation-system/docs/tasks.md) | [design](./hotel-reservation-system/docs/design_cn.md) · [requirements](./hotel-reservation-system/docs/requirements_cn.md) · [tasks](./hotel-reservation-system/docs/tasks_cn.md) |
| 24 | [design](./distributed-email-service/docs/design.md) · [requirements](./distributed-email-service/docs/requirements.md) · [tasks](./distributed-email-service/docs/tasks.md) | [design](./distributed-email-service/docs/design_cn.md) · [requirements](./distributed-email-service/docs/requirements_cn.md) · [tasks](./distributed-email-service/docs/tasks_cn.md) |
| 25 | [design](./object-storage-system/docs/design.md) · [requirements](./object-storage-system/docs/requirements.md) · [tasks](./object-storage-system/docs/tasks.md) | [design](./object-storage-system/docs/design_cn.md) · [requirements](./object-storage-system/docs/requirements_cn.md) · [tasks](./object-storage-system/docs/tasks_cn.md) |
| 26 | [design](./gaming-leaderboard-system/docs/design.md) · [requirements](./gaming-leaderboard-system/docs/requirements.md) · [tasks](./gaming-leaderboard-system/docs/tasks.md) | [design](./gaming-leaderboard-system/docs/design_cn.md) · [requirements](./gaming-leaderboard-system/docs/requirements_cn.md) · [tasks](./gaming-leaderboard-system/docs/tasks_cn.md) |
| 27 | [design](./payment-system/docs/design.md) · [requirements](./payment-system/docs/requirements.md) · [tasks](./payment-system/docs/tasks.md) | [design](./payment-system/docs/design_cn.md) · [requirements](./payment-system/docs/requirements_cn.md) · [tasks](./payment-system/docs/tasks_cn.md) |
| 28 | [design](./digital-wallet-system/docs/design.md) · [requirements](./digital-wallet-system/docs/requirements.md) · [tasks](./digital-wallet-system/docs/tasks.md) | [design](./digital-wallet-system/docs/design_cn.md) · [requirements](./digital-wallet-system/docs/requirements_cn.md) · [tasks](./digital-wallet-system/docs/tasks_cn.md) |
| 29 | [design](./stock-exchange-system/docs/design.md) · [requirements](./stock-exchange-system/docs/requirements.md) · [tasks](./stock-exchange-system/docs/tasks.md) | [design](./stock-exchange-system/docs/design_cn.md) · [requirements](./stock-exchange-system/docs/requirements_cn.md) · [tasks](./stock-exchange-system/docs/tasks_cn.md) |

Chapter 12 currently ships only the design document; requirements and tasks are not yet available.

## Project Structure

```
sdi-implement/
├── README.md                    # This file
├── README_CN.md                 # Chinese version
├── System Design Interview.md   # Book content reference
├── scale-from-zero/             # Ch.02: Staged local HTTP scaling experiments
├── capacity-estimation/         # Ch.03: Explicit-unit capacity and availability estimates
├── rate-limiter/                # Ch.05: Rate Limiter
│   ├── README.md               # Usage documentation
│   ├── README_CN.md            # Chinese Usage documentation
│   ├── pyproject.toml           # Package configuration
│   ├── src/rate_limiter/       # Source code
│   ├── tests/                  # Tests (180 tests)
│   ├── examples/               # Usage examples
│   └── docs/                   # Design, requirements and tasks documents
├── consistent-hashing/          # Ch.06: Consistent Hashing
│   ├── README.md               # Usage documentation
│   ├── README_CN.md            # Chinese documentation
│   ├── pyproject.toml          # Package configuration
│   ├── src/consistent_hashing/ # Source code
│   ├── tests/                  # Tests (77 tests)
│   ├── examples/               # Usage examples
│   └── docs/                   # Design, requirements and tasks documents
├── key-value-store/             # Ch.07: Key-Value Store
│   ├── README.md               # Usage documentation
│   ├── README_CN.md            # Chinese documentation
│   ├── pyproject.toml          # Package configuration
│   ├── proto/                  # Protobuf service definitions
│   ├── src/kv_store/           # Source code
│   ├── tests/                  # Tests (unit, integration, property-based)
│   ├── examples/               # Usage examples
│   └── docs/                   # Design, requirements and tasks documents
├── unique-id-generator/         # Ch.08: Unique ID Generator
│   ├── README.md               # Usage documentation
│   ├── README_CN.md            # Chinese documentation
│   ├── pyproject.toml          # Package configuration
│   ├── src/unique_id/          # Source code
│   ├── tests/                  # Tests (75 tests)
│   ├── examples/               # Usage examples
│   └── docs/                   # Design, requirements and tasks documents
├── url-shortener/               # Ch.09: URL Shortener
│   ├── README.md               # Usage documentation
│   ├── README_CN.md            # Chinese documentation
│   ├── pyproject.toml          # Package configuration
│   ├── src/url_shortener/      # Source code
│   ├── tests/                  # Tests (unit + property-based)
│   ├── examples/               # Demo server
│   └── docs/                   # Design, requirements and tasks documents
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
├── gaming-leaderboard-system/   # Ch.26: Monthly Redis rankings and journal recovery
├── payment-system/              # Ch.27: Simulated payments, double-entry ledger and reconciliation
├── digital-wallet-system/       # Ch.28: Sharded transfers, event replay and TCC recovery
├── stock-exchange-system/       # Ch.29: Limit-order matching, replicas and market data recovery
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

## Engineering quality

See [engineering guidelines](CONTRIBUTING.md) and the [review and validation record](ENGINEERING_REVIEW.md). Run `python tools/audit_code.py` for a dependency-free source inventory and review signals.

The engineering review covers all 27 projects, with per-project changes, concurrency regressions and explicit integration-test limits. Use `python tools/run_project_tests.py --jobs 2 --output .test-tmp/review` to run chapter suites in isolated interpreters after installing their dependencies.
