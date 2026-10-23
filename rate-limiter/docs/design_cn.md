# 设计文档：限流组件（Rate Limiter）

## 架构概览

限流组件采用分层架构，在算法层、存储层、配置层和集成层之间进行清晰的职责分离。

```
┌─────────────────────────────────────────────────┐
│           集成层 Integration Layer                │
│   (装饰器 Decorators、FastAPI 中间件、Flask 中间件) │
├─────────────────────────────────────────────────┤
│           限流核心 Rate Limiter Core              │
│   (RateLimiter 类 —— 编排限流检查)                 │
├─────────────────────────────────────────────────┤
│           算法层 Algorithm Layer                   │
│   (TokenBucket, LeakingBucket, FixedWindow,     │
│    SlidingWindowLog, SlidingWindowCounter)       │
├─────────────────────────────────────────────────┤
│           存储层 Storage Layer                     │
│   (MemoryBackend, RedisBackend)                 │
├─────────────────────────────────────────────────┤
│           配置 Configuration                       │
│   (规则 Rules、限流值 limits、键解析器 key resolvers) │
└─────────────────────────────────────────────────┘
```

## 项目结构

```
rate_limiter/
├── pyproject.toml
├── README.md
├── src/
│   └── rate_limiter/
│       ├── __init__.py
│       ├── core.py              # RateLimiter 编排器
│       ├── config.py            # 规则定义、配置加载
│       ├── algorithms/
│       │   ├── __init__.py
│       │   ├── base.py          # 抽象基类
│       │   ├── token_bucket.py
│       │   ├── leaking_bucket.py
│       │   ├── fixed_window.py
│       │   ├── sliding_window_log.py
│       │   └── sliding_window_counter.py
│       ├── storage/
│       │   ├── __init__.py
│       │   ├── base.py          # 抽象基类存储
│       │   ├── memory.py        # 内存线程安全存储
│       │   └── redis.py         # 基于 Lua 脚本的 Redis 存储
│       ├── middleware/
│       │   ├── __init__.py
│       │   ├── fastapi.py       # FastAPI 的 ASGI 中间件
│       │   └── flask.py         # Flask 的 WSGI 中间件/扩展
│       ├── decorators.py        # @rate_limit 装饰器
│       └── keys.py              # 键解析函数
├── tests/
│   ├── __init__.py
│   ├── test_algorithms/
│   │   ├── __init__.py
│   │   ├── test_token_bucket.py
│   │   ├── test_leaking_bucket.py
│   │   ├── test_fixed_window.py
│   │   ├── test_sliding_window_log.py
│   │   └── test_sliding_window_counter.py
│   ├── test_storage/
│   │   ├── __init__.py
│   │   ├── test_memory.py
│   │   └── test_redis.py
│   ├── test_core.py
│   ├── test_decorators.py
│   ├── test_middleware/
│   │   ├── __init__.py
│   │   ├── test_fastapi.py
│   │   └── test_flask.py
│   └── test_keys.py
└── examples/
    ├── basic_usage.py
    ├── fastapi_example.py
    └── flask_example.py
```

## 组件设计

### 1. 算法基类

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class RateLimitResult:
    """限流检查结果。"""
    allowed: bool
    remaining: int
    limit: int
    reset_after: float                      # 限流窗口重置所需秒数
    retry_after: Optional[float] = None     # 客户端需等待秒数后重试（允许时为 None）


class BaseAlgorithm(ABC):
    """限流算法的抽象基类。"""

    @abstractmethod
    def check(self, key: str, rule: "RateLimitRule", storage: "BaseStorage") -> RateLimitResult:
        """检查请求是否应被放行。

        参数：
            key: 限流主体的唯一标识
            rule: 要应用的限流规则
            storage: 用于持久化状态的存储后端

        返回：
            RateLimitResult，表明本次请求是否被允许
        """
        ...
```

### 2. 存储基类

```python
from abc import ABC, abstractmethod
from typing import Any, Optional


