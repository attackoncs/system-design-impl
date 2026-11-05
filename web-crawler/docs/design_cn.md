# 设计文档：网页爬虫

本章的核心循环是：领取URL并保留主机，检查robots，下载与发现链接，确认结果，再把新URL交回调度器。宏观图与源码入口见[学习路线](learning-guide_cn.md)；[原书/开源对照](open-source-review_cn.md)列出实现范围。以下链接直接指向维护中的源码，替代过期的整段实现副本。

## 组件与状态归属

| 组件 | 保存什么、负责什么 | 源码 |
|---|---|---|
| Crawler | 本批结果/尝试预算、在途任务、内容认领；编排与取消补偿 | [crawler.py](../src/web_crawler/crawler.py) |
| URLFrontier | 内存主机优先堆、可用队首、延迟主机、主机保留 | [frontier.py](../src/web_crawler/frontier.py) |
| SQLiteCrawlState | jobs/hosts/fingerprints/snapshots；独占owner、事务与恢复 | [state.py](../src/web_crawler/state.py) |
| URLLibFetcher / DNSCache | 有界下载槽与时限；TTL/LRU地址缓存 | [fetcher.py](../src/web_crawler/fetcher.py)、[dns.py](../src/web_crawler/dns.py) |
| RobotsChecker | origin缓存、未命中合并、规则与Crawl-delay | [robots.py](../src/web_crawler/robots.py) |
| ContentParser / LinkExtractor | HTML识别、首个base、相对链接、坏链接隔离 | [parser.py](../src/web_crawler/parser.py) |
| URLFilter / URLSeen / ContentSeen | 入队过滤、地址规范化去重、SHA-256内容身份 | [filters.py](../src/web_crawler/filters.py)、[dedup.py](../src/web_crawler/dedup.py) |
| ConsistentHashPartition | 按主机决定owner，显式路由接口 | [partition.py](../src/web_crawler/partition.py) |
| CrawlerConfig / 模型 | 有类型的配置、结果和优先级 | [config.py](../src/web_crawler/config.py)、[models.py](../src/web_crawler/models.py) |

默认零运行时依赖，Python>=3.9。Fetcher可替换，内容处理通过同步/异步Hook扩展。默认内存模式不持久化；state_path启用同一管线的磁盘调度。

## 内存调度：只让可用主机参与选择

每个主机一个按优先级与全局入队序号排序的堆。只有可用主机队首进入就绪堆，礼貌间隔未到的主机进入延迟唤醒堆。后入的高优先级URL可替换已经发布的队首；旧堆项按序号惰性失效。相同优先级FIFO，HIGH优先于MEDIUM再优先于LOW。

`get(reserve=True)`领取一个URL并保留其主机，直到`complete(entry)`释放。从完成时间起计算下次间隔，首次请求没有历史时间可等待。积压在活跃主机上的URL不会在每次get时全量重扫。堆操作按条目摊销为对数级；单次get可能唤醒多个主机或清理旧项，不宣称严格单步O(log n)。这些同步方法属于事件循环线程。

Crawler先限制创建任务的数量，再进入Semaphore保护的处理管线，防止为所有URL先创建协程。max_pages限制“已尝试+在途”，包含ERROR/DUPLICATE/DISALLOWED。当有空闲槽和剩余预算，每50毫秒检查其他到期主机；满载时等待任务完成。它避免慢主机拖住空闲槽，并不允许同主机并行。

## 持久任务：领取、保存与恢复

```python
crawler = Crawler(CrawlerConfig(
    state_path="crawl.db", max_pages=100, max_concurrent=8,
    per_host_delay=1, recrawl_interval_seconds=3600,
))
results = await crawler.crawl(["https://example.org/"])
# 外部调度器稍后再次运行：
results = await crawler.crawl([])
```

