# 工程审查总览

## 当前状态（2026-11-06）

根目录列出的 **27个独立子项目均已有按章节复审记录**，包括原书覆盖核对、开源实现对照、CodeGraph调用链、修改或保留理由，以及验证结果和部署边界。逐项目入口见[复审覆盖清单](docs/review-coverage_cn.md)和[深入审查进度](DEEP_REVIEW_PROGRESS.md)。第1、4、30章是阅读/学习章节，不计入27个业务子项目。

“已复审”表示选定参考实现已按上述流程检查，不表示原书讨论的所有生产能力均已实现。自动供给/安全缩容、真正共识、跨地域容灾、真实供应商等遗留项，仍以各章记录为准。

| 最近验证范围 | 已有证据 | 计数边界 |
| --- | --- | --- |
| 第2章，从零扩容 | 本轮77项通过、5个子例通过，51.35秒；修复租约检查与写入竞态、非法TTL破坏旧缓存 | [章节记录](scale-from-zero/docs/chapter02-review.md) · [验证报告](scale-from-zero/benchmarks/REPORT.md) |
| 第3章，容量估算 | 本轮16项通过，0.52秒；修复精确节点向上取整及备用节点大数相加 | [章节记录](capacity-estimation/docs/chapter03-review.md) · [验证报告](capacity-estimation/benchmarks/REPORT.md) |
| 第13章，聊天系统 | 11月5日主套202项通过、1项跳过，6.612秒；11月6日独立Sentinel补测1项通过，JUnit 5.498秒、恢复2.319秒 | 两轮覆盖203个不同用例，不能记为单次203项通过；见[章节记录](chat-system/docs/chapter13-review.md)和[验证报告](chat-system/benchmarks/results.md) |

本轮覆盖补齐时重新执行的是第2、3章，其余25项沿用各自日期的既有验证证据；第13章上述独立补测也按单独环境记录。本次文档同步没有重跑全仓测试，也不生成新的全仓通过总数。第5章最新265项、第12章最新311项等数字，已在[当前逐章索引](DEEP_REVIEW_PROGRESS.md)对应到各章报告。

以下保留 **2026-10-23基础审查的历史范围、表格及原始数字**。其中1,949项通过、7项跳过及当时的功能/环境说明，仅描述该轮结果；后续真实Redis、PostgreSQL、Sentinel和新增回归的状态应查看上方入口，不将新旧结果相加或覆盖。

## 历史记录：全仓库工程审查 — 2026-10-23

> 本文记录此前全仓库基础审查及当时测试结果。后续按章节深入修改的最新状态、测试数和文档更新见 [DEEP_REVIEW_PROGRESS.md](DEEP_REVIEW_PROGRESS.md)，不以本报告代表全部深入审查已完成。

### 范围与验证

本轮覆盖全部 **27 个独立子项目、244 个 Python 源文件**，保留各章节独立安装方式与公开 API。全仓库统一源码排版、检查未定义名称和无用导入；对发现的并发、资源释放和职责组织问题做针对性修复。生成的 protobuf 文件不参与格式化。容量估算、附近好友等项目主要完成可读性整理和回归验证，并未为了形式统一强行重写架构。

27 个项目最新完整测试结果合计 **1,921 passed**；另有跨项目资源契约测试 **28 passed**，共 **1,949 passed**。使用真实 Redis 的聊天、附近好友结果替代默认跳过集成测试的结果，不重复计数。剩余 **7 skipped**：视频 PostgreSQL 集成测试 6 项未配置 `VIDEO_TEST_DATABASE_DSN`，聊天 Sentinel 故障转移测试 1 项未配置 `CHAT_TEST_REDIS_LAB`。排行榜也运行了真实 Redis 测试，视频运行了真实 ffmpeg 测试。限流器保留 1 条 Starlette/httpx 弃用警告。

测试在本地 `.style-review-venv` 中运行，各章节使用独立 Python 进程、导入路径和临时目录。详细日志保存在本地 `scale-from-zero/.runtime/review-final/` 和 `review-redis/`；这些运行产物不提交。下表是本轮验证记录，不代表生产负载或所有外部故障场景均已覆盖。

### 逐项目记录

所有项目均完成源码可读性整理和全套现有测试。下表列出额外的主要处理。

