# News Feed System

Chapter 12 of *System Design Interview*: an executable Python reference for publishing and reading a reverse chronological feed. See [requirements](docs/requirements.md), [tasks](docs/tasks.md), [the chapter audit](docs/chapter12-review.md) and [design](docs/design.md) for acceptance evidence and deployment limits.

## Features

- Authenticated POST/GET `/v1/me/feed`, hydrated author/content/media/actions/counters, validated keyset pagination.
- Hybrid fanout: bounded push workers for ordinary authors and indexed pull for celebrities. Cold, expired and historical feed windows repair from storage.
- SQLite posts, profiles, follows and mute settings; atomic post/publication outbox, restart replay, immutable post IDs, cache fallback after eviction.
- Five cache groups: feed IDs, hot/normal content, social relationships, actions and counters. Fixed reader workers overlap content/profile I/O and author queries, preserving order and draining owned work on failure/cancellation; profile misses are coalesced with bounded striped locks.
- Queue capacity includes queued, delayed and in-flight jobs. Finite retry, fenced acknowledgements, bounded dead letters and cancellation recovery.
- Standard library runtime; local HTTP demo has request concurrency, size and timeout limits.
- Thread-safe local request QPS, outcomes, in-flight counts and latency snapshots, without per-user labels or retained request samples.

## Installation

```sh
pip install -e ".[dev]"
```

## Library example

```python
import asyncio
from news_feed_system import FeedRequest, NewsFeedAPI, UserProfile

async def main():
    api = NewsFeedAPI(database_path="feed.sqlite3")
    api.auth.register_token("author-token", "alice")
    api.auth.register_token("reader-token", "bob")
    await asyncio.to_thread(api.users.put, UserProfile("alice", "Alice"))
    await api.graph.add_follow("bob", "alice")
    async with api:
        await api.publish(FeedRequest("alice", "author-token", "Hello!"))
        # Optional: wait for background work in a deterministic local example.
        await api.publisher.recover_pending()
        await api.queue.join()
        feed = await api.retrieve("reader-token", page_size=20)
        print([entry.post.content for entry in feed.entries])

asyncio.run(main())
```

`NewsFeedAPI` belongs to one event loop. Publication accepts a post and its outbox transaction before background distribution; a warm feed is eventually consistent. Call `retrieve(..., cursor=feed.next_cursor)` for older pages. The default read repair interval is 30 seconds, page size is capped at 100, and each read phase uses at most 16 fixed workers per request for content, author queries or profile assembly. Configure these with `NewsFeedConfig`. Use `await api.mute(user_id, author_id)` / `unmute` to persist preferences in SQLite mode.

Without `database_path`, authoritative stores are in memory and retain all source posts until process exit. Cache capacity limits apply to cache tiers and individual feeds, not total source history.

## HTTP demo

From this project directory in PowerShell:

```powershell
$env:NEWS_FEED_TOKEN = 'local-author-token'
$env:NEWS_FEED_VIEWER_TOKEN = 'local-reader-token'
python examples/demo_server.py --database feed.sqlite3 --port 8000
```

```sh
curl -X POST http://127.0.0.1:8000/v1/me/feed -H "Authorization: Bearer local-author-token" -H "Content-Type: application/json" -d '{"content":"Hello","media_urls":["https://cdn.example/photo.jpg"]}'
curl http://127.0.0.1:8000/v1/me/feed -H "Authorization: Bearer local-reader-token"
```

The author and optional distinct reader credentials are loaded at startup. The demo creates their profiles and the reader's follow relationship. Media URLs are returned for clients to load; media upload and CDN provisioning are external integrations. Notifications are bounded local records; no third-party push is sent.

## Local request observations

`api.metrics()` returns independent JSON-compatible snapshots for `publish` and `retrieve`:

```python
observations = api.metrics()
print(observations["retrieve"]["qps_last_60s"])
print(observations["retrieve"]["latency_seconds"]["mean"])
```

Each operation reports lifetime request/outcome/in-flight counts and completed latency count/total/mean/max in seconds. Recent QPS uses 60 fixed one-second buckets and divides starts in the current and preceding 59 buckets by 60, including during startup. This is an approximate bucketed rate. Storage is fixed per operation; no token, user, payload or individual latency sample is retained. A monitoring thread can safely collect snapshots.

Publication success measures source acceptance, not fanout completion or push delivery. HTTP rejections before invoking the API are not counted. Exporters, latency distributions, cross-instance aggregation, dashboards and alerts remain integrations.

## Reliability and deployment limits

SQLite is a local source, not a replicated broker. The outbox confirms transfer to the local fanout/notification stages, rather than durable worker completion; process loss after enqueue can lose proactive cache updates. Cold reads and periodic repair reconstruct feed content from source. Delivery can repeat; feed entries and retained local notifications deduplicate. Worker failures retry three times, then enter a 100-entry diagnostic dead-letter buffer. `queue.join()` covers queued/claimed work; it does not include an undispatched publication outbox.

Tokens, rate windows, engagement counters/actions and notification records are process-local. SQLite mute snapshots and social caches require refresh/TTL after writes from another instance. Consistent hashing, shared cache/broker, distributed quotas, replicas, load balancing and multi-region operations require deployment adapters. No claim of ten-million-DAU capacity is made.

## Verification

```sh
python -m pip install -e ".[dev]"
python -m pytest tests -q -p no:cacheprovider
```

The 2026-10-29 review passed **282 tests** with warnings treated as errors, including subprocess exit recovery, SQLite contention, privacy changes, overlapping bounded content/profile reads, cancellation draining, request observations, worker retry and real local HTTP flows. Delayed queue timeouts use `asyncio.TimeoutError` for the declared Python 3.9/3.10 support; a regression simulates their separate exception hierarchy. Python 3.13 was executed here; a full runtime-version matrix was not run.

If using an uninstalled source checkout, set `PYTHONPATH` to this project's absolute `src` directory before testing so subprocesses inherit it. Restricted Windows environments also need a writable `TEMP`/`TMP` and permission for local loopback sockets.
