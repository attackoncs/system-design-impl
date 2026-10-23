# 设计文档：通知系统

## 架构概览

通知系统库在单个进程内遵循事件驱动的管道架构。主编排器（`NotificationService`）校验请求、检查用户偏好、执行限流和去重，然后将通知分发到各通道独立的异步队列。Worker 池从这些队列消费任务，并通过可插拔的 Provider 接口完成投递。失败的投递会以指数退避方式进行重试。每个生命周期事件都通过状态机进行追踪，用于分析和审计。

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    Public API (NotificationService)                       │
│         validate → preferences → rate limit → dedup → enqueue            │
├─────────────────────────────────────────────────────────────────────────┤
│  Message Queues (per-channel)     │  Worker Pool (per-channel)           │
│  asyncio.Queue × 4 channels      │  asyncio.Task workers × N            │
├───────────────────────────────────┼──────────────────────────────────────┤
│  Rate Limiter          │  Deduplicator         │  Retry Handler          │
│  (sliding window)      │  (content-hash TTL)   │  (exp backoff + jitter) │
├────────────────────────┼───────────────────────┼─────────────────────────┤
│  Provider Interface (ABC)         │  Simulated Provider (testing)        │
│  APNs / FCM / SMS / Email         │  configurable latency + failure      │
├─────────────────────────────────────────────────────────────────────────┤
│  Templates       │  Settings       │  Contact Info    │  Event Tracker   │
│  (placeholder)   │  (opt-in/out)   │  (email/phone/   │  (state machine) │
│                  │                 │   device tokens)  │                  │
├─────────────────────────────────────────────────────────────────────────┤
│  Models & Config & Exceptions & Notification Log                         │
└─────────────────────────────────────────────────────────────────────────┘
```

## 项目结构

```
notification-system/
├── pyproject.toml
├── README.md
├── docs/
│   ├── requirements.md
│   ├── design.md
│   └── tasks.md
├── src/
│   └── notification_system/
│       ├── __init__.py          # 公共 API 导出
│       ├── config.py            # NotificationConfig 数据类
│       ├── models.py            # 数据模型（NotificationRequest, NotificationStatus, Channel 等）
│       ├── exceptions.py        # 自定义异常层级
│       ├── service.py           # NotificationService 编排器
│       ├── queue.py             # 带各通道独立 asyncio.Queue 的 MessageQueue
│       ├── worker.py            # WorkerPool 和 Worker（asyncio 任务）
│       ├── provider.py          # Provider ABC + SimulatedProvider
│       ├── templates.py         # 带占位符替换的 NotificationTemplate
│       ├── rate_limiter.py      # SlidingWindowRateLimiter
│       ├── dedup.py             # 带内容哈希 TTL 的 Deduplicator
│       ├── retry.py             # 带指数退避 + 抖动的 RetryHandler
│       ├── settings.py          # NotificationSettings（按用户、按通道 opt-in/out）
│       ├── contacts.py          # ContactInfoStore（email、phone、device tokens）
│       ├── tracker.py           # EventTracker（状态机 + 分析）
│       └── log.py               # NotificationLog（内存实现 + 后端抽象接口）
├── tests/
│   ├── __init__.py
│   ├── test_models.py
│   ├── test_exceptions.py
│   ├── test_service.py
│   ├── test_queue.py
│   ├── test_worker.py
│   ├── test_provider.py
│   ├── test_templates.py
│   ├── test_rate_limiter.py
│   ├── test_dedup.py
│   ├── test_retry.py
│   ├── test_settings.py
│   ├── test_contacts.py
│   ├── test_tracker.py
│   ├── test_log.py
│   └── test_properties.py      # 基于属性的测试（Hypothesis）
└── examples/
    └── demo_server.py           # 使用 stdlib http.server 的 HTTP 演示
```

## 组件设计

### 1. 自定义异常（`exceptions.py`）

```python
class NotificationError(Exception):
    """所有通知系统错误的基类异常。"""
    pass


class ValidationError(NotificationError):
    """当通知请求无效时抛出。"""

    def __init__(self, message: str, field: str = "") -> None:
        self.field = field
        super().__init__(message)


class QueueFullError(NotificationError):
    """当通道队列达到最大容量时抛出。"""

    def __init__(self, channel: str, max_depth: int) -> None:
        self.channel = channel
        self.max_depth = max_depth
        super().__init__(
            f"Queue for channel '{channel}' is full (max_depth={max_depth})"
        )


class RateLimitExceededError(NotificationError):
    """当通知超出收件人的限流阈值时抛出。"""

    def __init__(self, user_id: str, channel: str, limit: int, window: float) -> None:
        self.user_id = user_id
        self.channel = channel
        self.limit = limit
        self.window = window
        super().__init__(
            f"Rate limit exceeded for user '{user_id}' on channel '{channel}': "
            f"{limit} per {window}s"
        )


class DuplicateNotificationError(NotificationError):
    """当通知作为重复被抑制时抛出。"""

    def __init__(self, original_id: str) -> None:
        self.original_id = original_id
        super().__init__(
            f"Duplicate notification suppressed (original_id='{original_id}')"
        )


class TemplateRenderError(NotificationError):
    """当通知模板无法渲染时抛出。"""

    def __init__(self, template_id: str, missing_keys: list[str]) -> None:
        self.template_id = template_id
        self.missing_keys = missing_keys
        super().__init__(
            f"Template '{template_id}' missing required keys: {missing_keys}"
        )


class DeliveryError(NotificationError):
    """当 Provider 投递通知失败时抛出。"""

    def __init__(
        self, notification_id: str, channel: str, reason: str, retryable: bool = True
    ) -> None:
        self.notification_id = notification_id
        self.channel = channel
        self.reason = reason
        self.retryable = retryable
        super().__init__(
            f"Delivery failed for '{notification_id}' on '{channel}': {reason}"
        )
```

### 2. 数据模型（`models.py`）

```python
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Channel(Enum):
    """通知投递通道。"""
    IOS_PUSH = "ios_push"
    ANDROID_PUSH = "android_push"
    SMS = "sms"
    EMAIL = "email"


class NotificationStatus(Enum):
    """通知生命周期状态（状态机）。"""
    CREATED = "created"
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    CLICKED = "clicked"
    UNSUBSCRIBED = "unsubscribed"


# 通知状态机的有效状态转换
VALID_TRANSITIONS: dict[NotificationStatus, set[NotificationStatus]] = {
    NotificationStatus.CREATED: {NotificationStatus.QUEUED, NotificationStatus.FAILED, NotificationStatus.UNSUBSCRIBED},
    NotificationStatus.QUEUED: {NotificationStatus.SENDING, NotificationStatus.FAILED},
    NotificationStatus.SENDING: {NotificationStatus.SENT, NotificationStatus.FAILED},
    NotificationStatus.SENT: {NotificationStatus.DELIVERED, NotificationStatus.FAILED},
    NotificationStatus.DELIVERED: {NotificationStatus.CLICKED},
    NotificationStatus.FAILED: set(),
    NotificationStatus.CLICKED: set(),
    NotificationStatus.UNSUBSCRIBED: set(),
}


@dataclass(frozen=True)
class NotificationRequest:
    """发送通知的请求。"""
    recipient_id: str
    channel: Channel
    title: str = ""
    body: str = ""
    template_id: Optional[str] = None
    template_params: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryResult:
    """Provider 投递尝试的结果。"""
    success: bool
    provider_message_id: Optional[str] = None
    error: Optional[str] = None
    retryable: bool = True


@dataclass
class NotificationTask:
    """队列处理的内部任务表示。"""
    notification_id: str
    request: NotificationRequest
    channel: Channel
    attempt: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    scheduled_at: Optional[datetime] = None


@dataclass(frozen=True)
class NotificationEvent:
    """通知的生命周期事件。"""
    notification_id: str
    channel: Channel
    status: NotificationStatus
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    details: Optional[str] = None


