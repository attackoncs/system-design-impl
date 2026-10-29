# 任务清单：限流组件（Rate Limiter）

## 任务 1：项目搭建与基础接口

- [x] 1.1 创建 `pyproject.toml`，包含项目元数据、依赖（redis、pytest、flask、fastapi、uvicorn、httpx）和构建配置
- [x] 1.2 创建 `src/rate_limiter/__init__.py`，导出公共 API
- [x] 1.3 创建 `src/rate_limiter/algorithms/base.py`，包含 `BaseAlgorithm` 抽象基类和 `RateLimitResult` dataclass
- [x] 1.4 创建 `src/rate_limiter/storage/base.py`，包含 `BaseStorage` 抽象基类，定义 get、set、increment 和 execute_atomic 方法
- [x] 1.5 创建 `src/rate_limiter/config.py`，包含 `Algorithm` 枚举、`RateLimitRule` dataclass 和 `RateLimiterConfig` dataclass

## 任务 2：内存存储后端

- [x] 2.1 创建 `src/rate_limiter/storage/__init__.py`，导出存储相关类
- [x] 2.2 实现 `src/rate_limiter/storage/memory.py` 中的 `MemoryStorage` 类，使用 threading 锁保证线程安全，通过过期时间戳支持 TTL，并实现原子 execute 方法
- [x] 2.3 创建 `tests/test_storage/__init__.py` 和 `tests/test_storage/test_memory.py`，覆盖 get/set/increment/TTL 过期/线程安全的单元测试

## 任务 3：Redis 存储后端

- [x] 3.1 实现 `src/rate_limiter/storage/redis.py` 中的 `RedisStorage` 类，封装 redis-py，通过 pipeline 支持 get/set/increment，通过 Lua 脚本求值实现 execute_atomic
- [x] 3.2 创建 `tests/test_storage/test_redis.py`，使用 fakeredis 或 mocked redis 编写 get/set/increment/Lua 脚本执行的单元测试

## 任务 4：Token Bucket（令牌桶）算法

- [x] 4.1 创建 `src/rate_limiter/algorithms/__init__.py`，导出各算法类
- [x] 4.2 实现 `src/rate_limiter/algorithms/token_bucket.py` 中的 `TokenBucketAlgorithm` 类 —— 维护令牌数和上次补充时间，根据经过的时间补充令牌，每次请求消耗一个令牌
- [x] 4.3 在 token_bucket.py 中编写 Lua 脚本常量，用于 Redis 上原子执行令牌桶操作
- [x] 4.4 创建 `tests/test_algorithms/__init__.py` 和 `tests/test_algorithms/test_token_bucket.py`，覆盖：令牌充足时放行、令牌耗尽时拒绝、随时间补充、突发流量处理

## 任务 5：Leaking Bucket（漏桶）算法

- [x] 5.1 实现 `src/rate_limiter/algorithms/leaking_bucket.py` 中的 `LeakingBucketAlgorithm` 类 —— 维护一个以固定速率流出的队列计数器，队列满时拒绝
- [x] 5.2 编写用于 Redis 原子漏桶操作的 Lua 脚本
- [x] 5.3 创建 `tests/test_algorithms/test_leaking_bucket.py`，覆盖：队列有空间时放行、队列满时拒绝、随时间流出

## 任务 6：Fixed Window Counter（固定窗口计数器）算法

- [x] 6.1 实现 `src/rate_limiter/algorithms/fixed_window.py` 中的 `FixedWindowAlgorithm` 类 —— 对当前时间窗口内的计数器递增，窗口边界处重置
- [x] 6.2 编写用于 Redis 原子固定窗口操作的 Lua 脚本
- [x] 6.3 创建 `tests/test_algorithms/test_fixed_window.py`，覆盖：窗口内计数、边界重置、达到限流值时拒绝

## 任务 7：Sliding Window Log（滑动窗口日志）算法

