# 限流器 — 第5章

实现五种算法、内存/Redis原子状态、请求规则筛选及Flask/FastAPI适配。
[English](README.md) · [章节复审与迁移](docs/chapter05-review.md) ·
[设计](docs/design_cn.md) · [实测](benchmarks/REPORT.md)。

本轮修复路径/键类型未生效、时钟回退绕过配额、跨进程身份哈希不稳定、
漏桶小数占用越界、HTTP响应不一致，并补充文件规则更新和监控计数。
249项测试通过，另有真实Redis多进程及实际HTTP实验。

## 运行

源码语法兼容Python3.9+，实际在3.13.9验证；Redis客户端实测6.4.0。
运行依赖redis-py，框架、fakeredis及pytest在dev可选依赖中。

```sh
pip install -e ".[dev]"
python -m pytest tests -q
python examples/basic_usage.py
python examples/policy_demo.py
python tools/benchmark.py --output benchmarks/local.json
```

本机既有pytest缓存和默认临时目录权限异常，复审关闭无关插件自动加载、
使用`-p no:cacheprovider`并为`--basetemp`指定`.test-tmp`内的新目录。
以`-W error`执行，仅过滤已安装Starlette1.8.0因可选httpx2未安装而产生的
`starlette.exceptions.StarletteDeprecationWarning`；没有跳过项目测试。
这些属于复审环境处理，不是应用运行要求。

## 配置与检查

```python
from rate_limiter import Algorithm, RateLimiter, RateLimiterConfig, RateLimitRule

limiter = RateLimiter(RateLimiterConfig(
    rules=[RateLimitRule(10, 60, Algorithm.TOKEN_BUCKET,
                         capacity=20, key_type="user_id",
                         path_pattern="/api/*", name="api-user")],
    fail_open=False,
))
result = limiter.check_request(request)  # 携带已认证用户的框架请求
print(result.allowed, limiter.metrics())
result = limiter.check("account-123", RateLimitRule(10, 60))
```

`path_pattern`按大小写敏感的`fnmatch`匹配URL路径，不包含查询参数；
纯字符串上下文没有路径，跳过带路径筛选的规则。
`key_type`支持ip/user_id/path/method/custom；custom必须指定key_func，
自定义函数优先。规则名、路径、键类型、函数标识、算法、窗口、限额和容量
共同隔离计数。同一请求内完全重复的规则只扣一次。
不同闭包须使用不同的稳定规则名，函数限定名不能区分捕获的变量。

匹配规则依次扣额，有一条拒绝则整体拒绝，采用最长重试时间；
多规则之间没有统一回滚事务。配置在入口复制，`config`返回副本；
`update_rules`完整校验后原子替换，每个请求持有自己的规则快照，
不在配置锁内等待存储IO。

## 算法与排队

| 算法 | 语义 |
|---|---|
| 令牌桶 | 稳态速率limit/window，可独立指定突发容量capacity |
| 漏桶 | 虚拟占用按固定速率下降，只接纳能完整放下的一个请求 |
| 固定窗口 | 计数/窗口起点；保留窗口边界突发特性 |
| 滑动日志 | 每个**成功接纳**的请求使用唯一UUID；条目数受limit限制 |
| 滑动计数 | 当前/上一窗口加权，假设上一窗口请求均匀分布 |

桶容量默认limit，桶响应中的limit代表容量；其他算法不接受capacity。
限额、窗口、容量必须为有界正整数，也限制桶排空时长。
漏桶算法只作准入判断，不延后执行HTTP请求。
若需要原书中的FIFO匀速执行，使用独立的本地队列：

```python
from rate_limiter.queue import LeakyBucketQueue
with LeakyBucketQueue(capacity=10, rate=5) as queue:
    future = queue.submit(lambda: "background result")
    print(future.result(timeout=2))
```