@dataclass
class ContactInfo:
    """用于通知投递的用户联系信息。"""
    user_id: str
    email: Optional[str] = None
    phone: Optional[str] = None
    device_tokens: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LogEntry:
    """通知尝试的持久化日志条目。"""
    notification_id: str
    recipient_id: str
    channel: Channel
    content_summary: str
    timestamp: datetime
    status: NotificationStatus
    attempt: int = 1
    error_details: Optional[str] = None
```

### 3. 配置（`config.py`）

```python
from dataclasses import dataclass, field
from typing import Optional

from notification_system.models import Channel


@dataclass
class RateLimitConfig:
    """单个作用域的限流配置。"""
    max_count: int = 100
    window_seconds: float = 3600.0


@dataclass
class RetryConfig:
    """失败投递的重试配置。"""
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 300.0
    jitter_factor: float = 0.1


@dataclass
class NotificationConfig:
    """通知系统的配置。

    所有数值必须为正数。无效输入会抛出 ValueError。
    """
    # 队列设置
    queue_max_depth: int = 10000

    # Worker 设置
    workers_per_channel: int = 3

    # 限流
    per_channel_rate_limits: dict[Channel, RateLimitConfig] = field(
        default_factory=lambda: {
            Channel.IOS_PUSH: RateLimitConfig(max_count=50, window_seconds=3600.0),
            Channel.ANDROID_PUSH: RateLimitConfig(max_count=50, window_seconds=3600.0),
            Channel.SMS: RateLimitConfig(max_count=10, window_seconds=3600.0),
            Channel.EMAIL: RateLimitConfig(max_count=20, window_seconds=3600.0),
        }
    )
    global_rate_limit: RateLimitConfig = field(
        default_factory=lambda: RateLimitConfig(max_count=100, window_seconds=3600.0)
    )

    # 去重
    dedup_window_seconds: float = 300.0

    # 重试
    retry: RetryConfig = field(default_factory=RetryConfig)

    # 日志保留
    log_retention_seconds: float = 86400.0 * 30  # 30 天

    def __post_init__(self) -> None:
        """校验配置值。"""
        if self.queue_max_depth <= 0:
            raise ValueError(f"queue_max_depth must be positive, got {self.queue_max_depth}")
        if self.workers_per_channel <= 0:
            raise ValueError(f"workers_per_channel must be positive, got {self.workers_per_channel}")
        if self.dedup_window_seconds <= 0:
            raise ValueError(f"dedup_window_seconds must be positive, got {self.dedup_window_seconds}")
        if self.retry.max_retries < 0:
            raise ValueError(f"max_retries must be non-negative, got {self.retry.max_retries}")
        if self.retry.base_delay <= 0:
            raise ValueError(f"base_delay must be positive, got {self.retry.base_delay}")
        if self.retry.max_delay <= 0:
            raise ValueError(f"max_delay must be positive, got {self.retry.max_delay}")
```

### 4. 消息队列（`queue.py`）

```python
import asyncio
from typing import Optional

from notification_system.exceptions import QueueFullError
from notification_system.models import Channel, NotificationTask


class MessageQueue:
    """使用 asyncio.Queue 的按通道消息队列系统。

    为每个通知通道维护独立的 asyncio.Queue 实例。
    支持每个通道可配置的最大深度，并提供深度/待处理计数查询。
    """

    def __init__(self, max_depth: int = 10000) -> None:
        self._max_depth = max_depth
        self._queues: dict[Channel, asyncio.Queue[NotificationTask]] = {
            channel: asyncio.Queue(maxsize=max_depth)
            for channel in Channel
        }

    async def enqueue(self, task: NotificationTask) -> None:
        """将通知任务入队到相应的通道队列。

        Args:
            task: 要入队的通知任务。

        Raises:
            QueueFullError: 当通道队列达到最大容量时抛出。
        """
        queue = self._queues[task.channel]
        if queue.full():
            raise QueueFullError(task.channel.value, self._max_depth)
        await queue.put(task)

    async def dequeue(self, channel: Channel) -> NotificationTask:
        """从通道队列出队下一个任务（队列为空时阻塞）。

        Args:
            channel: 要出队的通道。

        Returns:
            FIFO 顺序下一个 NotificationTask。
        """
        return await self._queues[channel].get()

    def dequeue_nowait(self, channel: Channel) -> Optional[NotificationTask]:
        """非阻塞出队。队列为空时返回 None。"""
        try:
            return self._queues[channel].get_nowait()
        except asyncio.QueueEmpty:
            return None

    def depth(self, channel: Channel) -> int:
        """通道队列当前待处理任务数量。"""
        return self._queues[channel].qsize()

    def is_full(self, channel: Channel) -> bool:
        """检查通道队列是否达到最大容量。"""
        return self._queues[channel].full()

    def is_empty(self, channel: Channel) -> bool:
        """检查通道队列是否没有待处理任务。"""
        return self._queues[channel].empty()
```

### 5. Provider 接口（`provider.py`）

```python
import asyncio
import random
import uuid
from abc import ABC, abstractmethod

from notification_system.models import Channel, DeliveryResult, NotificationTask


class Provider(ABC):
    """通知投递 Provider 的抽象接口。

    每个 Provider 负责特定通道类型的投递。
    实现必须兼容 async。
    """

    @abstractmethod
    async def deliver(self, task: NotificationTask) -> DeliveryResult:
        """通过此 Provider 投递通知。

        Args:
            task: 包含收件人和内容的通知任务。

        Returns:
            带成功状态和 Provider 消息 ID 的 DeliveryResult。
        """
        ...

    @property
    @abstractmethod
    def channel(self) -> Channel:
        """此 Provider 负责的通道。"""
        ...


class SimulatedProvider(Provider):
    """用于测试、行为可配置的模拟 Provider。

    以可配置的延迟和失败率模拟投递。
    适用于无需真实第三方服务即可测试完整管道。
    """

    def __init__(
        self,
        channel: Channel,
        latency_ms: float = 50.0,
        failure_rate: float = 0.0,
    ) -> None:
        self._channel = channel
        self._latency_ms = latency_ms
        self._failure_rate = failure_rate

    @property
    def channel(self) -> Channel:
        return self._channel

    async def deliver(self, task: NotificationTask) -> DeliveryResult:
        """以可配置的延迟和失败模拟投递。"""
        # 模拟网络延迟
        await asyncio.sleep(self._latency_ms / 1000.0)

        # 模拟随机失败
        if random.random() < self._failure_rate:
            return DeliveryResult(
                success=False,
                error="Simulated delivery failure",
                retryable=True,
            )

        return DeliveryResult(
            success=True,
            provider_message_id=f"sim_{uuid.uuid4().hex[:12]}",
        )
```

### 6. Worker 池（`worker.py`）

```python
import asyncio
import logging
from typing import Optional

from notification_system.exceptions import DeliveryError
from notification_system.models import Channel, NotificationStatus, NotificationTask
from notification_system.provider import Provider
from notification_system.queue import MessageQueue
from notification_system.retry import RetryHandler
from notification_system.tracker import EventTracker

logger = logging.getLogger(__name__)


