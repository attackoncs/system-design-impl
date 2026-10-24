"""Direct fanout consumers with bounded retries and cancellation recovery."""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
from typing import Optional

import news_feed_system.models
import news_feed_system.news_feed_cache
import news_feed_system.queue

logger = logging.getLogger(__name__)
# Kept for callers that explicitly supply an asyncio.Queue to FanoutWorker.
_STOP_SENTINEL = object()


def _validate_retries(max_retries: int, retry_delay_seconds: float) -> None:
    if (
        isinstance(max_retries, bool)
        or not isinstance(max_retries, int)
        or max_retries < 0
    ):
        raise ValueError("max_retries must be a non-negative integer")
    if (
        isinstance(retry_delay_seconds, bool)
        or not isinstance(retry_delay_seconds, (int, float))
        or not math.isfinite(retry_delay_seconds)
        or retry_delay_seconds <= 0
    ):
        raise ValueError("retry_delay_seconds must be finite and positive")


class FanoutWorker:
    """Claim one job at a time, acknowledge success, and recover failures.

    The default queue retains pending and claimed capacity reservations.
    Cache writes must be idempotent by (reader, post_id), since a partial
    batch is retried after failure or cancellation. An explicitly supplied
    asyncio.Queue remains supported for legacy callers and sentinel shutdown.
    """

    def __init__(
        self,
        queue: news_feed_system.queue.MessageQueue,
        cache: news_feed_system.news_feed_cache.NewsFeedCache,
        worker_id: int = 0,
        *,
        max_retries: int = 3,
        retry_delay_seconds: float = 0.05,
    ) -> None:
        _validate_retries(max_retries, retry_delay_seconds)
        self._queue = queue
        self._cache = cache
        self._worker_id = worker_id
        self._max_retries = max_retries
        self._retry_delay = retry_delay_seconds
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def _process(self, task: news_feed_system.models.FanoutTask) -> None:
        entry = news_feed_system.models.FeedEntry(
            post_id=task.post_id, author_id=task.author_id, timestamp=task.timestamp
        )
        for index, user_id in enumerate(task.target_user_ids, 1):
            result = self._cache.append(user_id, entry)
            if inspect.isawaitable(result):
                await result
            elif index % 32 == 0:
                # Large batches should not monopolize the event loop.
                await asyncio.sleep(0)

    def _release_completed_claim(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is None:
            self._queue.retry(task.result())

    async def _next_claim(self) -> Optional[news_feed_system.queue.QueueClaim]:
        claim_task = asyncio.create_task(self._queue.claim())
        stop_task = asyncio.create_task(self._stop.wait())
        try:
            await asyncio.wait(
                (claim_task, stop_task), return_when=asyncio.FIRST_COMPLETED
            )
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
            if claim_task.done():
                return claim_task.result()
            claim_task.cancel()
            await asyncio.gather(claim_task, return_exceptions=True)
            self._release_completed_claim(claim_task)
            return None
        except asyncio.CancelledError:
            claim_task.cancel()
            stop_task.cancel()
            await asyncio.gather(claim_task, stop_task, return_exceptions=True)
            self._release_completed_claim(claim_task)
            raise

    async def _run(self) -> None:
        while not self._stop.is_set():
            claim = await self._next_claim()
            if claim is None:
                break
            try:
                await self._process(claim.task)
            except asyncio.CancelledError:
                self._queue.retry(claim)
                raise
            except Exception as exc:
                logger.exception(
                    "Fanout worker %d failed post %s (attempt %d)",
                    self._worker_id,
                    claim.task.post_id,
                    claim.attempts,
                )
                if claim.attempts <= self._max_retries:
                    delay = min(
                        self._retry_delay * (2 ** min(claim.attempts - 1, 20)), 30
                    )
                    self._queue.retry(claim, delay)
                else:
                    self._queue.fail(claim, str(exc))
            else:
                self._queue.ack(claim)

    async def _run_legacy(self, source: asyncio.Queue) -> None:
        while True:
            item = await source.get()
            try:
                if item is _STOP_SENTINEL:
                    return
                await self._process(item)
            finally:
                source.task_done()

    def start(self, sentinel_queue: Optional[asyncio.Queue] = None) -> asyncio.Task:
        if self._task is not None and not self._task.done():
            raise RuntimeError("FanoutWorker is already running")
        self._stop.clear()
        runner = (
            self._run() if sentinel_queue is None else self._run_legacy(sentinel_queue)
        )
        self._task = asyncio.create_task(
            runner, name=f"fanout-worker-{self._worker_id}"
        )
        return self._task

    def request_stop(self) -> None:
        """Finish the current job and stop claiming further work."""
        self._stop.set()

    @property
    def task(self) -> Optional[asyncio.Task]:
        return self._task


class WorkerPool:
    """Manage direct consumers without an unbounded intermediary queue.

    stop() finishes claimed jobs, leaves pending/retry jobs in MessageQueue,
    and cancels slow workers after the timeout, returning their claims.
    Call queue.join() before stop() when every accepted job must finish.
    """

    def __init__(
        self,
        queue: news_feed_system.queue.MessageQueue,
        cache: news_feed_system.news_feed_cache.NewsFeedCache,
        num_workers: int = 4,
        *,
        max_retries: int = 3,
        retry_delay_seconds: float = 0.05,
    ) -> None:
        if (
            isinstance(num_workers, bool)
            or not isinstance(num_workers, int)
            or num_workers <= 0
        ):
            raise ValueError("num_workers must be a positive integer")
        _validate_retries(max_retries, retry_delay_seconds)
        self._queue = queue
        self._cache = cache
        self._num_workers = num_workers
        self._max_retries = max_retries
        self._retry_delay = retry_delay_seconds
        self._workers: list[FanoutWorker] = []
        self._running = False
        self._stopping = False

    async def start(self) -> None:
        if self._running:
            raise RuntimeError("WorkerPool is already running")
        self._workers = [
            FanoutWorker(
                self._queue,
                self._cache,
                worker_id=i,
                max_retries=self._max_retries,
                retry_delay_seconds=self._retry_delay,
            )
            for i in range(self._num_workers)
        ]
        for worker in self._workers:
            worker.start()
        self._running = True

    async def stop(self, *, timeout: float = 30.0) -> None:
        if not self._running or self._stopping:
            raise RuntimeError("WorkerPool is not running or is already stopping")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be finite and positive")
        self._stopping = True
        tasks = [worker.task for worker in self._workers if worker.task is not None]
        for worker in self._workers:
            worker.request_stop()
        try:
            _, pending = await asyncio.wait(tasks, timeout=timeout)
            for task in pending:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logger.error("Fanout worker exited unexpectedly: %r", result)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            self._workers = []
            self._running = False
            self._stopping = False

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def num_workers(self) -> int:
        return self._num_workers
