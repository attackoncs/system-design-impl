"""Hybrid feed retrieval, privacy filtering, keyset pagination and hydration."""

from __future__ import annotations

import heapq
import collections
from dataclasses import dataclass
from time import monotonic
from collections.abc import Iterable
from typing import Optional

from news_feed_system.action_cache import ActionCache
from news_feed_system.auth import Authenticator
from news_feed_system.concurrency import gather_owned, run_sync
from news_feed_system.config import NewsFeedConfig
from news_feed_system.counter_cache import CounterCache
from news_feed_system.cursors import decode_cursor, encode_cursor
from news_feed_system.exceptions import AuthenticationError, ValidationError
from news_feed_system.fanout_service import FanoutService
from news_feed_system.models import (
    CounterType,
    FeedEntry,
    HydratedFeed,
    HydratedFeedEntry,
    Post,
    PostCounters,
    UserProfile,
)
from news_feed_system.news_feed_cache import NewsFeedCache
from news_feed_system.post_cache import PostCache
from news_feed_system.social_graph_cache import CachedSocialGraph
from news_feed_system.storage import PostKey, UserStore, post_key
from news_feed_system.user_cache import UserCache


@dataclass(frozen=True)
class _Coverage:
    signature: tuple
    expires_at: float
    oldest_key: Optional[PostKey]


class _Candidates:
    """A bounded page merge shared by the fixed asynchronous read workers."""

    def __init__(
        self,
        user_id: str,
        following: set[str],
        fanout: FanoutService,
        limit: int,
        before: Optional[PostKey],
    ) -> None:
        self.user_id = user_id
        self.limit = limit
        self.before = before
        self._following = following
        self._fanout = fanout
        self._posts: dict[str, Post] = {}

    def merge(self, posts: Iterable[Post]) -> None:
        for post in posts:
            if (
                post.author_id in self._following
                and (self.before is None or post_key(post) < self.before)
                and self._fanout.can_view(self.user_id, post)
            ):
                self._posts[post.post_id] = post
        newest = heapq.nlargest(self.limit, self._posts.values(), key=post_key)
        self._posts = {post.post_id: post for post in newest}

    def __len__(self) -> int:
        return len(self._posts)

    def oldest_key(self) -> Optional[PostKey]:
        return min(map(post_key, self._posts.values()), default=None)

    def newest_first(self) -> list[Post]:
        return sorted(self._posts.values(), key=post_key, reverse=True)


