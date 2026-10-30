# 第12章实现与验证任务

更新于2026-10-29。[英文任务清单](tasks.md)使用相同任务编号。任务对应[需求](requirements_cn.md)，本轮最终验证记录在[章节核对](chapter12-review.md)。已勾选条目表示本轮已检查的本地实现，不代表部署验收，也不代替最终回归。

## 已实现的本地参考方案

| 任务 | 状态 | 工作与需求关联 | 验收证据 |
| --- | --- | --- | --- |
| TASK-12-01 | [x] | 组合 `NewsFeedAPI`，分离发布/读取，提供鉴权的 POST/GET `/v1/me/feed`（REQ-12-01、REQ-12-14、REQ-12-15）。 | `api.py`、`http.py`；`test_http.py`、`test_api_lifecycle.py`、`test_chapter12.py`。 |
| TASK-12-02 | [x] | 校验并复制已接受帖子，从令牌确定身份，在本地原子更新限流窗口（REQ-12-02、REQ-12-03）。 | `auth.py`、`rate_limiter.py`、`post_service.py`；`test_post_service.py`、`test_chapter12.py`、`test_http.py`。 |
| TASK-12-03 | [x] | 提供内存/SQLite 帖子和资料适配器、不可变帖子身份及帖子/outbox 原子接受；重启后公平重放待分发任务（REQ-12-03、REQ-12-04）。 | `storage.py`、`post_cache.py`、`post_service.py`；`test_chapter12.py`、`test_cache_reliability.py`。 |
| TASK-12-04 | [x] | 提供图抽象、SQLite 关注索引、关系/计数缓存，以及并发加载的失效栅栏（REQ-12-05、REQ-12-15）。 | `social_graph.py`、`sqlite_graph.py`、`social_graph_cache.py`；`test_social_graph.py`、`test_social_graph_cache.py`、`test_cache_reliability.py`。 |
| TASK-12-05 | [x] | 按关注者阈值选择推/拉、过滤接收者并分批入队，明星帖子可通过源索引读取（REQ-12-06）。 | `fanout_service.py`、`news_feed_service.py`；`test_fanout_service.py`、`test_chapter12.py`。 |
| TASK-12-06 | [x] | 推送/读取/通知复用静音与分享策略，SQLite 保存静音偏好，异步加载后复核可见性（REQ-12-07）。 | `policy.py`、`fanout_service.py`、`news_feed_service.py`、`notification_service.py`；`test_mute_policy.py`、`test_notification_reliability.py`、`test_chapter12.py`。 |
| TASK-12-07 | [x] | 待处理/延迟/已领取状态共同预留容量，校验确认凭据，实现有界重试/死信及取消归还，并兼容支持的 Python 超时异常语义（REQ-12-08）。 | `queue.py`、`worker.py`；`test_queue.py`、`test_worker.py`、`test_worker_reliability.py`、`test_chapter12_queue_audit.py`。 |
| TASK-12-08 | [x] | 维护有界、排序、去重的每读者 ID 缓存，使部分成功后的 worker 重试安全（REQ-12-08、REQ-12-09）。 | `news_feed_cache.py`；`test_news_feed_cache.py`、`test_worker_reliability.py`、`test_chapter12.py`。 |
| TASK-12-09 | [x] | 合并推拉候选，实现校验后的时间/ID keyset 分页，缓存 ID 加载、分类、作者拉取复用固定数量 worker，并行组装仍保持输出顺序（REQ-12-10、REQ-12-15）。 | `news_feed_service.py`；`test_chapter12.py`、`test_http.py`、`test_chapter12_read_audit.py`。 |
| TASK-12-10 | [x] | 冷/到期/变更/历史窗口从源修复，继续扫描隐藏内容后的批次，保持不滑动的修复期限（REQ-12-11）。 | `news_feed_service.py`、`storage.py`；`test_chapter12.py`、`test_cache_reliability.py`。 |
| TASK-12-11 | [x] | 五组缓存接入读取组装，内容/资料未命中回源，隔离可变返回值；同作者资料缺失经有界条带锁合并回源（REQ-12-12）。 | 内容、社交、ID、互动、计数和资料缓存模块；`test_action_cache.py`、`test_counter_cache.py`、`test_user_cache.py`、`test_cache_reliability.py`、`test_chapter12.py`、`test_chapter12_read_audit.py`。 |
| TASK-12-12 | [x] | 用固定接收者验证 worker 记录每接收者有界、去重的新内容通知（REQ-12-13）。 | `notification_service.py`；`test_notification_service.py`、`test_notification_reliability.py`。这是本地模拟，不是第三方推送。 |
| TASK-12-13 | [x] | 拥有一个事件循环和共享启动/关闭，排空已接纳发布并恢复取消的领取；限制 HTTP 并发/大小/期限，阻塞存储移出事件循环；通过 `api.metrics()` 观测发布/读取结果、接受/读取耗时及分桶近期 QPS（REQ-12-14、REQ-12-15、REQ-12-16）。 | `api.py`、`concurrency.py`、`http.py`、`worker.py`、`metrics.py`；`test_api_lifecycle.py`、`test_worker_reliability.py`、`test_http.py`、`test_chapter12.py`、`test_chapter12_metrics.py`。 |

