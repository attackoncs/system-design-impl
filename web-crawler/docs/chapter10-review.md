# 第 10 章实现核对

## 2026-11-09 robots按RFC 9309补齐

已有：origin缓存与miss合并、`*`/`$`、最长匹配且同长Allow优先、百分号转义规范化（保留`%2F`/`%23`/字面`%2A`/`%24`）。
新增（`robots.py`以自带解析器替换urllib.robotparser）：同一产品标记的多个分组合并（2.2.1），产品标记大小写无关精确匹配（旧实现为子串匹配），仅在无专属分组时合并使用全部`*`分组（旧实现只取第一个`*`组），空行不再拆散分组；/robots.txt隐式允许；2xx仅解析前500 KiB（2.5）；3xx最多跟随5跳，超出或无Location视为不可用；任何4xx（含401/403）视为无限制；5xx视为全部禁止（2.3.1.3–2.3.1.4）；缓存TTL上限24小时，失败最多60秒后重试，期间沿用上次成功结果（2.4）。`FetchError`新增`status_code`，以区分HTTP错误与网络失败。
刻意差异：网络失败（无状态码）默认仍放行，这是此前文档明确保留且有既有测试约束的兼容行为；新增`CrawlerConfig.robots_unreachable_policy="disallow"`即按RFC全部禁止。User-agent值中的`/版本`仍被去除；Crawl-delay（非RFC）取合并分组中的最大值。
验证：/tmp一次性脚本覆盖分组合并、`*`回退、子串不匹配、404/403/503/HTTP 500/网络失败两种策略、5跳与6跳重定向、500 KiB截断、不可达时沿用缓存、24小时上限；既有套件`-W error` **259项通过**，未修改测试或基准。

## 2026-11-09 持久调度查询效率

问题：`SQLiteCrawlState.get()`用`ROW_NUMBER() OVER (PARTITION BY host ...)`对全部已到期pending行排序编号，再取`LIMIT window_size`，每次派发都是O(pending·log pending)；20万待抓取时每次get约0.43秒。
修复：新增索引`host_jobs(state,host,priority)`（隐含rowid）；查询改为递归CTE按索引逐个跳到下一个pending主机（每主机一次seek），过滤活动主机与`hosts.next_ready`后，每主机用`ORDER BY priority,rowid LIMIT 1`取已到期队首，再按`priority,url`排序取窗口。代价降为O(主机数·log n)。语义不变：同一组主机队首、同样的礼貌/到期/优先级过滤和排序，领取仍在同一事务内置为inflight。旧库在open时`CREATE INDEX IF NOT EXISTS`自动补建。
测量（Linux/Python3.14，/tmp一次性脚本，新旧代码在同一数据库副本上运行相同get/finish/retry序列）：20万任务/2000主机，每次get **427ms → 11.3ms**，返回序列完全一致；4000任务/150主机、window为1/3/128各3000步，序列亦完全一致。
验证：`-W error`下修改前后均 **259项通过**。未新增测试。


## 2026-11-05 复核

重新阅读原书2729–3176行及既有需求/设计/任务，并用CodeGraph核查调度、持久完成、内容认领和取消路径。2026-11-01已完成持久frontier、周期重爬、DNS与robots等实现；本轮保留其覆盖矩阵，不重复添加这些功能。

发现并复现连续取消缺陷：`_io`首次取消后直接等待线程Task，第二次取消可取消这个包装任务，使crawl提前返回，慢open仍可能随后获得状态文件锁；close期间取消还可能使`_running`无法复位。最小修复是在数据库操作和子任务清理两处共用`_drain`，每次取消都保持shield直至接受的工作完成；嵌套finally确保关闭状态文件并复位运行标记，随后传播取消。它不会强制中断系统DNS、SQL或用户阻塞代码。

[3项回归](../tests/test_cancellation_review.py)分别在open/finish/close入口用事件阻塞真实SQLite操作，先在旧实现失败，再验证排空前不能返回、已有文件所有权不被提前释放、取消向外传播、原实例及新owner可继续使用。修改前256项/13.04秒；最终 **259项/14.05秒，-W error**。实际回环HTTP仍2成功、1禁止、2份快照且恢复后无未确认任务；mock示例4页成功。详情和原始输出见[验证报告](../benchmarks/REPORT.md)。

GitHub重新检索并固定读取Scrapy `36a7e21239f4be2941ce2059400d79702d5d7986`，对照调度器、下载感知队列及robots等待者隔离，见[源码对照](open-source-review_cn.md)。未安装或运行Scrapy，也没有进行公网抓取。严格优先级、完整RFC分组/失败政策、成员发现与安全迁移、动态渲染、数据库复制及五年留存仍是下文说明的边界。

## 2026-11-01 历史实现与覆盖

本轮继续完成上一轮遗留的持久调度、恢复、周期重爬、分片路由及 DNS 缓存，依据仓库《System Design Interview.md》第 10 章。这里记录可运行的参考实现与尚需部署系统支持的边界。

2026-11-01收尾：补充可用主机队首调度、空闲并发槽补位、书中robots通配符与编码边界、base href和坏链接隔离。最新[原书/开源对照](open-source-review_cn.md)与[测量记录](../benchmarks/REPORT.md)包含可复跑证据；下表所列恢复与部署边界仍适用。