class NewsFeedService:
    def __init__(
        self,
        authenticator: Authenticator,
        graph: CachedSocialGraph,
        fanout: FanoutService,
        feeds: NewsFeedCache,
        posts: PostCache,
        users: UserCache,
        user_store: UserStore,
        actions: ActionCache,
        counters: CounterCache,
        config: Optional[NewsFeedConfig] = None,
    ) -> None:
        self._auth = authenticator
        self._graph = graph
        self._fanout = fanout
        self._feeds = feeds
        self._posts = posts
        self._users = users
        self._user_store = user_store
        self._actions = actions
        self._counters = counters
        self._config = config or NewsFeedConfig()
        # Records only complete first-page windows, never arbitrary old pages.
        self._coverage = collections.OrderedDict()

    async def retrieve(
        self,
        auth_token: str,
        *,
        page_size: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> HydratedFeed:
        if not isinstance(auth_token, str) or not auth_token:
            raise AuthenticationError()
        user_id = await self._auth.validate_token(auth_token)
        if user_id is None:
            raise AuthenticationError()
        limit = self._config.default_page_size if page_size is None else page_size
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= self._config.max_page_size
        ):
            raise ValidationError("Invalid page_size", field="page_size")
        before = decode_cursor(cursor) if cursor is not None else None
        following = set(await self._graph.get_following(user_id))
        if len(following) > self._config.max_following:
            raise ValidationError("Following limit exceeded")
        posts = await self._candidates(user_id, following, limit + 1, before)
        hydrated = [await self._hydrate(user_id, post) for post in posts]
        # Relationships/settings may change while the source or profile loads.
        current = set(await self._graph.get_following(user_id))
        hydrated = [
            entry
            for entry in hydrated
            if entry.post.author_id in current
            and self._fanout.can_view(user_id, entry.post)
        ]
        has_more = len(hydrated) > limit
        entries = hydrated[:limit]
        return HydratedFeed(
            entries=entries,
            has_more=has_more,
            next_cursor=encode_cursor(post_key(entries[-1].post)) if has_more else None,
        )

    async def _candidates(
        self, user_id: str, following: set[str], limit: int, before: Optional[PostKey]
    ) -> list[Post]:
        candidates = _Candidates(user_id, following, self._fanout, limit, before)
        await self._cached_posts(user_id, following, candidates)
        muted = self._fanout.get_mute_list(user_id)
        eligible = following - muted
        celebrities = await self._celebrity_authors(eligible)
        signature = (frozenset(following), frozenset(muted), frozenset(celebrities))
        coverage = self._coverage.get(user_id)
        repair = self._needs_repair(coverage, signature, candidates)
        await self._pull_posts(
            eligible if repair else celebrities,
            candidates,
            worker_count=min(self._config.read_concurrency, len(following)),
        )
        result = candidates.newest_first()
        self._remember_result(user_id, result, before, signature, coverage, repair)
        return result

    async def _cached_posts(
        self, user_id: str, following: set[str], candidates: _Candidates
    ) -> None:
        for entry in self._feeds.get_entries(
            user_id, page_size=self._feeds.max_entries
        ):
            if entry.author_id in following and (
                candidates.before is None
                or (entry.timestamp, entry.post_id) < candidates.before
            ):
                post = await run_sync(self._posts.get, entry.post_id)
                if post is not None:
                    candidates.merge([post])

    async def _celebrity_authors(self, authors: set[str]) -> set[str]:
        pending = iter(sorted(authors))
        celebrities = set()

        async def classify() -> None:
            for author in pending:
                if await self._fanout.is_celebrity(author):
                    celebrities.add(author)

        await gather_owned(
            *(
                classify()
                for _ in range(min(self._config.read_concurrency, len(authors)))
            )
        )
        return celebrities

    @staticmethod
    def _needs_repair(
        coverage: Optional[_Coverage], signature: tuple, candidates: _Candidates
    ) -> bool:
        if candidates.before is not None or coverage is None:
            return True
        if coverage.signature != signature or monotonic() >= coverage.expires_at:
            return True
        if len(candidates) < candidates.limit:
            return True
        oldest = candidates.oldest_key()
        # A retained older cache entry can hide a hole in the complete window.
        return (
            coverage.oldest_key is not None
            and oldest is not None
            and oldest < coverage.oldest_key
        )

    async def _pull_posts(
        self, authors: set[str], candidates: _Candidates, *, worker_count: int
    ) -> None:
        pending = iter(sorted(authors))

        async def pull() -> None:
            for author in pending:
                # Repair covers cold/evicted push feeds, delayed workers and
                # celebrity transitions. Warm windows only pull celebrities.
                page = await self._author_posts(
                    candidates.user_id, author, candidates.limit, candidates.before
                )
                candidates.merge(page)

        await gather_owned(*(pull() for _ in range(worker_count)))

    def _remember_result(
        self,
        user_id: str,
        result: list[Post],
        before: Optional[PostKey],
        signature: tuple,
        coverage: Optional[_Coverage],
        repair: bool,
    ) -> None:
        for post in result:
            self._feeds.append(
                user_id, FeedEntry(post.post_id, post.author_id, post.created_at)
            )
        if before is not None:
            return
        floor = post_key(result[-1]) if result else None
        # Frequent warm reads must not postpone the next source repair.
        deadline = (
            monotonic() + self._config.read_repair_seconds
            if repair
            else coverage.expires_at
        )
        self._coverage[user_id] = _Coverage(signature, deadline, floor)
        self._coverage.move_to_end(user_id)
        if len(self._coverage) > self._config.coverage_max_users:
            self._coverage.popitem(last=False)

    async def _author_posts(
        self, user_id: str, author: str, limit: int, before: Optional[PostKey]
    ) -> list[Post]:
        result = []
        while len(result) < limit:
            batch = await run_sync(
                self._posts.get_by_author, author, max(64, limit), before
            )
            if not batch:
                break
            result.extend(
                post for post in batch if self._fanout.can_view(user_id, post)
            )
            if len(batch) < max(64, limit):
                break
            before = post_key(batch[-1])
        return result[:limit]

    async def _hydrate(self, user_id: str, post: Post) -> HydratedFeedEntry:
        author = self._users.get(post.author_id)
        if author is None:
            author = await run_sync(self._user_store.get, post.author_id)
            if author is None:
                author = UserProfile(post.author_id, post.author_id)
            self._users.put(author)
        return HydratedFeedEntry(
            post=post,
            author=author,
            actions=self._actions.get_actions(user_id, post.post_id),
            counters=PostCounters(
                like_count=self._counters.get(post.post_id, CounterType.LIKE),
                reply_count=self._counters.get(post.post_id, CounterType.REPLY),
            ),
        )
