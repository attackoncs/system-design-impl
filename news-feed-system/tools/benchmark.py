"""Measure warm first-page reads, sequentially and with bounded client concurrency.

Uses in-memory source data and five local cache groups; no HTTP or external DB.
Seed/setup and initial repair are outside the timed regions.
"""

import argparse
import asyncio
import datetime as dt
import json
from pathlib import Path
import platform
import statistics
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news_feed_system import NewsFeedAPI  # noqa: E402
from news_feed_system.config import NewsFeedConfig  # noqa: E402
from news_feed_system.models import FeedEntry, Post, UserProfile  # noqa: E402


async def run(args):
    api = NewsFeedAPI(NewsFeedConfig(read_repair_seconds=3600))
    api.auth.register_token("reader-token", "reader")
    api.users.put(UserProfile("author", "Author"))
    await api.graph.add_follow("reader", "author")
    epoch = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
    for index in range(args.posts):
        post = Post(
            f"post{index:06}",
            "author",
            f"Content {index}",
            created_at=epoch + dt.timedelta(seconds=index),
        )
        api.posts.put(post)
        api.feeds.append(
            "reader", FeedEntry(post.post_id, post.author_id, post.created_at)
        )
    expected = [
        f"post{index:06}"
        for index in range(args.posts - 1, args.posts - args.page_size - 1, -1)
    ]
    await api.retrieve("reader-token", page_size=args.page_size)
    original = api.posts.get
    calls, lock = 0, threading.Lock()

    def counted(post_id):
        nonlocal calls
        with lock:
            calls += 1
        return original(post_id)

    api.posts.get = counted

    async def read(count):
        for _ in range(count):
            feed = await api.retrieve("reader-token", page_size=args.page_size)
            assert [entry.post.post_id for entry in feed.entries] == expected
            assert feed.has_more and feed.next_cursor

    seconds = {"sequential": [], "concurrent": []}
    lookups = {name: [] for name in seconds}
    for _ in range(args.repeats):
        calls = 0
        started = time.perf_counter()
        await read(args.requests)
        seconds["sequential"].append(time.perf_counter() - started)
        lookups["sequential"].append(calls)
        calls = 0
        started = time.perf_counter()
        await asyncio.gather(
            *(
                read(len(range(client, args.requests, args.clients)))
                for client in range(min(args.clients, args.requests))
            )
        )
        seconds["concurrent"].append(time.perf_counter() - started)
        lookups["concurrent"].append(calls)
    return {
        "python": platform.python_version(),
        "platform": platform.system(),
        "posts": args.posts,
        "cache_ids": api.feeds.get_count("reader"),
        "page_size": args.page_size,
        "requests_per_sample": args.requests,
        "clients": args.clients,
        "read_concurrency_per_request": api.config.read_concurrency,
        "repeats": args.repeats,
        "seconds": seconds,
        "median_seconds": {
            name: statistics.median(values) for name, values in seconds.items()
        },
        "content_lookups": lookups,
        "scope": "Warm in-memory API reads; excludes seeding, initial repair, HTTP, SQLite and external services.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--posts", type=int, default=500)
    parser.add_argument("--page-size", type=int, default=20)
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--clients", type=int, default=4)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(
        args.posts, args.page_size, args.requests, args.clients, args.repeats
    ) < 1 or not args.page_size < min(args.posts, 101):
        parser.error(
            "require positive sizes, 1 <= page-size <= 100 and page-size < posts"
        )
    result = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "median_seconds": result["median_seconds"],
                "content_lookups": result["content_lookups"],
            }
        )
    )