class Worker:
    """从通道队列消费任务的单个异步 Worker。

    拉取任务、调用 Provider、记录事件，并通过委托给 RetryHandler 来处理失败。
    """

    def __init__(
        self,
        worker_id: str,
        channel: Channel,
        queue: MessageQueue,
        provider: Provider,
        tracker: EventTracker,
        retry_handler: RetryHandler,
    ) -> None:
        self._worker_id = worker_id
        self._channel = channel
        self._queue = queue
        self._provider = provider
        self._tracker = tracker
        self._retry_handler = retry_handler
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """启动 Worker 循环。"""
        self._running = True
        self._task = asyncio.current_task()
        while self._running:
            try:
                notification_task = await asyncio.wait_for(
                    self._queue.dequeue(self._channel), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue

            await self._process_task(notification_task)

    async def stop(self) -> None:
        """向 Worker 发出停止信号，等当前任务完成后退出。"""
        self._running = False

    async def _process_task(self, task: NotificationTask) -> None:
        """通过 Provider 处理单个通知任务。"""
        self._tracker.record(task.notification_id, task.channel, NotificationStatus.SENDING)

        result = await self._provider.deliver(task)

        if result.success:
            self._tracker.record(task.notification_id, task.channel, NotificationStatus.SENT)
        else:
            error = DeliveryError(
                notification_id=task.notification_id,
                channel=task.channel.value,
                reason=result.error or "Unknown error",
                retryable=result.retryable,
            )
            await self._retry_handler.handle_failure(task, error)


class WorkerPool:
    """每个通知通道的异步 Worker 池。

    管理 Worker 任务的生命周期：start、stop、优雅关闭。
    为每个通道启动可配置数量的 Worker。
    """

    def __init__(
        self,
        queue: MessageQueue,
        providers: dict[Channel, Provider],
        tracker: EventTracker,
        retry_handler: RetryHandler,
        workers_per_channel: int = 3,
    ) -> None:
        self._queue = queue
        self._providers = providers
        self._tracker = tracker
        self._retry_handler = retry_handler
        self._workers_per_channel = workers_per_channel
        self._workers: dict[Channel, list[Worker]] = {}
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        """为所有通道启动全部 Worker 任务。"""
        for channel in Channel:
            provider = self._providers.get(channel)
            if provider is None:
                continue
            self._workers[channel] = []
            for i in range(self._workers_per_channel):
                worker = Worker(
                    worker_id=f"{channel.value}_worker_{i}",
                    channel=channel,
                    queue=self._queue,
                    provider=provider,
                    tracker=self._tracker,
                    retry_handler=self._retry_handler,
                )
                self._workers[channel].append(worker)
                task = asyncio.create_task(worker.start())
                self._tasks.append(task)

    async def stop(self, graceful: bool = True) -> None:
        """停止全部 Worker。

        Args:
            graceful: 如果为 True，则等待正在进行的投递完成。
        """
        for channel_workers in self._workers.values():
            for worker in channel_workers:
                await worker.stop()

        if graceful:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        else:
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)

        self._tasks.clear()
        self._workers.clear()
```

### 7. 通知模板（`templates.py`）

```python
import re
from typing import Optional

from notification_system.exceptions import TemplateRenderError


class NotificationTemplate:
    """支持命名占位符替换的通知模板。

    支持 title 和 body 中的 {variable_name} 语法占位符。
    模板通过唯一 ID 注册以供复用。
    """

    PLACEHOLDER_PATTERN = re.compile(r"\{(\w+)\}")

    def __init__(self, template_id: str, title: str, body: str) -> None:
        self._template_id = template_id
        self._title = title
        self._body = body

    @property
    def template_id(self) -> str:
        return self._template_id

    @property
    def title_template(self) -> str:
        return self._title

    @property
    def body_template(self) -> str:
        return self._body

    def get_required_keys(self) -> set[str]:
        """从 title 和 body 中提取所有占位符名称。"""
        title_keys = set(self.PLACEHOLDER_PATTERN.findall(self._title))
        body_keys = set(self.PLACEHOLDER_PATTERN.findall(self._body))
        return title_keys | body_keys

    def render(self, params: dict[str, str]) -> tuple[str, str]:
        """用给定参数渲染模板。

        Args:
            params: 占位符名称到值的映射字典。

        Returns:
            （已渲染的标题, 已渲染的正文）元组。

        Raises:
            TemplateRenderError: 当必需占位符缺失时抛出。
        """
        required = self.get_required_keys()
        missing = required - set(params.keys())
        if missing:
            raise TemplateRenderError(self._template_id, sorted(missing))

        rendered_title = self.PLACEHOLDER_PATTERN.sub(
            lambda m: params[m.group(1)], self._title
        )
        rendered_body = self.PLACEHOLDER_PATTERN.sub(
            lambda m: params[m.group(1)], self._body
        )
        return rendered_title, rendered_body


class TemplateRegistry:
    """通知模板注册表，以模板 ID 为键。"""

    def __init__(self) -> None:
        self._templates: dict[str, NotificationTemplate] = {}

    def register(self, template: NotificationTemplate) -> None:
        """按模板 ID 注册模板。"""
        self._templates[template.template_id] = template

    def get(self, template_id: str) -> Optional[NotificationTemplate]:
        """按 ID 检索模板。找不到返回 None。"""
        return self._templates.get(template_id)

    def list_ids(self) -> list[str]:
        """列出所有已注册的模板 ID。"""
        return list(self._templates.keys())
```

### 8. 限流器（`rate_limiter.py`）

```python
import time
from collections import defaultdict

from notification_system.config import RateLimitConfig
from notification_system.exceptions import RateLimitExceededError
from notification_system.models import Channel


class SlidingWindowRateLimiter:
    """用于按用户、按通道频率上限的滑动窗口限流器。

    为每个 (user, channel) 对使用排序后的时间戳列表。每次检查时，
    先移除窗口外已过期的时间戳，再检查计数是否超限。

    同时支持按通道和全局（跨通道）限流。
    """

    def __init__(
        self,
        per_channel_limits: dict[Channel, RateLimitConfig],
        global_limit: RateLimitConfig,
        clock: callable = None,
    ) -> None:
        self._per_channel_limits = per_channel_limits
        self._global_limit = global_limit
        self._clock = clock or time.monotonic
        # (user_id, channel) -> 时间戳列表
        self._channel_windows: dict[tuple[str, Channel], list[float]] = defaultdict(list)
        # user_id -> 时间戳列表（跨通道全局）
        self._global_windows: dict[str, list[float]] = defaultdict(list)

    def check(self, user_id: str, channel: Channel) -> None:
        """检查通知是否符合限流要求。

        Args:
            user_id: 收件人用户 ID。
            channel: 通知通道。

        Raises:
            RateLimitExceededError: 当超出限流阈值时抛出。
        """
        now = self._clock()

        # 检查按通道限制
        channel_config = self._per_channel_limits.get(channel)
        if channel_config:
            key = (user_id, channel)
            self._prune(self._channel_windows[key], now - channel_config.window_seconds)
            if len(self._channel_windows[key]) >= channel_config.max_count:
                raise RateLimitExceededError(
                    user_id, channel.value, channel_config.max_count, channel_config.window_seconds
                )

        # 检查全局限制
        self._prune(self._global_windows[user_id], now - self._global_limit.window_seconds)
        if len(self._global_windows[user_id]) >= self._global_limit.max_count:
            raise RateLimitExceededError(
                user_id, "global", self._global_limit.max_count, self._global_limit.window_seconds
            )

    def record(self, user_id: str, channel: Channel) -> None:
        """记录一次通知发送以用于限流追踪。

        Args:
            user_id: 收件人用户 ID。
            channel: 通知通道。
        """
        now = self._clock()
        self._channel_windows[(user_id, channel)].append(now)
        self._global_windows[user_id].append(now)

    def _prune(self, timestamps: list[float], cutoff: float) -> None:
        """移除早于 cutoff 的时间戳。"""
        while timestamps and timestamps[0] < cutoff:
            timestamps.pop(0)
```

### 9. 去重器（`dedup.py`）

```python
import hashlib
import time
from typing import Optional

from notification_system.exceptions import DuplicateNotificationError
from notification_system.models import Channel


