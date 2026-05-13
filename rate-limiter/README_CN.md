# Rate Limiter 限流器

一个生产级的 Python 限流库，支持多种限流算法、可配置规则以及本地（内存）和分布式（Redis）存储后端。为 Flask 和 FastAPI 提供中间件集成，支持标准 HTTP 429 响应和限流响应头。

> English documentation: [README.md](./README.md)

## 核心特性

- **5 种限流算法**：令牌桶、漏桶、固定窗口、滑动窗口日志、滑动窗口计数器
- **2 种存储后端**：内存（单进程）和 Redis（分布式）
- **框架集成**：FastAPI 中间件、Flask 扩展和通用装饰器
- **灵活的键解析**：基于 IP、用户 ID、复合键和自定义函数
- **多规则支持**：单个端点可应用多个规则，取最严格结果
- **动态规则更新**：运行时修改规则无需重启
- **容错机制**：可配置的故障开放或故障关闭行为
- **标准响应头**：`X-RateLimit-Remaining`、`Retry-After` 等

## 系统架构

```
┌──────────────────────┐   ┌───────────────────┐   ┌───────────────────────┐
│     用户接口层        │   │     核心协调层     │   │      算法实现层        │
│                      │   │                    │   │                       │
│ • @rate_limit 装饰器  │──▶│ • RateLimiter     │──▶│ • TokenBucket 令牌桶   │
│ • FastAPI 中间件      │   │ • 规则引擎         │   │ • LeakingBucket 漏桶   │
│ • Flask 扩展          │   │ • 键解析器         │   │ • FixedWindow 固定窗口 │
│                      │   │                    │   │ • SlidingWindowLog    │
└──────────────────────┘   └───────────────────┘   │ • SlidingWindowCounter│
                                                    └───────────┬───────────┘
                                                                │
                                                    ┌───────────▼───────────┐
                                                    │      存储后端层        │
                                                    │                       │
                                                    │ • MemoryStorage 内存   │
                                                    │ • RedisStorage Redis   │
                                                    └───────────────────────┘
```

### 分层职责

| 层级 | 组件 | 职责 |
|------|------|------|
| 用户接口层 | 装饰器、FastAPI 中间件、Flask 扩展 | 面向用户的 API，框架钩子 |
| 核心协调层 | RateLimiter、配置、键解析器 | 编排协调、规则评估、键解析 |
| 算法实现层 | 5 种算法实现 | 限流检查逻辑、状态管理 |
| 存储后端层 | 内存、Redis | 持久化状态、原子操作 |

## 安装

```bash
pip install -e .
```

安装开发依赖：

```bash
pip install -e ".[dev]"
```

### 环境要求

- Python >= 3.9
- Redis（仅在使用 Redis 存储后端时需要）

## 快速开始

```python
from rate_limiter import RateLimiter, RateLimiterConfig, RateLimitRule, Algorithm

# 定义规则
rules = [
    RateLimitRule(limit=100, window=60, algorithm=Algorithm.TOKEN_BUCKET),
]

# 创建限流器
config = RateLimiterConfig(rules=rules)
limiter = RateLimiter(config)

# 检查请求
result = limiter.check_request("user-123")

if result.allowed:
    print(f"请求允许，剩余 {result.remaining} 次")
else:
    print(f"已限流，{result.retry_after} 秒后重试")
```

### 使用装饰器

```python
from rate_limiter import rate_limit

@rate_limit(limit=10, window=60, algorithm="token_bucket")
def my_endpoint():
    return "Hello, world!"
```

## 配置参考

### RateLimiterConfig 全局配置

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `rules` | `list[RateLimitRule]` | `[]` | 限流规则列表 |
| `storage_backend` | `str` | `"memory"` | `"memory"` 或 `"redis"` |
| `redis_url` | `str` | `"redis://localhost:6379"` | Redis 连接 URL |
| `key_prefix` | `str` | `"rl:"` | 存储键前缀 |
| `include_headers` | `bool` | `True` | 是否在响应中包含限流头 |
| `fail_open` | `bool` | `True` | 存储不可用时是否允许请求 |