- [x] 7.1 实现 `src/rate_limiter/algorithms/sliding_window_log.py` 中的 `SlidingWindowLogAlgorithm` 类 —— 存储每次请求的时间戳，移除过期条目，统计剩余数量
- [x] 7.2 编写用于 Redis 原子滑动窗口日志操作的 Lua 脚本（ZADD + ZREMRANGEBYSCORE + ZCARD）
- [x] 7.3 创建 `tests/test_algorithms/test_sliding_window_log.py`，覆盖：限流内放行、达到限流值时拒绝、旧条目过期

## 任务 8：Sliding Window Counter（滑动窗口计数器）算法

- [x] 8.1 实现 `src/rate_limiter/algorithms/sliding_window_counter.py` 中的 `SlidingWindowCounterAlgorithm` 类 —— 根据时间重叠比例对当前窗口与上一窗口的计数加权求和
- [x] 8.2 编写用于 Redis 原子滑动窗口计数器操作的 Lua 脚本
- [x] 8.3 创建 `tests/test_algorithms/test_sliding_window_counter.py`，覆盖：加权计算准确性、窗口过渡、边界行为

## 任务 9：键解析

- [x] 9.1 实现 `src/rate_limiter/keys.py` 中的 `ip_key`（处理 X-Forwarded-For）、`user_id_key` 和 `composite_key` 工厂函数
- [x] 9.2 创建 `tests/test_keys.py`，覆盖：直接请求与代理请求的 IP 提取、用户 ID 提取、复合键生成

## 任务 10：核心 RateLimiter 编排器

- [x] 10.1 实现 `src/rate_limiter/core.py` 中的 `RateLimiter` 类 —— 初始化存储与算法，提供单规则检查的 `check()` 和全规则检查的 `check_request()`，支持运行时动态配置的 `update_rules()`，并处理存储异常时的 fail-open / fail-closed 行为
- [x] 10.2 创建 `tests/test_core.py`，覆盖：单规则检查、多规则（最严格优先）、动态规则更新、存储后端选择、fail-open/fail-closed 行为、普通字符串 key 支持

## 任务 11：装饰器集成

- [x] 11.1 实现 `src/rate_limiter/decorators.py` 中的 `rate_limit` 装饰器 —— 解析键、检查限流、在被拒时抛出适当异常或返回 429 响应
- [x] 11.2 创建 `tests/test_decorators.py`，覆盖：基础函数装饰、限流执行、自定义键函数、被拒时正确抛异常

## 任务 12：FastAPI 中间件

- [x] 12.1 创建 `src/rate_limiter/middleware/__init__.py`，导出中间件相关类
- [x] 12.2 实现 `src/rate_limiter/middleware/fastapi.py` 中的 `RateLimitMiddleware`（ASGI）—— 检查所有适用规则，被拒时返回带响应头的 429 JSONResponse，放行时向响应添加限流响应头
- [x] 12.3 创建 `tests/test_middleware/__init__.py` 和 `tests/test_middleware/test_fastapi.py`，使用 httpx/TestClient 覆盖：放行请求获得响应头、被拒请求返回 429、多规则应用

## 任务 13：Flask 中间件

- [x] 13.1 实现 `src/rate_limiter/middleware/flask.py` 中的 `RateLimitExtension` 类 —— 注册 before_request 和 after_request 钩子，被拒时返回带响应头的 429
- [x] 13.2 创建 `tests/test_middleware/test_flask.py`，使用 Flask 测试客户端覆盖：放行请求、被拒请求返回 429 及正确响应头

## 任务 14：示例与文档

- [x] 14.1 创建 `examples/basic_usage.py`，演示使用装饰器进行内存限流
- [x] 14.2 创建 `examples/fastapi_example.py`，演示带限流中间件和多条规则的 FastAPI 应用
- [x] 14.3 创建 `examples/flask_example.py`，演示带限流扩展的 Flask 应用
- [x] 14.4 创建 `README.md`，包含安装说明、快速入门、配置参考、算法说明和集成指南
