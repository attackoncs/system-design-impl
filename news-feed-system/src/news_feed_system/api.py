"""Composition root for publishing, retrieval and owned background workers."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from news_feed_system.action_cache import ActionCache
from news_feed_system.auth import Authenticator, InMemoryAuthenticator
from news_feed_system.concurrency import run_sync
from news_feed_system.config import NewsFeedConfig
from news_feed_system.counter_cache import CounterCache
from news_feed_system.fanout_service import FanoutService
from news_feed_system.models import FeedRequest, HydratedFeed
from news_feed_system.news_feed_cache import NewsFeedCache
from news_feed_system.news_feed_service import NewsFeedService
from news_feed_system.notification_service import NotificationService
from news_feed_system.policy import InMemoryMuteStore, SQLiteMuteStore
from news_feed_system.post_cache import PostCache
from news_feed_system.post_service import PostService
from news_feed_system.queue import MessageQueue
from news_feed_system.rate_limiter import SlidingWindowRateLimiter
from news_feed_system.social_graph import InMemorySocialGraph
from news_feed_system.social_graph_cache import CachedSocialGraph
from news_feed_system.sqlite_graph import SQLiteSocialGraph
from news_feed_system.storage import (
    InMemoryPostStore,
    InMemoryUserStore,
    SQLitePostStore,
    SQLiteUserStore,
)
from news_feed_system.user_cache import UserCache
from news_feed_system.worker import WorkerPool

logger = logging.getLogger(__name__)


class NewsFeedAPI:
    def __init__(
        self,
        config: Optional[NewsFeedConfig] = None,
        *,
        database_path: Optional[str] = None,
        authenticator: Optional[Authenticator] = None,
    ) -> None:
        self.config = config or NewsFeedConfig()
        cache = self.config.cache
        fanout = self.config.fanout
        self.auth = (
            authenticator if authenticator is not None else InMemoryAuthenticator()
        )
        if database_path is not None:
            source = SQLitePostStore(database_path)
            self.users = SQLiteUserStore(database_path)
            backend = SQLiteSocialGraph(database_path, self.config.max_following)
            mute_store = SQLiteMuteStore(database_path)
        else:
            source = InMemoryPostStore()
            self.users = InMemoryUserStore()
            backend = InMemorySocialGraph()
            mute_store = InMemoryMuteStore()
        self.graph = CachedSocialGraph(
            backend, ttl_seconds=cache.social_graph_cache_ttl_seconds
        )
        self.posts = PostCache(
            cache.post_cache_hot_capacity,
            cache.post_cache_normal_capacity,
            cache.hot_cache_access_threshold,
            store=source,
        )
        self.post_store = source
        self.feeds = NewsFeedCache(cache.news_feed_max_entries)
        self.user_cache = UserCache(
            cache.user_cache_capacity, cache.user_cache_ttl_seconds
        )
        self.actions = ActionCache(cache.action_cache_capacity)
        self.counters = CounterCache()
        self.queue = MessageQueue(fanout.queue_max_size)
        self.fanout = FanoutService(
            self.graph, self.posts, self.queue, fanout, mute_store=mute_store
        )
        self.notifications = NotificationService(
            self.graph,
            post_cache=self.posts,
            visibility=self.fanout.can_view,
        )
        self.publisher = PostService(
            self.auth,
            SlidingWindowRateLimiter(self.config.rate_limit),
            self.posts,
            self.fanout,
            self.notifications,
            dispatch_on_publish=False,
        )
        self.reader = NewsFeedService(
            self.auth,
            self.graph,
            self.fanout,
            self.feeds,
            self.posts,
            self.user_cache,
            self.users,
            self.actions,
            self.counters,
            self.config,
        )
        self.workers = WorkerPool(self.queue, self.feeds, fanout.num_workers)
        self._recovery_task = None
        self._running = False
        self._loop = None
        self._lifecycle_lock = asyncio.Lock()
        self._stopping = False
        self._stop_task = None
        self._active_publications: set[asyncio.Task] = set()
        self._publications_drained = asyncio.Event()
        self._publications_drained.set()

    def _check_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("NewsFeedAPI belongs to one event loop")
        self._loop = loop

    async def start(self) -> None:
        self._check_loop()
        async with self._lifecycle_lock:
            if self._stopping:
                raise RuntimeError("NewsFeedAPI is stopping")
            if self._running:
                return
            await self.workers.start()
            self._running = True
            self._stop_task = None
            self._recovery_task = asyncio.create_task(self._recover())

    async def _recover(self) -> None:
        while True:
            try:
                await self.publisher.recover_pending()
            except Exception:
                logger.exception("Publication outbox recovery failed")
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """Share one shutdown; caller cancellation leaves cleanup owned and running."""
        self._check_loop()
        async with self._lifecycle_lock:
            if not self._running and not self._stopping:
                return
            if self._stop_task is None:
                self._stopping = True
                self._stop_task = asyncio.create_task(self._stop_owned())
            task = self._stop_task
        await asyncio.shield(task)

    async def _stop_owned(self) -> None:
        try:
            if self._recovery_task is not None:
                self._recovery_task.cancel()
                await asyncio.gather(self._recovery_task, return_exceptions=True)
            # Publications admitted before shutdown finish before consumers stop.
            # Accepted posts remain in the source outbox if dispatch is deferred.
            try:
                await asyncio.wait_for(self._publications_drained.wait(), timeout=30)
            except asyncio.TimeoutError:
                publications = list(self._active_publications)
                for task in publications:
                    task.cancel()
                await asyncio.gather(*publications, return_exceptions=True)
            try:
                await asyncio.wait_for(self.queue.join(), timeout=30)
            except asyncio.TimeoutError:
                logger.warning(
                    "Shutdown deadline reached; source posts remain recoverable"
                )
        finally:
            try:
                if self.workers.is_running:
                    await self.workers.stop()
            finally:
                async with self._lifecycle_lock:
                    self._running = False
                    self._stopping = False
                    self._recovery_task = None

    async def publish(self, request: FeedRequest) -> str:
        await self.start()
        async with self._lifecycle_lock:
            if self._stopping or not self._running:
                raise RuntimeError("NewsFeedAPI is stopping")
            task = asyncio.current_task()
            self._active_publications.add(task)
            self._publications_drained.clear()
        try:
            return await self.publisher.publish(request)
        finally:
            self._active_publications.discard(task)
            if not self._active_publications:
                self._publications_drained.set()

    async def retrieve(
        self,
        auth_token: str,
        *,
        page_size: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> HydratedFeed:
        self._check_loop()
        return await self.reader.retrieve(
            auth_token, page_size=page_size, cursor=cursor
        )

    async def mute(self, user_id: str, author_id: str) -> None:
        self._check_loop()
        await run_sync(self.fanout.add_mute, user_id, author_id)

    async def unmute(self, user_id: str, author_id: str) -> None:
        self._check_loop()
        await run_sync(self.fanout.remove_mute, user_id, author_id)

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *exc):
        await self.stop()