### RateLimitRule 规则配置

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `limit` | `int` | *（必需）* | 窗口内最大请求数 |
| `window` | `int` | *（必需）* | 时间窗口（秒） |
| `algorithm` | `Algorithm` | `TOKEN_BUCKET` | 使用的算法 |
| `key_func` | `Callable` | `None` | 自定义键解析函数 |
| `key_type` | `str` | `"ip"` | `"ip"`、`"user_id"` 或 `"custom"` |
| `path_pattern` | `str` | `None` | 端点路径模式 |
| `name` | `str` | `None` | 规则名称（便于识别） |

## 算法详解

### 令牌桶 (Token Bucket)

维护一个令牌桶，以固定速率填充令牌，每个请求消耗一个令牌。允许突发流量直到桶容量耗尽。

- **状态**：`tokens`（当前令牌数）+ `last_refill`（上次填充时间）
- **优势**：优雅处理突发流量
- **劣势**：需要跟踪两个值
- **适用场景**：允许短时突发的 API 限流

```python
# 原子操作伪代码
elapsed = now - last_refill
tokens = min(capacity, tokens + elapsed * refill_rate)
if tokens >= 1:
    tokens -= 1  # 允许
else:
    retry_after = (1 - tokens) / refill_rate  # 拒绝
```

### 漏桶 (Leaking Bucket)

维护一个以固定速率排空的队列。队列满时拒绝新请求。

- **状态**：`queue_count`（队列长度）+ `last_drain`（上次排空时间）
- **优势**：保证稳定的输出速率
- **劣势**：不允许突发流量
- **适用场景**：需要稳定、可预测吞吐量的系统

### 固定窗口 (Fixed Window)

将时间划分为固定大小的窗口，每个窗口独立计数。窗口边界时重置。

- **状态**：`window_start`（窗口起始时间）+ `counter`（计数器）
- **优势**：简单高效，内存占用少
- **劣势**：窗口边界可能允许 2 倍流量
- **适用场景**：简单场景，可接受边界突发

### 滑动窗口日志 (Sliding Window Log)

记录每个请求的精确时间戳，移除过期条目后计数。

- **状态**：时间戳有序集合
- **优势**：最精确，无边界问题
- **劣势**：内存占用高（存储每个时间戳）
- **适用场景**：需要精确限流的严格场景

### 滑动窗口计数器 (Sliding Window Counter)

当前窗口计数与前一窗口计数的加权和，近似真正的滑动窗口。

- **状态**：两个计数器（当前窗口 + 前一窗口），按窗口起始时间作为键
- **优势**：低内存下的良好精度
- **劣势**：近似值（假设前一窗口请求均匀分布）
- **适用场景**：平衡精度和资源效率

```python
# 加权计算公式
window_progress = (now - current_window_start) / window
overlap_ratio = 1.0 - window_progress
sliding_count = current_count + (previous_count * overlap_ratio)
```

## 框架集成

### FastAPI 中间件

```python
from fastapi import FastAPI
from rate_limiter import RateLimiter, RateLimiterConfig, RateLimitRule, Algorithm
from rate_limiter.middleware.fastapi import RateLimitMiddleware

app = FastAPI()

rules = [
    RateLimitRule(limit=100, window=60, algorithm=Algorithm.TOKEN_BUCKET),
    RateLimitRule(limit=1000, window=3600, algorithm=Algorithm.SLIDING_WINDOW_COUNTER),
]
config = RateLimiterConfig(rules=rules)
limiter = RateLimiter(config)

app.add_middleware(RateLimitMiddleware, limiter=limiter)

@app.get("/api/data")
async def get_data():
    return {"message": "Hello"}
```

限流响应示例：

```
HTTP/1.1 429 Too Many Requests
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 45.2
Retry-After: 45.2
```

### Flask 扩展

```python
from flask import Flask
from rate_limiter import RateLimiter, RateLimiterConfig, RateLimitRule, Algorithm
from rate_limiter.middleware.flask import RateLimitExtension

app = Flask(__name__)

rules = [RateLimitRule(limit=100, window=60, algorithm=Algorithm.TOKEN_BUCKET)]
config = RateLimiterConfig(rules=rules)
limiter = RateLimiter(config)

rate_ext = RateLimitExtension(app, limiter=limiter)

@app.route("/api/data")
def get_data():
    return {"message": "Hello"}
```

### 装饰器