一个文件只允许一个活动owner，OS文件锁在进程崩溃后释放。领取将任务置为inflight，启动恢复未确认任务。HTML快照、内容指纹、任务完成状态和主机可用时间一起提交。失败在持久预算内指数退避；取消归还未确认工作。阻塞数据库操作在线程执行，外层取消后等待已开始的操作结束才释放连接和锁。`_drain`对已接受数据库工作和子任务清理保持shield，即使连续取消也先排空，再传播取消。嵌套finally保证关闭owner及复位`_running`；慢open不会在crawl已返回后泄漏文件锁，清理后的实例仍可复用。

持久frontier只加载有限可用主机窗口，其余任务留在磁盘；没有另加周期写回缓冲。每个批次返回当前结果，未完成任务可在后续批次继续。到期重爬在每次crawl启动时激活；未变内容延长间隔至基础间隔16倍，变化时恢复基础间隔并提高优先级。

## robots与网址边界

robots按origin缓存，以条带锁合并并发miss。标准库负责分组解析，匹配器补充书中`*`、末尾`$`、较具体规则优先和同长Allow优先。先拆URL再规范化路径：`%23`不变成片段，`%2F`不变成路径分隔符，字面`%2A`/`%24`不变成操作符；未保留字符的转义可归一化，Unicode使用UTF-8编码比较。空路径带查询时按`/`开头匹配。

通配符采用顺序子串搜索，避免用易回溯正则处理规则；百分号字节转换仅用固定长度的匹配。保留标准库代理分组选取和网络异常放行政策，没有实现完整RFC分组合并/HTTP失败策略。默认重定向重新入队，目标再次经过过滤、robots与礼貌调度；显式自动同源跟随的边界见[下载器契约](transport-review.md)。

HTML采用首个base href解析相对链接；缺少或格式错误的base回退文档地址。一个坏href只跳过自己。规范化小写scheme/host并去片段，保留路径、查询与userinfo大小写。入队前拒绝错误主机、端口和非HTTP(S)地址，种子与发现链接走相同过滤入口。

## 地址去重与内容去重

URL去重防止同一规范化地址重复入队；内容指纹防止不同地址返回相同字节时重复归档。指纹先由一个任务认领，其他任务等待其提交；认领者失败或取消后可重试，避免误将未保存内容视为已完成。

相同HTML放在不同目录中仍可能发现不同相对地址。因此DUPLICATE仍提取与入队链接，只省略重复快照及Hook。内容Hook异常被隔离，不能把用户副作用当成SQLite事务；回调成功后崩溃可能重放，需以URL/指纹保证幂等。

## 分片与部署边界

ConsistentHashPartition按主机稳定归属；远端发现先持久保存，再调用route_url。目标持久接收后回调才能返回，重复投递必须幂等。各节点使用独立状态文件，成员变化需外部协调任务与礼貌时间移交，见[分片契约](partition-review.md)。

当前没有成员发现、消息代理、自动安全迁移、数据库复制/分片、五年保留自动清理、反垃圾模型或动态渲染集群。严格优先级不等于书中的概率选择/PageRank，低优先级可能长期等待。系统getaddrinfo不能安全强制中断，仍占用有限下载槽直到返回；DNS应用TTL不等于权威TTL。无连接池、HTTP2或生产吞吐承诺。

## 验收入口

- [本轮回归](../tests/test_open_source_review.py)：复杂度、随机调度oracle、空闲槽补位、robots编码、base与坏链接。
- [持久恢复](../tests/test_durable_crawl.py)、[生命周期](../tests/test_lifecycle.py)、[分片管线](../tests/test_partition_pipeline.py)、[真实下载](../tests/test_transport_review.py)。
- [连续取消回归](../tests/test_cancellation_review.py)：受控阻塞open/finish/close，确认排空前所有权仍保留、取消传播及实例可复用。
- [基准与HTTP复跑](../benchmarks/REPORT.md)：259项测试、历史相同负载调度对照，以及本轮自有回环服务的下载/重启验证。
