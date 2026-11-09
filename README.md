# System Design Interview - Python Implementations

<!-- LEARNING_GUIDE_START -->
## Code reading guide

[Learn through a concrete example: architecture, state changes and annotated source (Chinese)](docs/learning-guide_cn.md).
<!-- LEARNING_GUIDE_END -->

Executable Python reference implementations of system design concepts from the book *System Design Interview* by Alex Xu. The 27 implemented chapter projects include documentation, source code, tests, examples, and explicit deployment limits.

> 中文版本请参阅 [README_CN.md](./README_CN.md)

## Completed

See the [review coverage inventory](docs/review-coverage_cn.md) for chapter evidence, historical validation and the scope of the current follow-up.

| Chapter | Topic | Status | Description |
|---------|-------|--------|-------------|
| 02 | [Scale From Zero](./scale-from-zero/) | ✅ Core reviewed | HAProxy/Redis source comparison; profile commits fenced against promotion, invalid TTL preserves cached data; 77 tests; production multi-region HA remains open |
| 03 | [Capacity Estimation](./capacity-estimation/) | ✅ Core reviewed | napkin-math source comparison; explicit-unit Decimal budgets, exact rational node ceilings and integer spares; 16 tests; [review](./capacity-estimation/docs/chapter03-review.md) |
| 05 | [Rate Limiter](./rate-limiter/) | ✅ Core reviewed | 265 tests; file rules, stable identities, clock fencing, bounded FIFO; real4-process Redis and2HTTP validation |
| 06 | [Consistent Hashing](./consistent-hashing/) | ✅ Core reviewed | 163 tests; GitHub source comparison, exact locality/statistics properties, O(1) unchanged-state plans; four-process validation |
| 07 | [Key-Value Store](./key-value-store/) | Core implemented; [open-source review: 428 tests](key-value-store/docs/open-source-review_cn.md); provisioning/drain unfinished | Dynamo-style, LSM-tree storage, quorum intersection, gossip protocol, gRPC |
| 08 | [Unique ID Generator](./unique-id-generator/) | Core implemented; [open-source review: 122 tests](unique-id-generator/docs/open-source-review_cn.md); identity/restart safety depends on deployment | Snowflake, precomputed layout fields, thread safety, exact UTC parsing |
| 09 | [URL Shortener](./url-shortener/) | ✅ Done | CRC32/Base62, transactional SQLite IDs, read-only deduplication, LRU and international URL redirects; [source review](./url-shortener/docs/open-source-review_cn.md) |
| 10 | [Web Crawler](./web-crawler/) | ✅ Core reviewed | Fixed Scrapy source comparison; repeated cancellation drains owned SQLite work and releases file locks; 259 tests; [review](./web-crawler/docs/chapter10-review.md) |
| 11 | [Notification System](./notification-system/) | ✅ Core reviewed | 466 tests; bounded batch admission, isolated receipts, transaction-timed leases and reusable workers; [source review](./notification-system/docs/open-source-review_cn.md) |
| 12 | [News Feed System](./news-feed-system/) | ✅ Core reviewed | Fixed Stream-Framework comparison; hybrid feed/outbox and bounded hydration; rejects excessive JSON nesting before publication; 311 tests; [review](./news-feed-system/docs/chapter12-review.md) |
| 13 | [Chat System](./chat-system/) | ✅ Core reviewed | Tinode/Zulip source comparison; durable inboxes, device recovery and fenced Redis routing; owned Redis/WSL runner and Sentinel readiness fixed; 202 suite cases plus 1 separate failover case passed; [review](./chat-system/docs/chapter13-review.md) |
| 14 | [Search Autocomplete](./search-autocomplete-system/) | ✅ Core reviewed | Sampled logs, immutable top-five trie, bounded replica failover and response integrity; 188 tests; [source review](./search-autocomplete-system/docs/open-source-review_cn.md) |
| 15 | [Video Streaming](./video-streaming-system/) | ✅ Core reviewed | Verified part assembly/repair, bounded leased DAG, real HLS/PostgreSQL/S3 tests; 164 distinct cases; [source review](./video-streaming-system/docs/open-source-review_cn.md) |
| 16 | [File Sync](./file-sync-system/) | ✅ Core reviewed | Encrypted delta sync, conflict/ACL/GC fences, response integrity and cursor recovery; 122 tests; [source review](./file-sync-system/docs/open-source-review_cn.md) |
| 17 | [Proximity Service](./proximity-service/) | ✅ Core reviewed | Geohash/distance, daily CAS publication, bounded cache/HTTP and replica failover; 76 tests; [Tile38/Redis source review](./proximity-service/docs/open-source-review_cn.md) |
| 18 | [Nearby Friends](./nearby-friends-system/) | ✅ Core reviewed | Authorized WS/Redis snapshots, TTL, bounded work and ring reattachment; 76 tests, 200/200 fanout; [source review](./nearby-friends-system/docs/open-source-review_cn.md) |
| 19 | [Maps System](./maps-system/) | ✅ Core reviewed | Directions/polyline, classified routing tiles, persistent maps, adaptive navigation/SSE; 113 tests; [Valhalla/OSRM review](./maps-system/docs/open-source-review_cn.md) |
| 20 | [Distributed Message Queue](./distributed-message-queue/) | ✅ Core reviewed | Batching, partition concurrency, tag filtering and scheduled polling with reserved HTTP capacity; 121 tests; [Kafka/RocketMQ review](./distributed-message-queue/docs/open-source-review_cn.md) |
| 21 | [Metrics Monitoring and Alerting](./metrics-monitoring-system/) | ✅ Core reviewed | Partitioned collection, weighted queries, incremental rollups, fenced alerts and strict queue rejection; 112 tests; [Prometheus/Alertmanager review](./metrics-monitoring-system/docs/open-source-review_cn.md) |
| 22 | [Ad Click Event Aggregation](./ad-click-aggregation-system/) | ✅ Core reviewed | Bounded fan-out, exact Top N, late corrections, replay/audit and source-corruption handling; 78 tests; [PyFlink/Flink review](./ad-click-aggregation-system/docs/open-source-review_cn.md) |
| 23 | [Hotel Reservation](./hotel-reservation-system/) | ✅ Core reviewed | Rolling inventory, atomic booking, frozen prices, simulated compensation and unknown-response handling; 100 tests; [open-source review](./hotel-reservation-system/docs/open-source-review_cn.md) |
| 24 | [Distributed Email](./distributed-email-service/) | ✅ Core reviewed | Stalwart/maddy source review; notification admission preserves write capacity; durable deduplication, read barriers and replica replay; 99 tests; production gaps documented |
| 25 | [S3-like Object Storage](./object-storage-system/) | ✅ Core reviewed | MinIO/SeaweedFS source review; robust upstream error replies, durable publication recovery and fenced GC/pack recovery; 138 tests; production gaps documented |
| 26 | [Gaming Leaderboard](./gaming-leaderboard-system/) | ✅ Core reviewed | WB-Games/Redis8.0.5 source review; shared rank snapshots, exact shard/epoch barriers, replay and profile cache; 72 tests including real Redis; production gaps documented |
| 27 | [Payment System](./payment-system/) | ✅ Core reviewed | Hyperswitch/TigerBeetle source review; trusted checkout, strict ledger/provider identity, cumulative retry budget and fenced reconciliation; 48 tests; simulated funds only |
| 28 | [Digital Wallet](./digital-wallet-system/) | ✅ Core reviewed | Formance/Seata source comparison; TCC/replay/quorum and bounded waits; fixes shard/account audit identity and accumulated-balance bounds; snapshot-plus-tail recovery (restart 21.7 s→22 ms at 3,000 events), linear replay and incremental journal sync; 70 tests; Raft/participant replication remain open |
| 29 | [Stock Exchange](./stock-exchange-system/) | ✅ Core reviewed | Exchange Core/CoinTossX source review; typed replay/promotion, corrupt-projection fences and irreversible market orders; 64 tests; sequencer HA/mmap remain open |
| 30 | [The Learning Continues](./docs/chapter30-learning-continues_cn.md) | ✅ Reading guide | Closing chapter has no new service; verified GitHub entry points and a path from architecture to code and failure experiments |

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
| 12 | [design](./news-feed-system/docs/design.md) · [requirements](./news-feed-system/docs/requirements.md) · [tasks](./news-feed-system/docs/tasks.md) | [design](./news-feed-system/docs/design_cn.md) · [requirements](./news-feed-system/docs/requirements_cn.md) · [tasks](./news-feed-system/docs/tasks_cn.md) |
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
2. **Regression verification** — Unit and integration tests, with real-service prerequisites and skipped cases reported explicitly
3. **Well documented** — README, architecture docs, design specs
4. **Engineering discipline** — Type hints, error handling, bounded resources and explicit consistency/durability limits
5. **Faithful to the book** — Implements the core concepts and algorithms described in each chapter

