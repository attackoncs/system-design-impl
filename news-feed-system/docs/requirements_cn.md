# 第12章需求：新闻动态系统

依据仓库《System Design Interview.md》第12章“Design A News Feed System”核对，更新于2026-10-29。本文约定可执行的本地参考实现，并将其与书中的部署讨论分开。[英文版本](requirements.md)使用相同需求编号；[任务清单](tasks_cn.md)、[设计](design_cn.md)和[章节核对](chapter12-review.md)提供实现及验证背景。

## 范围与状态

主要流程为发布和按时间倒序读取。Web/移动客户端使用 HTTP 适配器；库组合可替换的存储/鉴权适配器、本地缓存和异步扇出 worker。社交关系使用有向关注表示；双向关注可以表示朋友关系。

书中的5000位朋友、1000万日活是设计假设，不是实测容量。默认读取范围限制为关注5000位作者；SQLite 关注图在写入时也执行该配置上限。内存或自定义社交图不等于分布式图数据库。下文“本地已实现”均不表示已部署生产服务、复制系统或外部供应商。

## 功能需求

| 编号 | 需求与验收条件 | 状态与证据 |
| --- | --- | --- |
| REQ-12-01 | 提供 POST 和 GET `/v1/me/feed`。POST 接受 JSON `content`、可选 `media_urls`、`selective_sharing`，返回已接受的 `post_id`；GET 返回组装后的条目、`has_more` 和 `next_cursor`。两者均需鉴权。客户端、身份、限流、大小和超时错误具有明确响应，内部异常细节不直接暴露。 | 本地已实现：`api.py`、`http.py`；`test_http.py`、`test_chapter12.py`。 |
| REQ-12-02 | 由已验证令牌确定作者/读者，正文中的 `user_id` 不能用于冒充其他作者。每作者滑动窗口发布配额在本进程内原子执行。优先使用 Bearer；查询令牌兼容可配置，适配器不记录可能包含令牌的请求 URL。 | 本地已实现：`auth.py`、`rate_limiter.py`、`http.py`；`test_post_service.py`、`test_chapter12.py`、`test_http.py`。全局配额及生产身份服务仍需集成。 |
| REQ-12-03 | 校验非空文本、受限集合大小和 HTTP(S) 媒体地址。接受前复制可变请求数据，调用方修改列表不能改变已接受帖子。帖子 ID 是不可变源身份，不同内容不能覆盖已有 ID。 | 本地已实现：`post_service.py`、`models.py`、`storage.py`、`post_cache.py`；`test_post_service.py`、`test_chapter12.py`、`test_cache_reliability.py`。媒体上传/转码不在此契约内。 |
| REQ-12-04 | 在同一原子操作中接受源帖子及待分发 outbox 标记。SQLite 模式在重启后恢复已提交帖子和待分发记录；重放失败任务时，早期失败批次不得持续饿死后续任务。接受发布先于异步扇出完成。 | 本地已实现：`storage.py`、`post_service.py`、`api.py`；`test_chapter12.py`、`test_api_lifecycle.py`。清除 outbox 表示本地分发完成，**不表示持久 worker 确认完成**。 |
| REQ-12-05 | 在 `SocialGraph` 接口后管理关注/被关注关系，支持缓存查询并执行读取范围上限。本地社交关系变更使相关缓存失效；失效前已开始的旧加载不能在失效后回填旧关系。 | 本地已实现：`social_graph.py`、`sqlite_graph.py`、`social_graph_cache.py`；`test_social_graph.py`、`test_social_graph_cache.py`、`test_cache_reliability.py`、`test_chapter12.py`。外部写入依赖刷新/TTL，不提供全局即时一致性。 |
| REQ-12-06 | 使用混合扇出。普通作者通过队列分批推送帖子 ID 给合格关注者；关注者数达到或超过明星阈值的作者跳过推送，在读取时按源索引拉取。分类/关注变化需触发源修复，不能静默隐藏较早内容。 | 本地已实现：`fanout_service.py`、`news_feed_service.py`；`test_fanout_service.py`、`test_chapter12.py`。 |
| REQ-12-07 | 扇出、读取和本地通知使用统一静音/定向分享策略。分享名单为 `None` 表示所有合格关注者，空列表表示无接收者。异步加载后、返回条目或记录通知前，重新检查当前关系和可见性。SQLite 静音偏好重开可恢复。 | 本地已实现：`policy.py`、`fanout_service.py`、`news_feed_service.py`、`notification_service.py`；`test_mute_policy.py`、`test_notification_reliability.py`、`test_chapter12.py`。 |
| REQ-12-08 | 队列容量共同计算待处理、延迟重试和已领取任务。领取凭据拒绝旧确认/重试；重试次数和退避、诊断死信均有界；取消时归还拥有的领取。部分扇出成功后的重试不能重复 Feed ID，也不能使其他 worker 停止。延迟任务领取须兼容支持的 Python 版本，包括 Python 3.9/3.10 的超时异常语义。 | 本地已实现：`queue.py`、`worker.py`、`news_feed_cache.py`；`test_queue.py`、`test_worker.py`、`test_worker_reliability.py`、`test_chapter12_queue_audit.py`。队列、领取及死信在内存中。 |
| REQ-12-09 | 每位读者保留可配置数量的 Feed ID，按 `(timestamp, post_id)` 倒序并按帖子 ID 去重；保存 ID/作者/时间元数据，不保存组装后的帖子和资料对象。迟到或重复任务不能错误挤掉较新条目。 | 本地已实现：`news_feed_cache.py`；`test_news_feed_cache.py`、`test_worker_reliability.py`、`test_chapter12.py`。每读者容量不等于全局内存上限。 |
| REQ-12-10 | 将普通作者推送和明星作者拉取合并为有界候选页。游标包含时间和帖子 ID，并严格校验，新增帖子不改变旧页边界。只组装可见候选；存在下一条可见内容时返回续读游标。并行组装须保持已排序的输出顺序。页面代表当前数据，不是跨页数据库快照。 | 本地已实现：`news_feed_service.py`；`test_chapter12.py`、`test_http.py`、`test_chapter12_read_audit.py`。 |
| REQ-12-11 | 冷缓存、到期、策略/分类变化及历史窗口从权威的每作者索引修复，不能遇到一批私有帖就停止。普通作者完整热窗口复用推送 ID，频繁刷新不得无限推迟修复。覆盖记录及作者查询并发均有界。 | 本地已实现：`news_feed_service.py`、`storage.py`；`test_chapter12.py`、`test_cache_reliability.py`。修复由读取触发，不是定时预加载。 |
| REQ-12-12 | 接入书中的五组缓存：Feed ID、热/普通内容、关注/被关注关系、用户互动、计数。组装作者名称/头像、正文/媒体、互动状态和计数。内容/资料未命中回源，作者资料不存在时以 ID 占位。缓存 ID 加载及资料组装也使用固定 worker；同作者的并发资料缺失通过有界条带锁合并回源。公开缓存值不能让调用者通过修改返回值改变内部状态。 | 本地已实现：各缓存模块及 `news_feed_service.py`；`test_action_cache.py`、`test_counter_cache.py`、`test_user_cache.py`、`test_cache_reliability.py`、`test_chapter12.py`、`test_chapter12_read_audit.py`。互动状态在本进程内。 |
| REQ-12-13 | 使用每接收者有界且去重的本地记录，通知合格关注者有新内容；接收者验证使用固定数量 worker，不为每位关注者创建任务。 | 已实现本地模拟：`notification_service.py`；`test_notification_service.py`、`test_notification_reliability.py`。真实移动/Web 推送、持久通知恢复和供应商回执尚未实现。 |
| REQ-12-14 | 明确拥有后台任务。API 实例属于一个事件循环，并发启动/关闭共用生命周期同步。关闭拒绝新发布，等待已接纳发布并停止拥有的恢复/worker；取消关闭等待者不能遗弃共享清理。HTTP 请求线程提交到共享循环，不为每个请求创建循环。 | 本地已实现：`api.py`、`concurrency.py`、`http.py`、`worker.py`；`test_api_lifecycle.py`、`test_worker_reliability.py`、`test_http.py`、`test_chapter12.py`。 |
| REQ-12-15 | 领域决策与持久化、HTTP、调度分离；注入存储/鉴权/图适配器，构造时校验不可变配置，阻塞存储不占用事件循环。限制请求并发、正文大小、请求/连接期限和每请求的读取并发。失败/取消须在返回前排空拥有的异步读取及已提交的线程工作。 | 本地已实现：协议/接口、配置、并发及 HTTP 模块；`test_chapter12.py`、`test_http.py`、`test_cache_reliability.py`、`test_chapter12_read_audit.py`。这些限制在本地执行，不是集群准入控制。 |