class Deduplicator:
    """基于内容哈希、带可配置 TTL 的去重器。

    从 (recipient_id, channel, content_hash) 计算去重键。
    维护带过期时间戳的去重键内存字典。
    在访问时自动过期旧条目。
    """

    def __init__(self, window_seconds: float = 300.0, clock: callable = None) -> None:
        self._window_seconds = window_seconds
        self._clock = clock or time.monotonic
        # dedup_key -> (notification_id, 过期时间戳)
        self._entries: dict[str, tuple[str, float]] = {}

    def compute_key(self, recipient_id: str, channel: Channel, content: str) -> str:
        """从收件人、通道和内容计算去重键。

        Args:
            recipient_id: 收件人用户 ID。
            channel: 通知通道。
            content: 通知内容（标题 + 正文）。

        Returns:
            组合键各组件的 SHA-256 十六进制摘要。
        """
        raw = f"{recipient_id}:{channel.value}:{content}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def check(self, dedup_key: str) -> Optional[str]:
        """检查去重键是否存在且未过期。

        Args:
            dedup_key: 要检查的去重键。

        Returns:
            如果是重复则返回原始 notification_id，否则返回 None。
        """
        self._expire_old_entries()
        entry = self._entries.get(dedup_key)
        if entry is not None:
            notification_id, expiry = entry
            if self._clock() < expiry:
                return notification_id
            else:
                del self._entries[dedup_key]
        return None

    def record(self, dedup_key: str, notification_id: str) -> None:
        """记录去重键及关联的通知 ID。

        Args:
            dedup_key: 去重键。
            notification_id: 要关联的通知 ID。
        """
        expiry = self._clock() + self._window_seconds
        self._entries[dedup_key] = (notification_id, expiry)

    def check_and_record(
        self, recipient_id: str, channel: Channel, content: str, notification_id: str
    ) -> None:
        """检查是否重复，若为新则记录。

        Args:
            recipient_id: 收件人用户 ID。
            channel: 通知通道。
            content: 通知内容。
            notification_id: 新通知的 ID。

        Raises:
            DuplicateNotificationError: 当检测到重复时抛出。
        """
        key = self.compute_key(recipient_id, channel, content)
        original_id = self.check(key)
        if original_id is not None:
            raise DuplicateNotificationError(original_id)
        self.record(key, notification_id)

    def _expire_old_entries(self) -> None:
        """移除所有过期条目。"""
        now = self._clock()
        expired_keys = [k for k, (_, expiry) in self._entries.items() if now >= expiry]
        for k in expired_keys:
            del self._entries[k]

    @property
    def entry_count(self) -> int:
        """活跃（未过期）去重条目数量。"""
        self._expire_old_entries()
        return len(self._entries)
```

### 10. 重试处理器（`retry.py`）

```python
import asyncio
import logging
import random
from datetime import datetime, timezone

from notification_system.config import RetryConfig
from notification_system.exceptions import DeliveryError
from notification_system.models import NotificationStatus, NotificationTask
from notification_system.queue import MessageQueue
from notification_system.tracker import EventTracker
from notification_system.log import NotificationLog

logger = logging.getLogger(__name__)


class RetryHandler:
    """以指数退避 + 抖动处理失败通知投递。

    延迟计算公式：min(base_delay * 2^attempt + jitter, max_delay)
    其中 jitter 是 [0, jitter_factor * base_delay * 2^attempt] 区间内的随机值。

    不可重试错误（无效令牌、永久拒绝）跳过重试，
    立即将通知标记为 FAILED。
    """

    def __init__(
        self,
        config: RetryConfig,
        queue: MessageQueue,
        tracker: EventTracker,
        notification_log: NotificationLog,
    ) -> None:
        self._config = config
        self._queue = queue
        self._tracker = tracker
        self._log = notification_log

    def compute_delay(self, attempt: int) -> float:
        """计算给定尝试次数的退避延迟。

        公式：min(base_delay * 2^attempt + jitter, max_delay)

        Args:
            attempt: 重试尝试次数（0 基）。

        Returns:
            延迟秒数。
        """
        exponential = self._config.base_delay * (2 ** attempt)
        jitter = random.uniform(0, self._config.jitter_factor * exponential)
        return min(exponential + jitter, self._config.max_delay)

    async def handle_failure(self, task: NotificationTask, error: DeliveryError) -> None:
        """处理一次失败的投递尝试。

        如果错误可重试且未超过最大重试次数，则以指数退避延迟重新入队。
        否则标记为永久失败。

        Args:
            task: 失败的通知任务。
            error: 带可重试标志的投递错误。
        """
        # 记录重试尝试
        self._log.record_attempt(
            notification_id=task.notification_id,
            recipient_id=task.request.recipient_id,
            channel=task.channel,
            content_summary=task.request.body[:100],
            status=NotificationStatus.FAILED,
            attempt=task.attempt + 1,
            error_details=error.reason,
        )

        # 不可重试错误：立即永久失败
        if not error.retryable:
            self._tracker.record(
                task.notification_id, task.channel, NotificationStatus.FAILED
            )
            return

        # 检查最大重试次数
        if task.attempt >= self._config.max_retries:
            self._tracker.record(
                task.notification_id, task.channel, NotificationStatus.FAILED
            )
            return

        # 计算延迟并安排重试
        delay = self.compute_delay(task.attempt)
        await asyncio.sleep(delay)

        # 以递增的尝试计数器重新入队
        retry_task = NotificationTask(
            notification_id=task.notification_id,
            request=task.request,
            channel=task.channel,
            attempt=task.attempt + 1,
            created_at=task.created_at,
            scheduled_at=datetime.now(timezone.utc),
        )
        await self._queue.enqueue(retry_task)
```

### 11. 通知设置（`settings.py`）

```python
from collections import defaultdict

from notification_system.models import Channel


class NotificationSettings:
    """按用户、按通道的通知偏好存储。

    新用户首次出现时，默认所有通道均为已选择。
    支持 opt-in、opt-out、批量更新和偏好查询。
    """

    def __init__(self) -> None:
        # user_id -> {channel -> opted_in}
        self._preferences: dict[str, dict[Channel, bool]] = {}

    def _ensure_user(self, user_id: str) -> None:
        """为新用户初始化默认偏好（全部 opted-in）。"""
        if user_id not in self._preferences:
            self._preferences[user_id] = {channel: True for channel in Channel}

    def is_opted_in(self, user_id: str, channel: Channel) -> bool:
        """检查用户是否选择了某个通道。

        Args:
            user_id: 用户标识符。
            channel: 通知通道。

        Returns:
            True 表示已选择（默认），False 表示已选择不接收。
        """
        self._ensure_user(user_id)
        return self._preferences[user_id][channel]

    def opt_out(self, user_id: str, channel: Channel) -> None:
        """让用户选择不接收某个通知通道。

        Args:
            user_id: 用户标识符。
            channel: 要选择不接收的通道。
        """
        self._ensure_user(user_id)
        self._preferences[user_id][channel] = False

    def opt_in(self, user_id: str, channel: Channel) -> None:
        """让用户重新选择接收某个通知通道。

        Args:
            user_id: 用户标识符。
            channel: 要选择接收的通道。
        """
        self._ensure_user(user_id)
        self._preferences[user_id][channel] = True

    def get_all_preferences(self, user_id: str) -> dict[Channel, bool]:
        """获取用户的所有通道偏好。

        Args:
            user_id: 用户标识符。

        Returns:
            每个 Channel 到其 opt-in 状态的字典映射。
        """
        self._ensure_user(user_id)
        return dict(self._preferences[user_id])

    def bulk_update(self, user_id: str, preferences: dict[Channel, bool]) -> None:
        """在单次操作中更新多个通道偏好。

        Args:
            user_id: 用户标识符。
            preferences: 通道到新的 opt-in 状态的字典映射。
        """
        self._ensure_user(user_id)
        for channel, opted_in in preferences.items():
            self._preferences[user_id][channel] = opted_in
```

### 12. 联系信息存储（`contacts.py`）

```python
from typing import Optional

from notification_system.models import Channel, ContactInfo