## Install All Dependencies

The repository root provides two aggregate entry points:

- `pyproject.toml` — collects the third-party dependencies of all 27 sub-projects, grouped into `runtime`, `dev`, `demo`, and `all` extras
- `requirements-all.txt` — installs every sub-project in editable mode together with its dev/test extras

```bash
# Third-party dependencies only
pip install -e ".[all]"

# Third-party dependencies + all sub-projects (recommended)
pip install -r requirements-all.txt
```

Afterwards you can run the documented test or example commands in each sub-project. Real-service tests still require their chapter-specific services and configuration. Note that `key-value-store` depends on the local package `consistent-hashing`; `requirements-all.txt` installs it first.

## Quick Start

```bash
# Enter a sub-project
cd rate-limiter

# Install dependencies
pip install -e ".[dev]"

# Run tests in the active interpreter
python -W error -m pytest tests -q

# Run examples
python examples/basic_usage.py
```

## Requirements

- Python >= 3.10 for the aggregate environment, as declared in the root `pyproject.toml`
- Standalone chapter requirements and additional dependencies are declared in each chapter's `pyproject.toml` and README

## License

MIT

## Engineering quality

See the [current review coverage](docs/review-coverage_cn.md), [chapter progress](DEEP_REVIEW_PROGRESS.md), and [engineering guidelines](CONTRIBUTING.md). [ENGINEERING_REVIEW.md](ENGINEERING_REVIEW.md) preserves the dated repository baseline and links to current evidence. Run `python tools/audit_code.py` for a dependency-free source inventory and review signals.

All 27 projects have chapter review records; those records come from different runs and dates, not one newly rerun repository-wide suite. Use `python tools/run_project_tests.py --jobs 2 --output .test-tmp/review` to run chapter suites in isolated interpreters after installing their dependencies. See CONTRIBUTING for selective-run result retention and real-service setup.