## 默认值与不变量

| 配置 | 本地默认值／含义 |
| --- | --- |
| 发布配额 | 每作者每3600秒滑动窗口10篇。 |
| 明星阈值 | 5000位关注者；`>=` 时采用拉取。 |
| 扇出 | 4个 worker；每批100位接收者；队列容量10000个任务。队列容量配置为0时明确表示无界模式。 |
| worker 失败 | 首次尝试后最多重试3次；初始延迟0.05秒，退避有上限；诊断死信保留100条。 |
| Feed/读取 | 每读者500个 ID；默认页20、最多100；作者查询并发16；读取修复窗口30秒；覆盖记录最多5000位读者。 |
| HTTP 适配器 | 32个并发请求处理器；正文1 MiB；连接超时10秒；应用请求期限15秒。可通过构造参数修改。 |

`queue.join()` 覆盖已入队工作，包括已领取任务，不包含尚未入队的源 outbox 记录。SQLite 是本地权威源，缓存可从其重建；选择 SQLite 不会使令牌、配额、互动/计数、队列和本地通知自动持久化。若进程在分发后、worker 完成前退出，后续读取可重建动态内容；主动通知不保证持久送达。

## 部署与运维需求

| 编号 | 需求 | 当前边界／后续验收 |
| --- | --- | --- |
| REQ-12-16 | 观测请求吞吐、刷新延迟、队列压力及失败，避免无界指标标签或保留载荷。 | `metrics.py` 和 `NewsFeedAPI.metrics()` 已实现本地请求观测，由 `test_chapter12_metrics.py` 覆盖：发布/读取请求数、执行中/成功/失败/取消数、生命周期耗时 count/total/mean/max 及近期 QPS。队列诊断/日志支持调查；生产导出、延迟分布、仪表盘及告警仍需集成，互动计数不等于服务指标。 |
| REQ-12-17 | 通过 CDN 提供媒体并发送真实推送。 | 已实现地址和本地通知记录。验收需配置媒体上传/源站/CDN、供应商适配、送达/失败处理及自有端点集成测试。 |
| REQ-12-18 | 无状态 Web 节点、共享缓存/消息代理、分片、复制/只读副本、全局配额及多机房。 | 尚未部署。验收需一致性/故障切换契约、分片路由/迁移、持久代理确认和故障恢复测试，不能仅凭 Python 接口或本地 SQLite 判定完成。 |
| REQ-12-19 | 验证书中1000万日活规模与延迟目标。 | 未测量。验收需说明负载、峰值分布、硬件、持久化设置、错误和延迟分位数。单元测试不能推出生产吞吐/延迟承诺。 |

