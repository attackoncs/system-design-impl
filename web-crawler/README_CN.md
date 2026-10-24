# 网页爬虫

对应《System Design Interview.md》第 10 章。使用 Python 标准库与 asyncio，实现优先级调度、单主机礼貌访问、robots 检查、HTML/链接解析及 URL/内容去重。

```python
import asyncio
from web_crawler import Crawler, CrawlerConfig

async def main():
    crawler = Crawler(CrawlerConfig(
        state_path="crawl.db", max_pages=100, max_concurrent=8,
        per_host_delay=1, recrawl_interval_seconds=3600,
        max_response_bytes=2 * 1024 * 1024,
    ))
    results = await crawler.crawl(["https://example.com/"])
    print([(r.url, r.status.value) for r in results])

asyncio.run(main())
```

配置 state_path 后，队列、去重指纹和 HTML 快照持久化；重启恢复在途任务，失败有界退避重试。一个文件只允许一个活动调度器。每次 crawl 是有限批次，max_pages 限制尝试次数，调用返回当前批次结果；外部调度器重复调用 crawl([]) 激活到期重爬。页面未变化会降低重爬频率，变化后提高优先级。未配置 state_path 时保持内存运行。

默认 DNS 缓存 300 秒、最多 1024 项，HTTP/TLS 保留原域名；下载默认最多 10 MiB，超时和取消会关闭请求 socket。所有重定向重新经过 URL 过滤、robots 和礼貌调度。同主机页面任务不重叠，识别 Crawl-delay。

分布式路由通过 ConsistentHashPartition 与 route_url(owner, entry) 注入。回调必须在目标持久接收后返回，并支持幂等重投；每个节点使用独立数据库。成员变更需外部协调安全移交，不自动发现或迁移节点。

完整回归 **213 项通过**。本实现未证明每月十亿页面的容量；状态保留、复制/分片、集群控制面及动态渲染仍需独立部署。详情见[章节核对](docs/chapter10-review.md)、[分片契约](docs/partition-review.md)和[下载器边界](docs/transport-review.md)。
