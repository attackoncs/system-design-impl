# 第 10 章实现核对

本轮继续完成上一轮遗留的持久调度、恢复、周期重爬、分片路由及 DNS 缓存，依据仓库《System Design Interview.md》第 10 章。这里记录可运行的参考实现与尚需部署系统支持的边界。

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

2026-10-24 完整项目 **213 项通过（12.69 秒）**，Ruff E9/F 通过。覆盖磁盘恢复、独占 owner、原子归档、持久重试与自适应重爬、取消重投、每批次预算、相同内容不同基准 URL 的链接发现、有限窗口公平性、真实分片投递管线，以及本机 HTTP、DNS、重定向、超限/截断响应、取消后的下载容量回收。未进行公网爬取、跨机房部署或生产容量压测。
