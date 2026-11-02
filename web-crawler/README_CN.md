# 网页爬虫

<!-- LEARNING_GUIDE_START -->
## 从这里开始读代码

[跟着一个案例读懂系统：架构、状态变化与关键代码详解](docs/learning-guide_cn.md)。
<!-- LEARNING_GUIDE_END -->

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

默认 DNS 缓存 300 秒、最多 1024 项，HTTP/TLS 保留原域名；下载默认最多 10 MiB，超时和取消会关闭请求 socket。所有重定向重新经过 URL 过滤、robots 和礼貌调度。同主机页面任务不重叠，识别 Crawl-delay。robots支持书中通配符、结束锚点和最长匹配，区分字面百分号转义；保留标准库代理分组及网络异常放行政策，不宣称完整RFC实现。

分布式路由通过 ConsistentHashPartition 与 route_url(owner, entry) 注入。回调必须在目标持久接收后返回，并支持幂等重投；每个节点使用独立数据库。成员变更需外部协调安全移交，不自动发现或迁移节点。

完整回归 **256 项通过**。本实现未证明每月十亿页面的容量；状态保留、复制/分片、集群控制面及动态渲染仍需独立部署。详情见[原书/开源对照](docs/open-source-review_cn.md)、[章节核对](docs/chapter10-review.md)、[分片契约](docs/partition-review.md)和[下载器边界](docs/transport-review.md)。

## 本轮调度与解析改进

内存队列只发布可用主机队首，活跃主机积压不再被反复扫描。一个下载很慢时，有空闲并发槽和剩余批次预算便每50毫秒检查其他已到期主机；满载时等待任务完成。同主机仍严格保留，礼貌间隔从完成开始。

首个`<base href>`控制相对链接；坏链接被逐条跳过，不影响同页其他链接。URL规范化只小写scheme/host，保留用户名/密码大小写；格式错误主机与端口在入队前被拒绝。

## 安装、复验与本地演示

```bash
pip install -e ".[dev]"
python -W error -m pytest tests -q
python examples/basic_crawl.py
python tools/benchmark.py --output benchmarks/local.json
python -W error tools/verify_http.py --output benchmarks/http-local.json
```

示例使用模拟页面，HTTP验证使用自有回环服务；不会因运行验证而抓取公网。[测量说明与原始数据](benchmarks/REPORT.md)区分内存调度耗时和真实HTTP正确性。首次使用先读[学习路线](docs/learning-guide_cn.md)，再对照[需求](docs/requirements_cn.md)、[设计](docs/design_cn.md)和[已验收任务](docs/tasks_cn.md)。
