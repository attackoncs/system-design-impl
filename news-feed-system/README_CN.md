# 动态消息系统

对应《System Design Interview》第12章，提供可运行的发布、读取与倒序动态参考实现。逐项覆盖见[章节核对](docs/chapter12-review.md)，架构见[设计文档](docs/design_cn.md)。

## 已实现

- 鉴权的 POST/GET `/v1/me/feed`，组装作者、正文、媒体地址、互动状态和计数，支持稳定游标分页。
- 普通作者推送到有界 ID 缓存，明星作者读取时拉取；冷缓存、历史分页和过期窗口按作者索引回源。
- SQLite 保存帖子、作者资料、关注和静音关系；帖子与待分发标记同事务接受，支持恢复重放和缓存淘汰回源。
- 五组缓存及线程同步；异步作者查询、通知验证使用固定数量协程。
- 等待、延迟重试、执行中任务共同占用队列容量；有限重试、确认凭据校验、死信缓冲和取消归还。
- 仅依赖标准库运行；HTTP 示例限制请求并发、正文大小和超时。

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

一个 API 实例使用同一个事件循环。发布成功代表帖子及待分发记录已接受，推送缓存最终一致；默认每30秒修复窗口，游标翻页使用存储索引，单页最多100条、每次请求最多16个作者查询协程。通过 `NewsFeedConfig` 配置。`await api.mute(user_id, author_id)` / `unmute` 在 SQLite 模式保存偏好。

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

## 可靠性与部署边界

SQLite 是本地源存储。待分发标记在交给本地扇出/通知阶段后清除，不是对持久 worker 完成的确认；此后进程退出可能丢失主动缓存更新，冷读取和定期回源会重建内容。投递采用至少一次语义，Feed ID 及仍保留的本地通知记录去重。worker 默认重试3次，再进入最多100条的诊断死信缓冲。`queue.join()` 等待队列任务，不包含尚未分发的源记录。

令牌、限流窗口、互动计数和通知记录仍在进程内。跨实例写入静音设置需刷新快照，社交缓存需通过本实例修改或等待 TTL。共享缓存/消息队列、分片、复制、负载均衡及多机房均需部署适配，未验证千万日活容量。

## 验证

```sh
pytest tests -q
```

2026-10-24 完整回归 **267项通过**，覆盖真实子进程退出恢复、SQLite 并发、隐私变更、有限并发、worker 重试取消和本地 HTTP 流程。