class ContactInfoStore:
    """用户联系信息的内存存储。

    管理邮箱地址、电话号码和设备令牌。
    支持每个用户的推送通道存储多个设备令牌。
    """

    def __init__(self) -> None:
        self._contacts: dict[str, ContactInfo] = {}

    def get(self, user_id: str) -> Optional[ContactInfo]:
        """检索用户的联系信息。"""
        return self._contacts.get(user_id)

    def set(self, contact: ContactInfo) -> None:
        """存储或更新用户的联系信息。"""
        self._contacts[contact.user_id] = contact

    def remove(self, user_id: str) -> None:
        """移除用户的全部联系信息。"""
        self._contacts.pop(user_id, None)

    def add_device_token(self, user_id: str, token: str) -> None:
        """为用户添加设备令牌。

        Args:
            user_id: 用户标识符。
            token: 要添加的设备令牌。
        """
        contact = self._contacts.get(user_id)
        if contact is None:
            contact = ContactInfo(user_id=user_id)
            self._contacts[user_id] = contact
        if token not in contact.device_tokens:
            contact.device_tokens.append(token)

    def remove_device_token(self, user_id: str, token: str) -> None:
        """为用户移除设备令牌。

        Args:
            user_id: 用户标识符。
            token: 要移除的设备令牌。
        """
        contact = self._contacts.get(user_id)
        if contact and token in contact.device_tokens:
            contact.device_tokens.remove(token)

    def resolve_endpoint(self, user_id: str, channel: Channel) -> Optional[str]:
        """为用户和通道解析投递端点。

        Args:
            user_id: 用户标识符。
            channel: 通知通道。

        Returns:
            端点字符串（email、phone 或首个 device token），
            如果该通道没有联系信息则返回 None。
        """
        contact = self._contacts.get(user_id)
        if contact is None:
            return None

        if channel == Channel.EMAIL:
            return contact.email
        elif channel == Channel.SMS:
            return contact.phone
        elif channel in (Channel.IOS_PUSH, Channel.ANDROID_PUSH):
            return contact.device_tokens[0] if contact.device_tokens else None
        return None
```

### 13. 事件追踪器（`tracker.py`）

```python
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from notification_system.models import Channel, NotificationEvent, NotificationStatus, VALID_TRANSITIONS