class BaseStorage(ABC):
    """存储后端的抽象基类。"""

    @abstractmethod
    def get(self, key: str) -> Optional[Any]:
        """获取指定 key 的值。"""
        ...

    @abstractmethod
    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        """设置 key 的值，可选秒级 TTL。"""
        ...

    @abstractmethod
    def increment(self, key: str, amount: int = 1, ttl: Optional[float] = None) -> int:
        """原子递增计数器，返回递增后的新值。"""
        ...

    @abstractmethod
    def execute_atomic(self, script: str, keys: list[str], args: list[Any]) -> Any:
        """执行原子操作（Redis 为 Lua 脚本，内存存储为加锁执行）。"""
        ...
```

### 3. 配置模型

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


class Algorithm(Enum):
    TOKEN_BUCKET = "token_bucket"
    LEAKING_BUCKET = "leaking_bucket"
    FIXED_WINDOW = "fixed_window"
    SLIDING_WINDOW_LOG = "sliding_window_log"
    SLIDING_WINDOW_COUNTER = "sliding_window_counter"


@dataclass
class RateLimitRule:
    """单条限流规则。"""
    limit: int                              # 最大请求数
    window: int                             # 时间窗口，单位秒
    algorithm: Algorithm = Algorithm.TOKEN_BUCKET
    key_func: Optional[Callable] = None     # 自定义键提取函数
    key_type: str = "ip"                    # "ip"、"user_id"、"custom"
    path_pattern: Optional[str] = None      # 特定路由模式，None 表示全部
    name: Optional[str] = None              # 规则的可读名称


@dataclass
class RateLimiterConfig:
    """顶层配置。"""
    rules: list[RateLimitRule] = field(default_factory=list)
    storage_backend: str = "memory"         # "memory" 或 "redis"
    redis_url: str = "redis://localhost:6379"
    key_prefix: str = "rl:"                 # 存储键前缀
    default_response_code: int = 429
    include_headers: bool = True
    fail_open: bool = True                  # 存储失败时是否放行请求
```

### 4. 核心 RateLimiter 类

```python
class RateLimiter:
    """主编排器，将算法、存储和配置串联在一起。"""

    def __init__(self, config: RateLimiterConfig):
        self._config = config
        self._storage = self._create_storage(config)
        self._algorithms: dict[Algorithm, BaseAlgorithm] = self._load_algorithms()

    def check(self, key: str, rule: RateLimitRule) -> RateLimitResult:
        """针对单条规则检查请求。

        根据 fail_open 配置处理存储层异常。
        """
        algorithm = self._algorithms[rule.algorithm]
        try:
            return algorithm.check(key, rule, self._storage)
        except Exception:
            if self._config.fail_open:
                return RateLimitResult(allowed=True, ...)
            else:
                return RateLimitResult(allowed=False, ...)

    def check_request(self, request_context, rules=None) -> RateLimitResult:
        """针对所有适用规则检查请求。

        返回最严格的结果（首次拒绝，或剩余配额最低）。
        支持普通字符串作为 key，或请求对象以自动解析键。
        """
        ...

    def update_rules(self, rules: list[RateLimitRule]) -> None:
        """运行时动态更新规则。"""
        self._config.rules = rules
```

### 5. Redis Lua 脚本（竞态条件处理）

每种算法对应一个 Lua 脚本以确保 Redis 操作的原子性：

**Token Bucket（令牌桶）Lua 脚本（概念示意）：**
```lua
-- KEYS[1] = 桶的 key
-- ARGV[1] = 容量 capacity, ARGV[2] = 填充速率 refill_rate, ARGV[3] = 当前时间
local tokens = tonumber(redis.call('hget', KEYS[1], 'tokens') or ARGV[1])
local last_refill = tonumber(redis.call('hget', KEYS[1], 'last_refill') or ARGV[3])
local elapsed = tonumber(ARGV[3]) - last_refill
local refilled = math.min(tonumber(ARGV[1]), tokens + elapsed * tonumber(ARGV[2]))
if refilled >= 1 then
    redis.call('hset', KEYS[1], 'tokens', refilled - 1)
    redis.call('hset', KEYS[1], 'last_refill', ARGV[3])
    redis.call('expire', KEYS[1], math.ceil(tonumber(ARGV[1]) / tonumber(ARGV[2])) + 1)
    return {1, math.floor(refilled - 1), tonumber(ARGV[1])}
else
    return {0, 0, tonumber(ARGV[1])}
end
```