## 审查与文档关卡

| 任务 | 状态 | 工作 | 完成条件 |
| --- | --- | --- | --- |
| TASK-12-14 | [x] | 重读第12章，用 CodeGraph/code-review-graph 追踪发布、扇出、读取、缓存和测试路径。 | 需求矩阵有源码依据；图结果用于导航，不能证明动态调用或测试完整，详见章节核对。 |
| TASK-12-15 | [x] | 补齐中英文 `requirements`、`tasks`，使用稳定编号、验收映射并明确本地/部署边界。 | 四份文档存在、交叉链接可达、两种语言编号一致；由协调审查同步 README/设计/核对记录。 |
| TASK-12-16 | [x] | 完成本轮最终回归、静态检查和文档检查。 | 2026-10-29：282项通过，无失败/跳过，以警告作为错误运行；源码/示例/新增测试通过格式与Python3.9语法检查，全章Ruff E9/F和文档链接通过。运行时及本机回环限制见章节核对。 |

## 部署与运维待办

以下明确保留未完成状态，不能因本地适配器拥有类似接口就勾选。

| 任务 | 状态 | 关联需求 | 验收条件 |
| --- | --- | --- | --- |
| TASK-12-17 | [ ] | 生产监控集成（REQ-12-16）。 | 将本地请求快照和队列诊断接入导出器、延迟分布、聚合、仪表盘及告警，补队列年龄观测并验证告警条件；区分服务指标与点赞/回复。本地请求快照本身已由 TASK-12-13 完成。 |
| TASK-12-18 | [ ] | 媒体/CDN 和真实通知供应商（REQ-12-17）。 | 配置上传/源站/CDN 及供应商适配器，验证自有集成端点、重试、幂等/回执及恢复行为。 |
| TASK-12-19 | [ ] | 共享缓存、持久消息代理和全局配额（REQ-12-18）。 | 定义跨实例契约、持久任务确认/租约、全局容量和配额语义，测试分发至确认之间进程退出的恢复。 |
| TASK-12-20 | [ ] | 分片、复制/只读副本和多机房（REQ-12-18）。 | 定义路由/迁移、一致性和恢复目标，再验证故障切换与故障下的旧读取行为。 |
| TASK-12-21 | [ ] | 容量验证（REQ-12-19）。 | 公开可复现负载和环境、错误数及延迟分位数，再声明达到书中规模或生产 SLO。 |

## 运行验收检查

在 `news-feed-system` 目录运行 `python -m pytest tests -q -p no:cacheprovider`。受限 Windows 环境可将 `TEMP`/`TMP` 指向仓库 `.test-tmp` 下已存在的可写目录。本章测试与其他章节隔离运行，不发送外部推送。

先以 `python -m pip install -e ".[dev]"` 安装本项目；若直接使用未安装源码，则将 `PYTHONPATH` 设为本项目 `src` 的绝对路径，让子进程恢复测试也能导入。Windows事件循环和HTTP测试需要本机回环连接。

源码检查遵循根目录[贡献指南](../../CONTRIBUTING.md)。将最终结果写入[chapter12-review.md](chapter12-review.md)，再同步更新两种语言的 TASK-12-16。本地队列测试通过不等于持久代理任务完成；`queue.join()` 不覆盖尚未分发的 outbox 记录。