class EventTracker:
    """带状态机校验和分析的生命周期事件追踪器。

    记录每个通知的状态转换。校验转换符合已定义的状态机。
    提供聚合分析和按通道统计。
    """

    def __init__(self, clock: callable = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # notification_id -> 事件列表（按时间排序）
        self._events: dict[str, list[NotificationEvent]] = defaultdict(list)

    def record(
        self,
        notification_id: str,
        channel: Channel,
        status: NotificationStatus,
        details: Optional[str] = None,
    ) -> None:
        """记录通知的生命周期事件。

        Args:
            notification_id: 通知标识符。
            channel: 通知通道。
            status: 新状态。
            details: 关于该转换的可选详情。
        """
        event = NotificationEvent(
            notification_id=notification_id,
            channel=channel,
            status=status,
            timestamp=self._clock(),
            details=details,
        )
        self._events[notification_id].append(event)

    def get_history(self, notification_id: str) -> list[NotificationEvent]:
        """获取通知的完整事件历史。

        Args:
            notification_id: 通知标识符。

        Returns:
            按时间顺序排列的 NotificationEvent 列表。
        """
        return list(self._events.get(notification_id, []))

    def get_current_status(self, notification_id: str) -> Optional[NotificationStatus]:
        """获取通知当前（最新）状态。"""
        events = self._events.get(notification_id)
        if not events:
            return None
        return events[-1].status

    def get_aggregate_counts(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> dict[str, int]:
        """按最终状态获取通知的聚合计数。

        Args:
            start_time: 时间范围过滤的可选起始。
            end_time: 时间范围过滤的可选结束。

        Returns:
            包含以下键的字典：total_sent、total_delivered、total_failed、total_clicked。
        """
        counts = {"total_sent": 0, "total_delivered": 0, "total_failed": 0, "total_clicked": 0}
        for notification_id, events in self._events.items():
            if not events:
                continue
            last_event = events[-1]
            if start_time and last_event.timestamp < start_time:
                continue
            if end_time and last_event.timestamp > end_time:
                continue
            if last_event.status == NotificationStatus.SENT:
                counts["total_sent"] += 1
            elif last_event.status == NotificationStatus.DELIVERED:
                counts["total_delivered"] += 1
            elif last_event.status == NotificationStatus.FAILED:
                counts["total_failed"] += 1
            elif last_event.status == NotificationStatus.CLICKED:
                counts["total_clicked"] += 1
        return counts

    def get_channel_stats(self, channel: Channel) -> dict[str, float]:
        """获取特定通道的投递统计。

        Returns:
            包含 success_rate、failure_rate 和 total 计数的字典。
        """
        total = 0
        success = 0
        failed = 0
        for events in self._events.values():
            if not events:
                continue
            # 检查此通知是否属于给定通道
            if events[0].channel != channel:
                continue
            total += 1
            last_status = events[-1].status
            if last_status in (NotificationStatus.SENT, NotificationStatus.DELIVERED, NotificationStatus.CLICKED):
                success += 1
            elif last_status == NotificationStatus.FAILED:
                failed += 1

        return {
            "total": total,
            "success_rate": success / total if total > 0 else 0.0,
            "failure_rate": failed / total if total > 0 else 0.0,
        }
```

### 14. 通知日志（`log.py`）

```python
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

from notification_system.models import Channel, LogEntry, NotificationStatus


class NotificationLogBackend(ABC):
    """通知日志持久化的抽象接口。"""

    @abstractmethod
    def append(self, entry: LogEntry) -> None:
        """追加日志条目。"""
        ...

    @abstractmethod
    def query(
        self,
        notification_id: Optional[str] = None,
        recipient_id: Optional[str] = None,
        channel: Optional[Channel] = None,
        status: Optional[NotificationStatus] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> list[LogEntry]:
        """按多种条件查询日志条目。"""
        ...

    @abstractmethod
    def cleanup(self, retention_seconds: float) -> int:
        """移除保留期之前的条目。返回移除的数量。"""
        ...


class InMemoryNotificationLog(NotificationLogBackend):
    """支持查询和保留期的内存通知日志。"""

    def __init__(self) -> None:
        self._entries: list[LogEntry] = []

    def append(self, entry: LogEntry) -> None:
        """向内存存储追加日志条目。"""
        self._entries.append(entry)

    def query(
        self,
        notification_id: Optional[str] = None,
        recipient_id: Optional[str] = None,
        channel: Optional[Channel] = None,
        status: Optional[NotificationStatus] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> list[LogEntry]:
        """查询匹配所有指定条件的条目。"""
        results = []
        for entry in self._entries:
            if notification_id and entry.notification_id != notification_id:
                continue
            if recipient_id and entry.recipient_id != recipient_id:
                continue
            if channel and entry.channel != channel:
                continue
            if status and entry.status != status:
                continue
            if start_time and entry.timestamp < start_time:
                continue
            if end_time and entry.timestamp > end_time:
                continue
            results.append(entry)
        return results

    def cleanup(self, retention_seconds: float) -> int:
        """移除保留期之前的条目。"""
        cutoff = datetime.now(timezone.utc).timestamp() - retention_seconds
        original_count = len(self._entries)
        self._entries = [
            e for e in self._entries if e.timestamp.timestamp() >= cutoff
        ]
        return original_count - len(self._entries)


class NotificationLog:
    """高层通知日志外观类。

    包装一个 NotificationLogBackend，提供记录尝试和查询历史的便捷方法。
    """

    def __init__(self, backend: Optional[NotificationLogBackend] = None) -> None:
        self._backend = backend or InMemoryNotificationLog()

    def record_attempt(
        self,
        notification_id: str,
        recipient_id: str,
        channel: Channel,
        content_summary: str,
        status: NotificationStatus,
        attempt: int = 1,
        error_details: Optional[str] = None,
    ) -> None:
        """记录一次通知尝试。"""
        entry = LogEntry(
            notification_id=notification_id,
            recipient_id=recipient_id,
            channel=channel,
            content_summary=content_summary,
            timestamp=datetime.now(timezone.utc),
            status=status,
            attempt=attempt,
            error_details=error_details,
        )
        self._backend.append(entry)

    def query(self, **kwargs) -> list[LogEntry]:
        """查询日志条目。委托给后端。"""
        return self._backend.query(**kwargs)

    def cleanup(self, retention_seconds: float) -> int:
        """清理旧条目。返回移除的数量。"""
        return self._backend.cleanup(retention_seconds)
```

### 15. Notification Service 编排器（`service.py`）

```python
import uuid
from typing import Optional

from notification_system.config import NotificationConfig
from notification_system.contacts import ContactInfoStore
from notification_system.dedup import Deduplicator
from notification_system.exceptions import ValidationError
from notification_system.models import (
    Channel,
    NotificationRequest,
    NotificationStatus,
    NotificationTask,
)
from notification_system.queue import MessageQueue
from notification_system.rate_limiter import SlidingWindowRateLimiter
from notification_system.settings import NotificationSettings
from notification_system.templates import NotificationTemplate, TemplateRegistry
from notification_system.tracker import EventTracker


class NotificationService:
    """通知管道的核心编排器。

    管道：validate → check preferences → rate limit → dedup → enqueue。

    协调所有子系统，提供发送通知、管理模板和查询状态的统一公共 API。
    """

    def __init__(
        self,
        config: Optional[NotificationConfig] = None,
        queue: Optional[MessageQueue] = None,
        settings: Optional[NotificationSettings] = None,
        contacts: Optional[ContactInfoStore] = None,
        rate_limiter: Optional[SlidingWindowRateLimiter] = None,
        deduplicator: Optional[Deduplicator] = None,
        tracker: Optional[EventTracker] = None,
        template_registry: Optional[TemplateRegistry] = None,
    ) -> None:
        self._config = config or NotificationConfig()
        self._queue = queue or MessageQueue(max_depth=self._config.queue_max_depth)
        self._settings = settings or NotificationSettings()
        self._contacts = contacts or ContactInfoStore()
        self._rate_limiter = rate_limiter or SlidingWindowRateLimiter(
            per_channel_limits=self._config.per_channel_rate_limits,
            global_limit=self._config.global_rate_limit,
        )
        self._deduplicator = deduplicator or Deduplicator(
            window_seconds=self._config.dedup_window_seconds
        )
        self._tracker = tracker or EventTracker()
        self._templates = template_registry or TemplateRegistry()

    async def send(self, request: NotificationRequest) -> str:
        """通过管道发送单条通知。

        管道步骤：
        1. 校验请求（联系信息存在）
        2. 检查用户偏好（opt-in/out）
        3. 解析内容（模板或直接传入）
        4. 限流检查
        5. 去重检查
        6. 入队到通道队列

        Args:
            request: 通知请求。

        Returns:
            唯一通知标识符。

        Raises:
            ValidationError: 请求无效时抛出。
            RateLimitExceededError: 限流超限时抛出。
            DuplicateNotificationError: 检测到重复时抛出。
        """
        notification_id = uuid.uuid4().hex

        # 1. 校验：检查联系信息是否存在
        endpoint = self._contacts.resolve_endpoint(request.recipient_id, request.channel)
        if endpoint is None:
            raise ValidationError(
                f"No contact info for user '{request.recipient_id}' on channel '{request.channel.value}'",
                field="recipient_id",
            )

        # 2. 检查偏好
        if not self._settings.is_opted_in(request.recipient_id, request.channel):
            self._tracker.record(
                notification_id, request.channel, NotificationStatus.UNSUBSCRIBED
            )
            raise ValidationError(
                f"User '{request.recipient_id}' has opted out of '{request.channel.value}'"
            )

        # 记录 CREATED 事件
        self._tracker.record(notification_id, request.channel, NotificationStatus.CREATED)

        # 3. 解析内容
        title, body = self._resolve_content(request)

        # 4. 限流
        self._rate_limiter.check(request.recipient_id, request.channel)
        self._rate_limiter.record(request.recipient_id, request.channel)

        # 5. 去重
        content = f"{title}:{body}"
        self._deduplicator.check_and_record(
            request.recipient_id, request.channel, content, notification_id
        )

        # 6. 入队
        task = NotificationTask(
            notification_id=notification_id,
            request=request,
            channel=request.channel,
        )
        await self._queue.enqueue(task)
        self._tracker.record(notification_id, request.channel, NotificationStatus.QUEUED)

        return notification_id

    async def send_batch(self, requests: list[NotificationRequest]) -> list[str]:
        """向多个收件人发送通知。

        Args:
            requests: 通知请求列表。

        Returns:
            通知标识符列表（每个请求一个）。
            失败请求在其位置返回空字符串。
        """
        results = []
        for request in requests:
            try:
                nid = await self.send(request)
                results.append(nid)
            except Exception:
                results.append("")
        return results

    def _resolve_content(self, request: NotificationRequest) -> tuple[str, str]:
        """从模板或直接字段解析通知内容。"""
        if request.template_id:
            template = self._templates.get(request.template_id)
            if template is None:
                raise ValidationError(
                    f"Template '{request.template_id}' not found",
                    field="template_id",
                )
            return template.render(request.template_params)
        return request.title, request.body
```

### 16. 公共 API（`__init__.py`）

```python
"""Notification System - 基于 asyncio 的进程内通知管道。"""

from notification_system.config import NotificationConfig, RateLimitConfig, RetryConfig
from notification_system.contacts import ContactInfoStore
from notification_system.dedup import Deduplicator
from notification_system.exceptions import (
    DeliveryError,
    DuplicateNotificationError,
    NotificationError,
    QueueFullError,
    RateLimitExceededError,
    TemplateRenderError,
    ValidationError,
)
from notification_system.log import NotificationLog, NotificationLogBackend, InMemoryNotificationLog
from notification_system.models import (
    Channel,
    ContactInfo,
    DeliveryResult,
    LogEntry,
    NotificationEvent,
    NotificationRequest,
    NotificationStatus,
    NotificationTask,
    VALID_TRANSITIONS,
)
from notification_system.provider import Provider, SimulatedProvider
from notification_system.queue import MessageQueue
from notification_system.rate_limiter import SlidingWindowRateLimiter
from notification_system.retry import RetryHandler
from notification_system.service import NotificationService
from notification_system.settings import NotificationSettings
from notification_system.templates import NotificationTemplate, TemplateRegistry
from notification_system.tracker import EventTracker
from notification_system.worker import Worker, WorkerPool

__all__ = [
    # Core
    "NotificationService",
    "NotificationConfig",
    "RateLimitConfig",
    "RetryConfig",
    # Models
    "Channel",
    "ContactInfo",
    "DeliveryResult",
    "LogEntry",
    "NotificationEvent",
    "NotificationRequest",
    "NotificationStatus",
    "NotificationTask",
    "VALID_TRANSITIONS",
    # Queue & Workers
    "MessageQueue",
    "Worker",
    "WorkerPool",
    # Provider
    "Provider",
    "SimulatedProvider",
    # Templates
    "NotificationTemplate",
    "TemplateRegistry",
    # Rate Limiting & Dedup
    "SlidingWindowRateLimiter",
    "Deduplicator",
    # Retry
    "RetryHandler",
    # Settings & Contacts
    "NotificationSettings",
    "ContactInfoStore",
    # Tracking & Logging
    "EventTracker",
    "NotificationLog",
    "NotificationLogBackend",
    "InMemoryNotificationLog",
    # Exceptions
    "NotificationError",
    "ValidationError",
    "QueueFullError",
    "RateLimitExceededError",
    "DuplicateNotificationError",
    "TemplateRenderError",
    "DeliveryError",
]
```

### 17. HTTP 演示服务器（`examples/demo_server.py`）

```python
"""仅使用 Python stdlib 的轻量级 HTTP 演示服务器。

端点：
    POST /notifications         - 发送通知
    GET  /notifications/<id>    - 获取通知状态和事件历史
    GET  /analytics             - 获取聚合投递分析
    GET  /analytics/<channel>   - 获取按通道统计
    GET  /settings/<user_id>    - 获取用户通知偏好
    PUT  /settings/<user_id>    - 更新用户通知偏好
    POST /templates             - 创建通知模板
    GET  /templates             - 列出所有模板
    POST /templates/<id>/render - 预览已渲染的模板

用法：
    python -m examples.demo_server --port 8000
"""

import asyncio
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

from notification_system import (
    Channel,
    ContactInfo,
    ContactInfoStore,
    NotificationConfig,
    NotificationRequest,
    NotificationService,
    NotificationSettings,
    NotificationTemplate,
    TemplateRegistry,
    EventTracker,
    RateLimitExceededError,
    DuplicateNotificationError,
    ValidationError,
)


class NotificationHandler(BaseHTTPRequestHandler):
    """通知系统演示的 HTTP 请求处理器。"""

    service: NotificationService
    tracker: EventTracker
    settings: NotificationSettings
    templates: TemplateRegistry
    loop: asyncio.AbstractEventLoop

    def do_POST(self) -> None:
        """处理 POST 请求。"""
        if self.path == "/notifications":
            self._handle_send_notification()
        elif self.path.startswith("/templates") and self.path.endswith("/render"):
            self._handle_render_template()
        elif self.path == "/templates":
            self._handle_create_template()
        else:
            self._send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_GET(self) -> None:
        """处理 GET 请求。"""
        if self.path.startswith("/notifications/"):
            notification_id = self.path[len("/notifications/"):]
            self._handle_get_status(notification_id)
        elif self.path.startswith("/analytics/"):
            channel_str = self.path[len("/analytics/"):]
            self._handle_channel_analytics(channel_str)
        elif self.path == "/analytics":
            self._handle_aggregate_analytics()
        elif self.path.startswith("/settings/"):
            user_id = self.path[len("/settings/"):]
            self._handle_get_settings(user_id)
        elif self.path == "/templates":
            self._handle_list_templates()
        else:
            self._send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_PUT(self) -> None:
        """处理 PUT 请求。"""
        if self.path.startswith("/settings/"):
            user_id = self.path[len("/settings/"):]
            self._handle_update_settings(user_id)
        else:
            self._send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _handle_send_notification(self) -> None:
        """POST /notifications - 发送通知。"""
        data = self._read_json()
        if data is None:
            return

        try:
            request = NotificationRequest(
                recipient_id=data["recipient_id"],
                channel=Channel(data["channel"]),
                title=data.get("title", ""),
                body=data.get("body", ""),
                template_id=data.get("template_id"),
                template_params=data.get("template_params", {}),
            )
            notification_id = self.loop.run_until_complete(self.service.send(request))
            self._send_json(HTTPStatus.CREATED, {"notification_id": notification_id})
        except RateLimitExceededError as e:
            self._send_error(HTTPStatus.TOO_MANY_REQUESTS, str(e))
        except (ValidationError, DuplicateNotificationError) as e:
            self._send_error(HTTPStatus.BAD_REQUEST, str(e))

    def _handle_get_status(self, notification_id: str) -> None:
        """GET /notifications/<id> - 获取状态和历史。"""
        history = self.tracker.get_history(notification_id)
        if not history:
            self._send_error(HTTPStatus.NOT_FOUND, "Notification not found")
            return
        events = [
            {"status": e.status.value, "timestamp": e.timestamp.isoformat(), "channel": e.channel.value}
            for e in history
        ]
        self._send_json(HTTPStatus.OK, {"notification_id": notification_id, "events": events})

    def _handle_aggregate_analytics(self) -> None:
        """GET /analytics - 聚合投递计数。"""
        counts = self.tracker.get_aggregate_counts()
        self._send_json(HTTPStatus.OK, counts)

    def _handle_channel_analytics(self, channel_str: str) -> None:
        """GET /analytics/<channel> - 按通道统计。"""
        try:
            channel = Channel(channel_str)
        except ValueError:
            self._send_error(HTTPStatus.NOT_FOUND, f"Unknown channel: {channel_str}")
            return
        stats = self.tracker.get_channel_stats(channel)
        self._send_json(HTTPStatus.OK, stats)

    def _handle_get_settings(self, user_id: str) -> None:
        """GET /settings/<user_id> - 获取偏好。"""
        prefs = self.settings.get_all_preferences(user_id)
        result = {ch.value: opted_in for ch, opted_in in prefs.items()}
        self._send_json(HTTPStatus.OK, {"user_id": user_id, "preferences": result})

    def _handle_update_settings(self, user_id: str) -> None:
        """PUT /settings/<user_id> - 更新偏好。"""
        data = self._read_json()
        if data is None:
            return
        prefs = {Channel(k): v for k, v in data.get("preferences", {}).items()}
        self.settings.bulk_update(user_id, prefs)
        self._send_json(HTTPStatus.OK, {"status": "updated"})

    def _handle_create_template(self) -> None:
        """POST /templates - 创建模板。"""
        data = self._read_json()
        if data is None:
            return
        template = NotificationTemplate(
            template_id=data["template_id"],
            title=data.get("title", ""),
            body=data.get("body", ""),
        )
        self.templates.register(template)
        self._send_json(HTTPStatus.CREATED, {"template_id": template.template_id})

    def _handle_list_templates(self) -> None:
        """GET /templates - 列出所有模板 ID。"""
        self._send_json(HTTPStatus.OK, {"templates": self.templates.list_ids()})

    def _handle_render_template(self) -> None:
        """POST /templates/<id>/render - 预览已渲染的模板。"""
        parts = self.path.split("/")
        template_id = parts[2] if len(parts) >= 4 else ""
        data = self._read_json()
        if data is None:
            return
        template = self.templates.get(template_id)
        if template is None:
            self._send_error(HTTPStatus.NOT_FOUND, f"Template '{template_id}' not found")
            return
        try:
            title, body = template.render(data.get("params", {}))
            self._send_json(HTTPStatus.OK, {"title": title, "body": body})
        except Exception as e:
            self._send_error(HTTPStatus.BAD_REQUEST, str(e))

    def _read_json(self) -> Optional[dict]:
        """读取并解析 JSON 请求体。"""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            self._send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON")
            return None

    def _send_json(self, status: HTTPStatus, data: dict) -> None:
        """发送 JSON 响应。"""
        body = json.dumps(data).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        """发送错误 JSON 响应。"""
        self._send_json(status, {"error": message})
```

## 设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 并发模型 | asyncio tasks + Queue | 单线程异步；无线程复杂度；原生异步 Queue 实现反压（backpressure） |
| 队列实现 | 每个通道一个 asyncio.Queue | 内置 maxsize 支持；FIFO 保证；非阻塞 put/get |
| Worker 池 | 每个 Worker 一个 asyncio.create_task | 轻量；协作式调度；易取消 |
| Provider 抽象 | ABC + async deliver() | 测试（模拟）与生产（真实 Provider）的干净接口 |
| 限流算法 | 滑动窗口（时间戳列表） | 准确频率追踪；无固定桶边界；实现简单 |
| 去重 | SHA-256 内容哈希 + TTL 字典 | O(1) 查找；确定性；自动过期防止内存增长 |
| 重试策略 | 指数退避 + 抖动 | 防止惊群效应；可配置上限；行业标准做法 |
| 事件追踪 | 事件列表的内存字典 | O(1) 追加；O(n) 查询；对单进程场景足够 |
| 通知日志 | ABC + InMemoryNotificationLog | 可插拔后端；默认实现无需依赖 |
| 模板引擎 | 基于正则的 {placeholder} 替换 | 零依赖；简单可预测；对通知内容足够 |
| 设置存储 | 按用户的内存字典 | O(1) 查找；默认 opted-in；opt-out 立即生效 |
| 联系信息 | 支持多令牌的内存字典 | O(1) 查找；每个用户支持多个设备令牌 |
| ID 生成 | uuid4 hex | 全局唯一；无需协调；128 位碰撞抵抗 |
| 配置 | 带校验的嵌套 dataclass | 类型安全；__post_init__ 中校验；合理默认值 |
| 项目布局 | src/ 布局 + hatchling | 标准 Python 打包；干净的导入路径 |
| HTTP 演示 | stdlib http.server | 零依赖；演示完整 API 面 |
| 错误处理 | 自定义异常层级 | 类型化错误支持精确 catch 块；全部继承 NotificationError |
| 时钟注入 | 可选 callable 参数 | 启用时间依赖组件的确定性测试 |

## 错误处理

| 错误条件 | 异常 | 何时抛出 |
|----------|------|----------|
| 缺少联系信息 | `ValidationError` | `send()` 时收件人缺少通道联系信息 |
| 用户已 opt-out | `ValidationError` | `send()` 时用户已选择不接收该通道 |
| 无效模板 ID | `ValidationError` | `send()` 时使用了不存在的 template_id |
| 队列达到容量 | `QueueFullError` | `enqueue()` 时通道队列已满 |
| 超出限流 | `RateLimitExceededError` | `send()` 时用户超出频率上限 |
| 重复通知 | `DuplicateNotificationError` | `send()` 时内容哈希匹配最近的发送 |
| 模板缺失键 | `TemplateRenderError` | `render()` 时参数字典不完整 |
| Provider 投递失败 | `DeliveryError` | Worker 从 Provider 收到失败的 DeliveryResult |
| 无效配置值 | `ValueError` | NotificationConfig.__post_init__ 中数值无效 |

所有自定义异常均继承自 `NotificationError`，用于通配捕获。

## 正确性属性

*属性（property）是系统所有有效执行都应满足的特征或行为——本质上是关于系统行为的形式化陈述。属性作为人类可读规格与机器可验证正确性保证之间的桥梁。*

### 属性 1：通道路由正确性

*对于任何* 指定通道 C 的有效通知请求，当通知成功入队时，它*必须*出现在通道 C 的队列中，并且*不得*出现在任何其他通道的队列中。

**验证：需求 1.1、2.1**

### 属性 2：opt-out 拒绝

*对于任何* 已选择不接收通道 C 通知的用户，提交该用户在通道 C 上的通知请求*必须*被拒绝，且事件追踪器*必须*为该通知记录 UNSUBSCRIBED 状态。

**验证：需求 1.2、1.6、8.3**

### 属性 3：无效收件人校验

*对于任何* 收件人没有为指定通道注册联系信息的通知请求，服务*必须*抛出 ValidationError。

**验证：需求 1.5**

### 属性 4：通知 ID 唯一性

*对于任何* N 个成功入队的通知集合，返回的所有 N 个通知标识符*必须*互不相同。

**验证：需求 1.8**

### 属性 5：队列 FIFO 顺序

*对于任何* 入队到单个通道队列的通知任务序列，全部出队*必须*按照入队顺序返回（FIFO）。

**验证：需求 2.6**

### 属性 6：队列深度不变量

*对于任何* 在通道队列上的入队和出队操作序列，报告的深度*必须*等于成功入队数减去成功出队数，且*不得*超过配置的最大深度。

**验证：需求 2.3、2.4、2.5**

### 属性 7：模板渲染替换

*对于任何* 带占位符的通知模板和完整的参数字典，渲染*必须*输出每个占位符都被对应值替换且结果中不再存在 `{placeholder}` 语法。

**验证：需求 5.1、5.2**

### 属性 8：模板缺失占位符错误

*对于任何* 至少有一个占位符的通知模板，用缺失一个或多个必需键的参数字典渲染*必须*抛出 TemplateRenderError 并列出缺失的键。

**验证：需求 5.3**

### 属性 9：模板注册往返

*对于任何* 以唯一 ID 注册的通知模板，通过该 ID 检索模板*必须*返回具有相同 ID、标题模板和正文模板的模板。

**验证：需求 5.6**

### 属性 10：限流器执行

*对于任何* 用户和通道，配置的限制为窗口 W 内 N 条通知，当窗口 W 内记录了 N 条通知后，下一次检查*必须*抛出 RateLimitExceededError。

**验证：需求 6.1、6.4**

### 属性 11：按通道限流独立性

*对于任何* 用户，在一个通道上耗尽限流*不得*影响其他通道的限流可用性（每个通道的限制独立追踪）。

**验证：需求 6.2**

### 属性 12：滑动窗口过期

*对于任何* 用户和通道，当所有记录的通知之后配置的时间窗口已完全过去时，限流器*必须*接受新通知（窗口向前滑动，旧条目过期）。

**验证：需求 6.6**

### 属性 13：去重键确定性

*对于任何* (recipient_id, channel, content) 三元组，多次计算去重键*必须*始终产生相同结果。任意组件不同的两个三元组*必须*产生不同的键。

**验证：需求 7.1**

### 属性 14：窗口内重复抑制

*对于任何* 已成功记录在去重器中的通知，在去重窗口内提交相同 (recipient_id, channel, content) 的第二条通知*必须*抛出包含原始通知 ID 的 DuplicateNotificationError。

**验证：需求 7.2、7.4**

### 属性 15：去重 TTL 过期

*对于任何* 去重条目，当配置的 TTL 已过，相同 (recipient_id, channel, content) 三元组*必须*被接受为新通知（条目已过期）。

**验证：需求 7.3、7.5**

### 属性 16：通知设置往返

*对于任何* 用户和通道，先 opt-out 再 opt-in*必须*恢复 opted-in 状态。任何更新后检索的偏好*必须*匹配最后设置的值。

**验证：需求 8.1、8.3、8.4**

### 属性 17：新用户默认 opted-in

*对于任何* 之前未注册的用户 ID，查询其偏好*必须*对所有通道返回 opted-in（True）。

**验证：需求 8.2**

### 属性 18：带上限的指数退避

*对于任何* 重试尝试次数 N，配置的 base_delay B 和 max_delay M，计算出的延迟*必须*至多为 M，*必须*遵循公式 min(B × 2^N + jitter, M)，其中 jitter 非负。

**验证：需求 9.1、9.3**

### 属性 19：重试耗尽标记永久失败

*对于任何* 投递失败且耗尽配置最大重试次数的通知，事件追踪器*必须*记录 FAILED 状态，且*不得*再进行进一步重试尝试。

**验证：需求 9.2、9.4**

### 属性 20：可重试与不可重试错误分类

*对于任何* 标记为不可重试的投递失败，重试处理器*必须*立即将通知标记为 FAILED 而不重新入队，无论剩余的重试预算如何。

**验证：需求 9.6**

### 属性 21：事件状态机有效性

*对于任何* 通知的事件历史，每对连续的状态转换*必须*是已定义状态机（VALID_TRANSITIONS 映射）中有效的转换。

**验证：需求 10.1**

### 属性 22：事件检索完整性

*对于任何* 已记录事件的通知 ID，检索事件历史*必须*按时间顺序返回所有已记录事件，每个事件包含有效的时间戳、通知 ID、通道和状态。

**验证：需求 10.2、10.3**

### 属性 23：分析聚合一致性

*对于任何* 已记录的通知事件集合，聚合计数（total_sent、total_delivered、total_failed、total_clicked）*必须*等于最新状态分别匹配每个对应类别的通知数量。

**验证：需求 10.4、10.5**

### 属性 24：通知日志查询正确性

*对于任何* 日志条目集合和任何查询过滤器（按 notification_id、recipient_id、channel、status 或时间范围），查询结果*必须*恰好包含匹配*所有*指定过滤条件的那些条目。

**验证：需求 12.1、12.2**
