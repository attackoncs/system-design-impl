# 第5章：章节覆盖与开源代码对照

复核日期：2026-10-31。依据仓库内《System Design Interview.md》第5章
（923–1507行），先阅读原文，再通过 CodeGraph 追踪核心、算法、存储和
接入层，并查询、阅读下面的 GitHub 仓库及相关源码。

## 开源参考与取舍

| 开源实现 | 已查看的入口 | 对本项目的参考 |
|---|---|---|
| [uber-go/ratelimit](https://github.com/uber-go/ratelimit) | [原子时间调度](https://github.com/uber-go/ratelimit/blob/main/limiter_atomic_int64.go) | `Take()` 通过等待控制执行节奏。对照区分本项目立即返回的准入算法与独立 FIFO 执行队列；没有照搬 Go 的 CAS 到 Python。 |
| [envoyproxy/ratelimit](https://github.com/envoyproxy/ratelimit)（书中 Lyft 项目的现有地址） | [配置、Redis、统计说明](https://github.com/envoyproxy/ratelimit/blob/main/README.md) | 对照规则匹配、配置更新、连接上限和按规则统计。保留本项目较小的库与中间件结构，未引入 gRPC、网关或其整套配置协议。 |
| [go-redis/redis_rate](https://github.com/go-redis/redis_rate) | [Lua 实现](https://github.com/go-redis/redis_rate/blob/v10/lua.go) | 对照服务端 TIME、原子判断与扣额、重试与重置时间分离，以及脚本对象复用。其 GCRA 不是本章五种算法的逐项实现，不新增第六种算法。 |
| [redis/redis-py](https://github.com/redis/redis-py) | [6.4.0 的 Script 实现](https://github.com/redis/redis-py/blob/v6.4.0/redis/commands/core.py#L5208) | 使用已有依赖的 `register_script()`：优先 EVALSHA，只有 NOSCRIPT 时加载脚本再执行；连接中断和超时不重放扣额。 |

这里参考其接口和实现思路，新增代码使用现有 Python 依赖，没有复制第三方
算法源码。外部仓库未在本轮安装运行，也未作完整质量或生产容量评估。
分支链接随上游变化，以上描述对应本次查阅内容。

脚本缓存的服务端约定也对照了[Redis Lua官方说明](https://redis.io/docs/latest/develop/programmability/eval-intro/)。

## 原书内容是否实现

| 原书主题 | 本地入口与结论 |
|---|---|
| 服务端或中间件位置 | `core.py`、`decorators.py`、两个 `middleware` 适配器均已实现；独立网关为部署选择。 |
| 令牌桶 | `algorithms/token_bucket.py`：独立容量、持续补充、原子扣额和重试时间。 |
| 漏桶 | `algorithms/leaking_bucket.py` 负责虚拟占用准入；`queue.py` 提供有界 FIFO 匀速启动，二者职责明确。 |
| 固定窗口 | `algorithms/fixed_window.py` 已实现，保留原书说明的边界突发特性。 |
| 滑动日志 | `algorithms/sliding_window_log.py` 已实现精确的接纳量限制；只记录成功请求，区别于原书示例记录拒绝的策略，避免拒绝流量撑大日志。 |
| 滑动计数 | `algorithms/sliding_window_counter.py` 已实现当前与前窗加权；与原书示例一样采用向下取整的准入效果，仍是近似算法。 |
| IP、用户、接口与组合规则 | `config.py`、`keys.py`、`core.py`：支持选择器、路径筛选、规则隔离和多规则检查；不负责认证。 |
| 磁盘规则与 worker 更新 | `rule_files.py`：有界文件、完整校验、原子发布、失败保留旧规则、可停止轮询。 |
| 429 和限流响应头 | `responses.py` 供 Flask/FastAPI/装饰器复用；包含书中的 `X-RateLimit-Retry-After` 及标准 `Retry-After`。 |
| 分布式竞争与状态同步 | `storage/redis.py`：共享单 Redis 主节点，服务端时钟与扣额在一个脚本内执行；无需粘性会话。 |
| 内存与延迟优化 | 内存键数与清理预算、Redis 连接与超时已有上限；本轮减少重复发送 Lua 脚本文本，未宣称吞吐提升比例。 |
| 监控 | 已有固定维度的允许、拒绝、错误、检查次数，以及热更新状态；尚无逐规则时序指标、采集器、仪表盘和告警。 |
| 多地域、最终一致同步等讨论 | 尚未实现边缘路由、跨地域额度同步或全球硬配额；共享 Redis 不等于具备这些能力。 |
| 软硬限流、其他网络层、客户端退避 | 属于原书延伸讨论；桶容量提供突发额度，未实现通用软限流模式、L3 限流或客户端重试 SDK。 |

章节的主要算法和单 Redis 共享状态链路已有实现；上表的部署与观测扩展仍有
明确边界，不能据此宣称整章讨论的所有生产能力均已具备。

## 本轮修正

1. `check_request("...")` 原先直接返回字符串键，绕过了其他入口的
   1–8192字符限制。现在在适用规则解析主体时先校验，无效键不会扣额；
   没有匹配规则时仍直接放行。
2. 64 KiB 内的过深 JSON 也可能触发 `RecursionError`，逃出轮询器的错误
   处理。解析层现将其转为 `ValueError`，保留旧规则并记录错误；后续合法
   更新仍可发布。其他解析错误继续沿用原有处理。
3. RedisStorage 复用 redis-py 的脚本对象，每实例最多缓存16个脚本。
   稳态每条规则只发一次 EVALSHA；首次使用或服务端缓存丢失时，会额外发送
   SCRIPT LOAD 并重发 EVALSHA。NOSCRIPT 代表脚本未执行，可以安全恢复；
   超时可能已扣额，仍按 fail-open/closed 处理，不重试。

状态键和算法语义不变，无需重置配额。Redis ACL 需允许 EVALSHA 和 SCRIPT
LOAD，以及脚本内部使用的命令；仅允许 EVAL 的旧配置需要相应调整。
关闭存储前仍应先排空请求，`close()` 同时清理客户端脚本缓存与连接池。

## 验证与阅读路线

基线249项通过；修改后265项通过，无跳过。新增16项在
[test_open_source_review.py](../tests/test_open_source_review.py)，覆盖非法键、
深层 JSON 后恢复、稳态仅发摘要、五算法的缓存丢失与冷缓存并发，以及两种
失败策略下超时只执行一次。全量测试将警告视为错误，仅在 pytest 参数中
过滤既有 Starlette TestClient 对可选 httpx2 的兼容提示。

CodeGraph已复查修改后的存储与解析入口。code-review-graph增量更新5个
文件、70个节点、501条边，无构建错误；三处源码变动的影响分析得到40个
直接节点、76个影响节点和19个附加文件。结果省略部分边，并有2790个
未解析调用点，不能把图结果当作完整调用证明。新增未跟踪测试另由实际
执行和学习导读的AST定义校验核对，未为构图暂存文件。

真实 Redis 的多进程和 HTTP 验证及脚本缓存恢复结果见
[实测报告](../benchmarks/REPORT.md)。fakeredis 的并发结果属于单元证据，
不能代替跨进程验证。既有吞吐回归数据仍保留，未用本轮功能检查替代压测。

建议先读[案例导读](learning-guide_cn.md)，沿 `check_request` → 算法 `check`
→ `RedisStorage.execute_algorithm` → `execute_atomic` 追踪，再查看 redis-py
的 `Script.__call__`，最后对照上述恢复与并发测试。

[2026-10-27复审记录](chapter05-review.md) · [设计](design_cn.md) ·
[需求](requirements_cn.md) · [任务](tasks_cn.md)
