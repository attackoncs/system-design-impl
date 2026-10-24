# News Feed System

Chapter 12 of *System Design Interview*: an executable Python reference for publishing and reading a reverse chronological feed. See [the chapter audit](docs/chapter12-review.md) and [design](docs/design.md) for coverage and deployment limits.

## Features

- Authenticated POST/GET `/v1/me/feed`, hydrated author/content/media/actions/counters, validated keyset pagination.
- Hybrid fanout: bounded push workers for ordinary authors and indexed pull for celebrities. Cold, expired and historical feed windows repair from storage.
- SQLite posts, profiles, follows and mute settings; atomic post/publication outbox, restart replay, immutable post IDs, cache fallback after eviction.
- Five cache groups: feed IDs, hot/normal content, social relationships, actions and counters. Thread-safe local mutations and bounded author lookup concurrency.
- Queue capacity includes queued, delayed and in-flight jobs. Finite retry, fenced acknowledgements, bounded dead letters and cancellation recovery.
- Standard library runtime; local HTTP demo has request concurrency, size and timeout limits.

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

`NewsFeedAPI` belongs to one event loop. Publication accepts a post and its outbox transaction before background distribution; a warm feed is eventually consistent. Call `retrieve(..., cursor=feed.next_cursor)` for older pages. The default read repair interval is 30 seconds, page size is capped at 100, and author lookups use at most 16 tasks per request. Configure these with `NewsFeedConfig`. Use `await api.mute(user_id, author_id)` / `unmute` to persist preferences in SQLite mode.

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

## Reliability and deployment limits

SQLite is a local source, not a replicated broker. The outbox confirms transfer to the local fanout/notification stages, rather than durable worker completion; process loss after enqueue can lose proactive cache updates. Cold reads and periodic repair reconstruct feed content from source. Delivery can repeat; feed entries and retained local notifications deduplicate. Worker failures retry three times, then enter a 100-entry diagnostic dead-letter buffer. `queue.join()` covers queued/claimed work; it does not include an undispatched publication outbox.

Tokens, rate windows, engagement counters/actions and notification records are process-local. SQLite mute snapshots and social caches require refresh/TTL after writes from another instance. Consistent hashing, shared cache/broker, distributed quotas, replicas, load balancing and multi-region operations require deployment adapters. No claim of ten-million-DAU capacity is made.

## Verification

```sh
pytest tests -q
```

The 2026-10-24 review passed **267 tests**, including subprocess exit recovery, SQLite contention, privacy changes, bounded concurrency, worker cancellation/retry and real local HTTP flows.