| 项目 | 主要处理 | 通过数 |
| --- | --- | ---: |
| ad-click-aggregation-system | HTTP 请求头超时；SQLite 初始化失败释放连接 | 11 |
| capacity-estimation | 整理计算模型和 CLI 排版，保留 Decimal 精度与纯计算边界 | 9 |
| chat-system | Redis Lua 脚本独立成模块；修复集成测试变量遮蔽；真实 Redis 回归 | 57 |
| consistent-hashing | 不可变拓扑快照；串行写入、短锁发布；哈希冲突及失败原子性 | 86 |
| digital-wallet-system | HTTP 超时；数据库异常释放；实验进程退出后临时文件清理重试 | 14 |
| distributed-email-service | HTTP 超时；数据库异常释放；清理无用导入 | 9 |
| distributed-message-queue | HTTP 请求头超时；整理 broker、日志、客户端职责中的代码表达 | 15 |
| file-sync-system | HTTP 请求头超时；整理存储、服务和客户端代码 | 33 |
| gaming-leaderboard-system | HTTP 超时；数据库异常释放；真实 Redis 回归 | 8 |
| hotel-reservation-system | HTTP 超时；数据库异常释放，保留库存和支付事务边界 | 13 |
| key-value-store | hinted handoff 按目标串行交付；保留 await 期间新增的 hint | 367 |
| maps-system | 缓存加载移出元数据锁；HTTP 超时；缓存容量校验 | 14 |
| metrics-monitoring-system | 缓存加载移出锁；HTTP 超时；数据库异常释放 | 15 |
| nearby-friends-system | 整理位置、后端和 WebSocket 代码；真实 Redis/WebSocket 回归 | 12 |
| news-feed-system | 修复停止标记残留导致 worker pool 重启后退出 | 175 |
| notification-system | 校验 worker 数；拒绝重复启动；属性测试独立管理事件循环 | 428 |
| object-storage-system | HTTP 超时；数据库异常释放；清理无用导入 | 10 |
| payment-system | HTTP 超时；数据库异常释放，保留幂等和事务保证 | 13 |
| proximity-service | HTTP 请求头超时；整理地理计算和存储代码 | 17 |
| rate-limiter | 修复算法后端分派、共享存储、重入锁、单调 TTL、最长重试延迟及中间件生命周期 | 191 |
| scale-from-zero | HTTP 超时；数据库异常释放；实验临时文件清理重试 | 11 |
| search-autocomplete-system | HTTP 请求头超时；整理 trie、服务和存储代码 | 30 |
| stock-exchange-system | HTTP 超时；数据库异常释放，保留撮合顺序和持久化语义 | 13 |
| unique-id-generator | 单条和批量生成复用同一状态机；验证时间范围后才发布状态 | 79 |
| url-shortener | 共享内存后端原子 get-or-create；双向映射和点击计数加锁 | 105 |
| video-streaming-system | 拆分签名授权模块；HTTP 超时；授权及真实转码回归 | 35 |
| web-crawler | 限制已创建任务数；保留任务异常；取消时回收所有自有任务 | 151 |

### 关键设计与回归证据

- **资源边界**：16 个 HTTP 适配器在读取请求头之前设置 socket 超时，防止不完整请求头长期占用线程。跨项目测试发送真实的半个请求头，验证连接关闭。10 个数据库适配器注入 PRAGMA 失败，验证连接被关闭。2 个缓存用事件控制慢加载，验证独立命中仍可完成，共 28 项。
- **并发原子性**：限流器验证 100 个并发请求严格只允许 7 个；一致性哈希验证暂停写入时旧快照仍可读、冲突节点不会被覆盖；短链接验证多个客户端共享后端时同 URL 去重；KV 验证交付过程中新增 hint 不丢失。
- **生命周期**：新闻 worker pool 验证停止后重新启动；通知系统禁止重复启动遗留旧 worker；爬虫限制任务创建而非仅在任务内加 semaphore，并在失败和取消时等待清理。
- **复用与职责**：限流 Flask/FastAPI 共享配置构造，存储通过明确接口分派原子算法；Snowflake 共用生成状态机；聊天 Lua 脚本与 Python 调用分离；视频授权签名与业务编排分离。
- **异步响应性**：FastAPI 将同步存储检查移入线程池；地图、指标缓存的慢加载不持有缓存元数据锁。同键同时缺失允许重复加载，发布时复查，以首个已发布结果为准；这不是 single-flight。
- **安装边界**：未引入跨章节运行时公共包。类似 SQLite 辅助类的事务、异常与持久化要求不同；统一验证契约比强行共用实现更稳妥。

### Concurrency and performance boundaries

A sequential hash-ring microbenchmark compared HEAD before this change with
this implementation: 20 physical nodes, 150 virtual nodes each, 5 repetitions,
median wall time. 20,000 lookups took 35.36 ms before and 45.78 ms after; 20
add/remove pairs took 59.07 ms before and 22.59 ms after. This is a local
microbenchmark, not service capacity. Safer snapshot reads add synchronization
cost; this change does not claim every operation became faster.

The concurrency regression pauses a writer inside a hash function and verifies
that a reader can complete against the prior snapshot before the writer resumes.
Additional stress tests run 4 readers alongside topology churn. Each lookup sees
one topology; separate API calls need not see the same topology. Custom hash
functions must be deterministic and thread safe. Snapshot replacement copies
state and is intended for read-heavy, relatively infrequent topology changes.

MemoryStorage still serializes atomic callbacks, providing mutual exclusion,
not transactional rollback. Callbacks must be short and avoid network I/O or
waiting for another storage operation. Python threads do not imply parallel
CPU execution. FastAPI uses Starlette's shared worker pool, whose capacity and
backend timeouts still bound concurrency. Flask's default storage is shared
within a route/process; use a distributed backend for multiple processes.

### 尚未验证或后续可深化的范围

- 未执行生产流量压测，也未证明全仓库 QPS 提升。保留金融、撮合与持久化的必要串行边界。
- PostgreSQL 与 Sentinel 集成需要上述显式环境；限流器本轮主要使用内存和 fakeredis，并非多进程真实 Redis 容量验证。
- 内存 TTL 长期回收、锁等待分布、各服务峰值容量及长期故障恢复仍需针对工作负载测量。HTTP 读取超时不是全局连接数或排队上限。
- 自定义短链接 StorageBackend 的默认 get_or_create 兼容实现只保证顺序调用；并发后端必须实现自己的原子覆盖。内存后端已实现。
- 大型服务仍可继续按稳定业务边界拆分；本轮只抽取有回归覆盖的内聚模块，不以行数作为重写依据。

### 复现

参见 [CONTRIBUTING.md](CONTRIBUTING.md) 的依赖、隔离运行、Redis 前置条件和静态检查说明。源码 AST 审计只输出大小、重复与异常处理信号，不把每个信号当成缺陷，也不能代替测试。