`api.metrics()` 只观测 API 操作：发布延迟止于源接受；进入 API 之前被 HTTP 层拒绝的请求不计入。耗时聚合以秒为单位，包含已结束的成功、失败和取消。近期 QPS 取当前及前59个一秒时间桶内的开始数除以60，启动初期也使用这一分母；它是分桶估算，不是精确滑动窗口。返回独立快照，可由监控线程采集。只接受固定 `publish`/`retrieve` 标签，不保留令牌、用户 ID、正文或每次延迟样本。

书中 SQL/NoSQL、纵向/横向扩展、复制和一致性模型属于设计权衡，不要求同时部署所有替代方案。[未完成任务](tasks_cn.md#部署与运维待办)列出具体后续工作。

## 验证约定

从 `news-feed-system` 目录单独运行本章测试，避免与其他章节同进程混跑：

```sh
python -m pip install -e ".[dev]"
python -m pytest tests -q -p no:cacheprovider
```

若直接使用未安装的源码，将 `PYTHONPATH` 设为本项目 `src` 的绝对路径，供子进程继承。Windows事件循环和本机HTTP测试需要回环连接权限。

本轮最终结果记录在[章节核对](chapter12-review.md)，不以历史测试条数代替本轮执行结果。测试仅使用本地文件、子进程及本机 HTTP，不发送第三方通知，也不验证分布式部署容量。需求变化时同步更新关联任务、对应英文文档和验收证据。