```python
from rate_limiter import rate_limit
from rate_limiter.decorators import RateLimitExceededException

@rate_limit(limit=5, window=60, algorithm="sliding_window_counter")
def process_order():
    return "Order processed"

try:
    result = process_order()
except RateLimitExceededException as e:
    print(f"已限流：{e.retry_after} 秒后重试")
```

## 键解析系统

### 内置键函数

| 函数 | 说明 |
|------|------|
| `ip_key` | 客户端 IP（支持 `X-Forwarded-For`） |
| `user_id_key` | 用户身份（JWT、API Key、用户对象） |
| `composite_key` | 组合多个键函数 |
| `path_key` | 请求路径 |
| `method_key` | HTTP 方法 |

### 自定义键函数

```python
def tenant_key(request):
    return f"tenant:{request.headers.get('X-Tenant-ID', 'default')}"

rule = RateLimitRule(limit=100, window=60, key_func=tenant_key)
```

## 存储后端

### 内存存储（默认）

线程安全的进程内存储，无外部依赖。

```python
config = RateLimiterConfig(storage_backend="memory")
```

- 通过 `threading.Lock` 保证原子操作
- 基于 TTL 的自动过期
- 重启后状态丢失

### Redis 存储（分布式）

集中式存储，支持多实例部署。

```python
config = RateLimiterConfig(storage_backend="redis", redis_url="redis://localhost:6379")
```

- 通过 Lua 脚本保证原子操作（无竞态条件）
- 跨进程/服务器共享状态
- 连接超时：2 秒

### 故障开放 vs 故障关闭

```python
# 故障开放（默认）：存储不可用时允许请求
config = RateLimiterConfig(fail_open=True)

# 故障关闭：存储不可用时拒绝请求
config = RateLimiterConfig(fail_open=False)
```

- **故障开放**：优先保证可用性，存储故障时放行请求
- **故障关闭**：优先保证安全性，存储故障时拒绝请求

两种模式下存储错误都会记录为警告日志，不会导致应用崩溃。

## 扩展性设计

### 新增算法

继承 `BaseAlgorithm` 并实现 `check` 方法：

```python
from rate_limiter.algorithms.base import BaseAlgorithm, RateLimitResult

class CustomAlgorithm(BaseAlgorithm):
    def check(self, key, rule, storage):
        # 实现自定义逻辑
        return RateLimitResult(allowed=True, remaining=99, limit=100, reset_after=60.0, retry_after=None)
```

### 新增存储后端

继承 `BaseStorage` 并实现必要方法：

```python
from rate_limiter.storage.base import BaseStorage

class CustomStorage(BaseStorage):
    def get(self, key): ...
    def set(self, key, value, ttl=None): ...
    def increment(self, key, amount=1, ttl=None): ...
    def execute_atomic(self, script, keys, args): ...
```

## 性能

| 算法 | 时间复杂度 | 每键空间占用 |
|------|-----------|-------------|
| 令牌桶 | O(1) | O(1) — 2 个值 |
| 漏桶 | O(1) | O(1) — 2 个值 |
| 固定窗口 | O(1) | O(1) — 2 个值 |
| 滑动窗口日志 | O(n) | O(n) — 所有时间戳 |
| 滑动窗口计数器 | O(1) | O(1) — 2 个计数器 |

Redis 每次检查增加一次网络往返（单次 Lua 脚本执行）。

## 部署建议

### 单机部署
- 使用内存存储后端
- 适合单进程应用
- 配置简单，性能最佳

### 分布式部署
- 使用 Redis 存储后端
- 支持多进程、多服务器
- 需要 Redis 高可用配置

### 生产配置示例

```python
config = RateLimiterConfig(
    storage_backend="redis",
    redis_url="redis://cluster-node:6379",
    fail_open=True,
    include_headers=True,
    key_prefix="prod:rl:",
)
```

## 运行测试

```bash
pip install -e ".[dev]"
pytest                          # 全部 180 个测试
pytest tests/test_algorithms/   # 算法测试
pytest tests/test_storage/      # 存储测试
pytest tests/test_middleware/    # 中间件测试
pytest tests/test_core.py       # 核心协调器测试
pytest tests/test_decorators.py # 装饰器测试
```

## License

MIT