### 6. 装饰器设计

```python
from functools import wraps
from typing import Callable, Optional


def rate_limit(
    limit: int,
    window: float,
    algorithm: str = "token_bucket",
    key_func: Optional[Callable] = None,
    limiter: Optional["RateLimiter"] = None,
):
    """装饰器，为函数或路由处理器加上限流。

    用法：
        @rate_limit(limit=100, window=60)
        def my_endpoint():
            ...
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # 从上下文解析 key
            # 检查限流
            # 若被拒，抛出/返回 429
            # 若放行，调用原函数
            ...
        return wrapper
    return decorator
```

### 7. FastAPI 中间件

```python
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class RateLimitMiddleware(BaseHTTPMiddleware):
    """FastAPI 的 ASGI 限流中间件。"""

    def __init__(self, app, limiter: "RateLimiter"):
        super().__init__(app)
        self.limiter = limiter

    async def dispatch(self, request: Request, call_next):
        # 从请求中解析 key
        # 检查所有适用规则
        # 若被拒：返回带响应头的 429
        # 若放行：继续处理并向响应添加限流头
        ...
```

### 8. 键解析

```python
from typing import Callable, Optional


def ip_key(request) -> str:
    """从请求中提取客户端 IP（支持 X-Forwarded-For 代理）。"""
    ...


def user_id_key(request) -> str:
    """从请求中提取已认证用户的 ID。"""
    ...


def composite_key(*extractors: Callable) -> Callable:
    """将多个键提取器组合成一个复合键。"""
    def resolver(request) -> str:
        parts = [extractor(request) for extractor in extractors]
        return ":".join(parts)
    return resolver
```

## 设计决策

| 决策点 | 方案 | 理由 |
|--------|------|------|
| 算法选择 | 按规则配置不同算法 | 不同接口可能需要不同算法（允许突发 vs 严格限制） |
| 存储抽象 | 基于接口抽象 | 可在不改动算法的前提下切换内存/Redis 存储 |
| Redis 原子性 | Lua 脚本 | 避免分布式环境中的竞态条件（单次往返完成） |
| 分布式同步 | 中心化 Redis | 多个限流实例通过 Redis 共享状态，无需粘性会话 |
| 线程安全（内存） | 每次操作加 threading.Lock | 在安全性与性能之间取得平衡 |
| 键解析 | 可插拔函数 | 为自定义限流键提供最大灵活性 |
| 中间件方式 | 同时支持 ASGI 和 WSGI | 覆盖 FastAPI 与 Flask 生态 |
| 响应头格式 | X-RateLimit-* + Retry-After | 行业标准（GitHub、Stripe 模式） |
| 容错策略 | 可配置的 fail-open / fail-closed | 允许运维方在可用性与安全性之间权衡 |
| 滑动窗口计数器键 | 基于窗口起始时间 | 确保窗口切换正确，无需显式状态迁移 |

## 错误处理

- **Redis 连接失败**：通过 `fail_open` 配置决定行为 —— 默认 fail-open（放行请求），或 fail-closed（拒绝请求）。失败会以警告级别记录。
- **Redis 超时**：连接和 socket 超时均设为 2 秒，防止阻塞。
- **无效配置**：初始化时抛出带描述信息的 `ValueError`。
- **缺少键解析器**：回退到基于 IP 的键解析。
- **普通字符串请求上下文**：当 `check_request` 收到普通字符串而非请求对象时，直接将该字符串作为限流 key 使用。
