# 新闻动态系统

<!-- LEARNING_GUIDE_START -->
## 从这里开始读代码

[跟着一个案例读懂系统：架构、状态变化与关键代码详解](docs/learning-guide_cn.md)。
<!-- LEARNING_GUIDE_END -->

对应《System Design Interview》第12章，提供可运行的发布、读取与倒序动态参考实现。需求及验收见[需求文档](docs/requirements_cn.md)，进度见[任务清单](docs/tasks_cn.md)，逐项覆盖见[章节核对](docs/chapter12-review.md)，架构见[设计文档](docs/design_cn.md)。

## 已实现

- 鉴权的 POST/GET `/v1/me/feed`，组装作者、正文、媒体地址、互动状态和计数，支持稳定游标分页。
- 普通作者推送到有界 ID 缓存，明星作者读取时拉取；冷缓存、历史分页和过期窗口按作者索引回源。
- SQLite 保存帖子、作者资料、关注和静音关系；帖子与待分发标记同事务接受，支持恢复重放和缓存淘汰回源。
- 五组缓存及线程同步；内容/资料 I/O 和作者查询复用固定读取协程，保序组装，失败或取消时排空所属任务；有界条带锁合并同作者的资料缓存未命中。
- 等待、延迟重试、执行中任务共同占用队列容量；有限重试、确认凭据校验、死信缓冲和取消归还。
- 仅依赖标准库运行；HTTP 示例限制请求并发、正文大小和超时。
- 线程安全的本地请求 QPS、结果、执行中数量及耗时快照，不保留用户标签或逐次请求样本。

## 安装与库示例

```sh
pip install -e ".[dev]"
```

```python
import asyncio
from news_feed_system import FeedRequest, NewsFeedAPI, UserProfile

async def main():
    api = NewsFeedAPI(database_path="feed.sqlite3")
    api.auth.register_token("author-token", "alice")
    api.auth.register_token("reader-token", "bob")
    await asyncio.to_thread(api.users.put, UserProfile("alice", "Alice"))
    await api.graph.add_follow("bob", "alice")
    async with api:
        await api.publish(FeedRequest("alice", "author-token", "你好"))
        # 演示中可主动等待后台任务完成。
        await api.publisher.recover_pending()
        await api.queue.join()
        feed = await api.retrieve("reader-token", page_size=20)
        print([entry.post.content for entry in feed.entries])

asyncio.run(main())
```

一个 API 实例使用同一个事件循环。发布成功代表帖子及待分发记录已接受，推送缓存最终一致；默认每30秒修复窗口，游标翻页使用存储索引，单页最多100条；每次读取的各阶段最多16个固定协程，分别处理内容、作者查询或资料组装。通过 `NewsFeedConfig` 配置。`await api.mute(user_id, author_id)` / `unmute` 在 SQLite 模式保存偏好。

不指定 `database_path` 时，源数据保存在内存，进程退出后丢失；缓存淘汰不删除源帖子，因此源历史没有缓存容量限制。

## HTTP 示例

在项目目录的 PowerShell 中运行：

```powershell
$env:NEWS_FEED_TOKEN = 'local-author-token'
$env:NEWS_FEED_VIEWER_TOKEN = 'local-reader-token'
python examples/demo_server.py --database feed.sqlite3 --port 8000
```

客户端使用 `Authorization: Bearer <token>`。POST JSON 包含 `content`、可选 `media_urls` 和 `selective_sharing`；GET 可传 `page_size` 和 `cursor`。发送者身份来自令牌，正文中的 `user_id` 不用于冒用身份。示例会加载作者、读者资料和关注关系；两个令牌必须不同。

媒体以 HTTP(S) 地址返回，由客户端加载。媒体上传、源站和 CDN 需要外部集成；通知模块只保存本地记录，没有发送真实第三方推送。

## 本地请求观测

`api.metrics()` 为 `publish` 和 `retrieve` 返回独立、可编码为 JSON 的快照：

```python
observations = api.metrics()
print(observations["retrieve"]["qps_last_60s"])
print(observations["retrieve"]["latency_seconds"]["mean"])
```

每类操作包含累计请求数、成功/失败/取消数、执行中数量，以及已结束操作的耗时 count/total/mean/max，单位为秒。近期 QPS 使用60个固定的一秒时间桶，将当前及前59桶的开始数除以60，启动初期也使用这一分母，因此是分桶近似值。每类操作占用固定存储，不记录令牌、用户、正文或逐次延迟样本，监控线程可安全采集。

发布成功只衡量源接受，不代表分发完成或推送送达；HTTP 层尚未调用 API 就拒绝的请求不计入。导出器、延迟分布、跨实例聚合、仪表盘及告警仍需集成。

## 可靠性与部署边界

SQLite 是本地源存储。待分发标记在交给本地扇出/通知阶段后清除，不是对持久 worker 完成的确认；此后进程退出可能丢失主动缓存更新，冷读取和定期回源会重建内容。投递采用至少一次语义，Feed ID 及仍保留的本地通知记录去重。worker 默认重试3次，再进入最多100条的诊断死信缓冲。`queue.join()` 等待队列任务，不包含尚未分发的源记录。

令牌、限流窗口、互动计数和通知记录仍在进程内。跨实例写入静音设置需刷新快照，社交缓存需通过本实例修改或等待 TTL。共享缓存/消息队列、分片、复制、负载均衡及多机房均需部署适配，未验证千万日活容量。

## 验证

```sh
python -m pip install -e ".[dev]"
python -m pytest tests -q -p no:cacheprovider
```

2026-10-29 完整回归 **282项通过**，以警告作为错误运行，覆盖真实子进程退出恢复、SQLite 并发、隐私变更、有界内容/资料读取重叠、取消排空、请求指标、worker 重试和本地 HTTP。延迟队列改用 `asyncio.TimeoutError`，适配声明支持的 Python 3.9/3.10；回归模拟其独立异常层次。本次实际执行 Python 3.13，未跑完整版本矩阵。

若直接使用尚未安装的源码，将 `PYTHONPATH` 设为本项目 `src` 的绝对路径，使子进程也能导入。受限 Windows 环境还需可写的 `TEMP`/`TMP` 及本机回环连接权限。