等待队列满时submit立即抛出标准库queue.Full；单个非daemon线程执行FIFO任务，
启动频率不超过rate，慢任务会进一步降低吞吐。关闭时取消等待任务并等待
当前任务结束，因此回调需要有界执行时间；没有持久化或跨进程交付保证。

## 文件更新与指标

[examples/rules.json](examples/rules.json)提供完整示例，JSON只能包含rules数组，
限制64KiB/100条，拒绝重复字段和未知字段。用os.replace发布完整文件。

```python
from rate_limiter.rule_files import RuleReloader
limiter.reload_rules("rules.json")  # 失败抛异常，不替换旧规则
with RuleReloader(limiter, "rules.json", interval=1) as worker:
    serve_application()
```

可选轮询线程只在完整校验成功后替换规则，失败保留旧配置；退出时停止并join。
worker.status提供reloads/errors/last_error，limiter.metrics提供固定维度的
checks/allowed/denied/storage_errors，按**规则检查次数**计数，不包含身份标签。
这些进程内指标需要外部采集器才能形成仪表盘和告警。

## 分布式、HTTP与资源边界

Redis配置使用storage_backend="redis"和redis_url；每条规则一次EVAL，
Redis TIME和检查/扣额在同一脚本内完成，不采用客户端时钟。
内存时钟在锁内采样，TTL使用monotonic；保留期间的高水位避免回退重置配额。
服务端大幅跳时、TTL/状态丢失、重启和管理员改写不在此保证范围内。

`RedisStorage(url, socket_timeout=2, max_connections=64)`提供超时和连接数上限，
关闭自动命令重试，避免丢响应后重复扣额。失败可能留下不确定扣额，按配置
fail_open/fail_closed处理；停止请求和后台任务后关闭自己持有的storage。
当前支持单Redis主节点；滑动计数在Lua内派生窗口键，因此哈希标签不代表
已经支持Redis Cluster，也没有主从故障切换或跨地域全局配额。

内存默认最多100000键，运行期间每50ms最多巡查64个过期项，容量压力下
最多巡查128项，不逐出仍有效的配额状态。清理不完整时可能暂时报告容量不足，
错误遵循失败策略。回调原子性指互斥，不代表异常后回滚。

Flask使用RateLimitExtension，FastAPI使用RateLimitMiddleware，参见
[Flask示例](examples/flask_example.py)和[FastAPI示例](examples/fastapi_example.py)。
异步适配将同步存储调用放到工作线程；取消请求不会强制中断已运行的调用。
默认拒绝状态429，可配置其他4xx/5xx；auto_headers和include_headers同时
作用于成功/拒绝响应。X-RateLimit-Reset表示相对秒数，拒绝时Retry-After
及X-RateLimit-Retry-After使用向上取整的整数秒。通用装饰器在
raise_exception=False时返回响应**字典**，实际HTTP响应应使用框架适配器。

默认ip_key只使用连接对端。仅在可信代理清除客户端伪造头后显式启用
trust_forwarded=True，优先使用框架可信代理配置。
凭证键改用跨进程稳定的SHA256；该工具不验证JWT或API密钥，真正按用户
限流应使用已认证主体/自定义提取函数，不能把未经验证的凭证当作认证结果。

## 真实验证与迁移

```sh
python tools/redis_probe.py --url redis://127.0.0.1:6379/0 --output benchmarks/redis-local.json
```

脚本使用随机独立命名空间，四个同步进程、偏移客户端时间和两个真实HTTP实例，
只删除自己的键，不清库、不停止所提供的Redis。本轮Redis8.0.5实验使用独立
服务，验证结束后已关闭；原始数据在benchmarks目录。

**迁移影响：**核心键改为v2及规则/身份哈希命名空间，旧配额会重置，需协调
发布，不能混跑旧新节点。默认代理信任、查询参数路径和拒绝响应关闭头的
语义也有变化，见[复审记录](docs/chapter05-review.md)。
边缘部署、多地域全局限流、Redis HA/Cluster、持久执行队列和自适应规则
仍属于部署/设计扩展，未宣称已实现。
