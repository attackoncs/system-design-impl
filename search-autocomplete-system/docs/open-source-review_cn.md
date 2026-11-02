# 第14章：原书、GitHub源码与本地实现核对

原书范围：《System Design Interview.md》4289–4763行，Design A Search Autocomplete System。2026-11-01阅读原书与开源代码，2026-11-02完成实现和验证。通过CodeGraph追踪采集、聚合、构建发布、本地查询和HTTP副本路径。

## GitHub实现与参考范围

| 项目 | 已阅读源码 | 适用范围与本地取舍 |
|---|---|---|
| [valeriansaliou/sonic](https://github.com/valeriansaliou/sonic) | [suggest执行器](https://github.com/valeriansaliou/sonic/blob/f16560b6ea67fd223354c065f7157646931db1b2/core/src/executor/suggest.rs) | FST读锁保护查询视图，限制结果数量；它以单词为单位且支持模糊因素，不等于本章整条短语按历史频次排序 |
| [RediSearch/RediSearch](https://github.com/RediSearch/RediSearch) | [Trie节点上界](https://github.com/RediSearch/RediSearch/blob/1fbe5191c925c710945a7ed526293d552e8e346a/src/trie/trie_node_internal.h)、[Trie搜索](https://github.com/RediSearch/RediSearch/blob/1fbe5191c925c710945a7ed526293d552e8e346a/src/trie/trie.c) | 子树分数上界和Top-K堆可用于剪枝；实际排序还有完全匹配、后缀长度及模糊匹配因素，不能直接替代本地频次/字典序契约 |

参考源码确认了“稳定读取视图、限制候选数量、利用排名上界剪枝”的设计。本地原有不可变Trie和过滤补位已具备这些机制，本轮保留正确实现，没有另写一套索引或复制外部源码。两个项目都不是本章“采集→按周聚合→离线发布→热门短语查询”整条流水线的直接替代品。

## 原书逐项覆盖

| 原书要点 | 本地实现 | 当前边界 |
|---|---|---|
| 起始前缀匹配、历史频次Top 5 | Trie.suggest、确定性频次/字典序排名 | 英文字母及空格，最多50字符；不做拼写纠错 |
| 每个节点缓存热门候选 | 不可变节点Top 5及有界请求LRU | 以额外内存换查询速度；容量按条目而非RSS计 |
| 日志采集与1/N采样 | 稳定event_id、Collector哈希采样 | 记录完成搜索，建议请求不增加频次；采样计数不外推总量 |
| 周聚合、离线Worker | 持久窗口聚合、SnapshotWorker | 默认滚动七天；自然周需显式UTC起止边界 |
| 整代Trie更新 | 输入快照、事务外构建、原子条件发布 | 失败保留旧代；未实现逐节点在线增量更新这一备选方案 |
| Trie DB与缓存回源 | SQLite保存快照，查询加载并缓存Trie | SQLite是单主机共享持久层，不是分布式文档数据库 |
| 即时过滤、异步物理删除 | 过滤修订栅栏、精确Top 5补位、purge-blocked | 屏蔽规则由运维提供；历史快照和事件需制定保留策略 |
| 按数据分布分片 | 按不同查询数均衡字典区间 | 不直接均衡请求热度或字节大小 |
| 多副本与可用性 | 实际HTTP协调、备用副本、版本/修订/归属校验 | 必要分片全失效时返回503，避免给出错误的部分排名 |
| AJAX和一小时浏览器缓存 | GET /suggest可被异步客户端调用 | 没有单独制作浏览器前端；no-store避免浏览器绕过即时过滤 |
| 百万级用户、100ms、48,000峰值QPS | 本地延迟工具及并发/超时回归 | 本次单机顺序测量不证明互联网规模吞吐或存储HA |
| 多语言、地域/CDN、实时热点 | 保留为原书延伸讨论 | 未实现，未作为当前核心能力宣称 |

## 本轮修复

### 完整JSON不代表完整HTTP响应

协调器通过 `HTTPResponse.read1()` 分段读取，防止慢速响应反复重置超时。但read1允许声明了Content-Length的响应提前EOF：原实现只看JSON能否解析，可能把已截断的副本响应当成成功，进而不使用健康备用节点。

现在读取结束后检查剩余长度；非零则抛IncompleteRead，沿原有失败路径切换备用副本。所有必要副本均不可用时仍返回Unavailable。正常Content-Length响应及合法的HTTP/1.0关闭分隔响应继续支持。[真实回环HTTP回归](../tests/test_http_integrity_review.py)验证这三种情况及备用副本结果。

### 统一有限数校验

Collector、SnapshotWorker和HTTP协调器复用现有 `store._finite_number`。超大整数不再从math.isfinite泄漏OverflowError，保持ValueError输入契约，继续拒绝布尔值、非有限数等无效输入。没有新增通用校验框架。

[数值边界回归](../tests/test_numeric_review.py)覆盖采集时间、Worker间隔/窗口/退避、显式窗口终点及clock返回值；HTTP测试另覆盖两种协调器超时参数。

### 显式释放资源并完成示例退出

既有测试捕获HTTPError后只检查状态码/响应头，没有关闭响应体；另一个SQLite迁移夹具只使用事务上下文，没有关闭连接。现已显式关闭，保留原断言与提交语义，不屏蔽ResourceWarning。

Windows网络示例的功能结果原本正确，但退出时临时数据库可能仍被短暂占用。现在在等待自有子进程、关闭Store后，仅对自建目录的Windows占用错误做有限清理重试；其他错误和持续占用仍报出。修复后示例完整退出。

## 验证结果

- 严格基线为166通过、4项因未释放资源告警失败；原170项全部保留，新增18项，**188 passed（7.78秒）**，启用 `-W error`，未屏蔽资源告警。
- 新增12项采集/Worker数值测试在修复前全部失败；6项HTTP测试中4项失败、2项作为正常响应兼容性保护。
- 既有并发冷加载、发布竞争、过滤修订、全局任务上限、慢HTTP超时和关闭等待回归通过。另用固定种子对120组数据、6240次过滤/跨片查询作完整排序对照，未发现新排名缺陷。
- 本地示例验证原书词频Top 5与过滤补位；网络示例验证三个独立分片进程和一个协调进程、终止一个副本后结果不变、即时过滤生效，退出清理成功。
- 10,000条不同查询、四分片、10,000次本地查询和500次HTTP查询：[原始测量](../benchmarks/review-2026-11-02.json)。构建72.1ms、首次冷查询52.7201ms、本地P99 0.0416ms、HTTP P99 16.4731ms。
- 测量为单主机顺序请求，HTTP服务自身持有全部分片，没有测量跨进程协调器的并发吞吐，也不作前后性能提升结论。详见[测量说明](../benchmarks/results.md)。

收尾检查：20个Python文件通过3.9语法解析，17份文档的482个本地链接有效；三张Mermaid图成功渲染，Ruff关键错误/格式、学习文档生成一致性及diff空白检查通过。代码图已增量更新，并检查本轮四个执行文件的影响范围。