| 原书内容 | 当前执行路径 |
| --- | --- |
| 种子、URL frontier、优先级 | 种子和发现链接统一过滤；HIGH/MEDIUM/LOW 排队。持久模式每次从磁盘读取有限的可用主机队首窗口，全部任务保存在 SQLite，避免把整个 frontier 加载进内存。 |
| 礼貌访问 | 同一主机只占用一个页面任务，完成后记录下次可访问时间；robots 下载后与页面之间也有等待，支持 robots Crawl-delay。持久模式完成确认与主机时间在同一事务中保存。 |
| robots 缓存 | 按 origin 区分，条带锁合并缓存未命中请求；失败时保持原有允许访问策略。默认每个 HTTP 重定向重新排队并检查目标路径的 robots。 |
| HTML 下载 | 全批次并发上限、有界下载线程、总 deadline、默认 10 MiB 体积限制、截断检测及取消关闭 socket。DNS TTL/LRU 缓存保留 HTTP Host、TLS SNI 和默认证书验证。详见 [下载器记录](transport-review.md)。 |
| URL 与内容去重 | URL 规范化且持久唯一；已完成内容 fingerprint 存盘。并发重复内容等待首个提交成功，首个失败可重新处理。相同 HTML 在不同 URL 下仍发现各自相对链接，跳过重复归档与回调。 |
| 内容保存与恢复 | state_path 启用后，HTML 快照、fingerprint、job 完成确认和主机时间原子提交；启动恢复 in-flight，取消重投，失败按持久预算退避重试。SQLiteArchive 仍可独立用作异步内容钩子。 |
| 新鲜度 | recrawl_interval_seconds 启用周期任务；每次 crawl 启动时激活到期记录。页面未变则间隔翻倍，最多基础间隔的 16 倍；变化时恢复基础间隔并提高优先级。 |
| 分布式抓取 | ConsistentHashPartition 按主机稳定分配 owner；Crawler 接受 partition 和 route_url 回调，非本地 URL 先持久化，再投递。投递异常重试；回调应在目标端持久接收后返回，并保证重复投递幂等。详见 [分片记录](partition-review.md)。 |
| 可扩展性 | Fetcher、内容回调、分片投递回调为独立接口；异步可调用对象会被正确等待。 |

## 持久批次与周期重爬

```python
import asyncio
from web_crawler import Crawler, CrawlerConfig

async def main():
    crawler = Crawler(CrawlerConfig(
        state_path="crawl.db", max_pages=100, max_concurrent=8,
        per_host_delay=1, recrawl_interval_seconds=3600,
        max_retries=2, retry_delay_seconds=1,
        max_response_bytes=2 * 1024 * 1024,
    ))
    await crawler.crawl(["https://example.com/"])
    # 外部调度器定时调用；不会在单次 crawl 中永久等待未来重爬。
    await crawler.crawl([])

asyncio.run(main())
```

`max_pages` 现在限制每批次的处理尝试，包括重复、禁止和失败，防止全是失败链接时无限消耗资源；每次返回本批次结果。未完成队列保留到后续批次。仅配置内存模式时，无磁盘恢复和跨重启去重。同一个 Crawler 拒绝并发 crawl；同一个状态文件由 OS 文件锁保护，仅允许一个活动 owner，进程崩溃自动释放，其他 owner 不能重置正在运行的任务。每个工作节点使用自己的状态文件。

状态库不保证用户回调与外部副作用恰好一次：回调成功后进程崩溃可能重放，需以规范化 URL/fingerprint 幂等。数据库出错会终止该批次并保留恢复状态，不会把未归档任务确认为成功。SQLiteArchive 独立钩子的错误沿用日志隔离策略，它与状态库的强制归档不是同一事务。

## 部署边界

- 一致性哈希环及投递协议可注入，但没有集群成员发现、跨机器消息代理、拓扑版本协商或自动安全移交。变更成员需外部控制面冻结/转移相关任务及礼貌时间；仅修改环不能保证过渡期间不重复抓取。
- HTML 快照、任务日志与指纹会增长，当前没有五年保留期自动清理、分片或复制；不宣称单机 SQLite 能承载原书每月十亿页、五年留存的规模。结果列表受批次预算约束，完整 frontier 位于磁盘。
- 重爬采用变化历史的简单退避；优先级是静态等级，没有计算 PageRank。动态渲染、反垃圾和地域部署为原书延伸方向，未提供浏览器渲染集群。
- 系统 getaddrinfo 不能安全强制中断；超时后仍占用有限下载槽直到系统解析返回。无 HTTP/2 或连接池。DNS 应用 TTL 不等于权威 DNS TTL。
- 同步用户回调应保持快速；Python HTML 解析仍同步。没有生产吞吐承诺。robots Request-rate、复杂分布式全局访问节奏不在当前本地调度契约中。

## 验证

2026-11-01完整项目 **256项通过（17.12秒，警告视为错误）**。覆盖磁盘恢复、独占owner、原子归档、持久重试与自适应重爬、取消重投、每批次预算、重复内容的相对链接发现、有限窗口、分片投递，以及本机HTTP、DNS、重定向、超限/截断与下载槽回收。另增加主机积压复杂度、随机调度oracle、空闲槽补位和robots编码/根路径查询回归。真实HTTP复跑确认2条成功、1条被禁止、2份快照且恢复后无未确认任务；未进行公网抓取或生产容量压测。
