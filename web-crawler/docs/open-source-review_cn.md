# 第10章：原书、开源实现与本地代码核对

日期：2026-11-01。原书范围为《System Design Interview.md》第10章。通过CodeGraph核对 `crawl → get → _process_url → _process_entry` 和持久完成/取消路径，并补齐上一轮尚未同步的文档与可复跑验证工具。

## GitHub与技术依据

已确认存在开源实现 [scrapy/scrapy](https://github.com/scrapy/scrapy)。阅读其[调度器源码](https://github.com/scrapy/scrapy/blob/master/scrapy/core/scheduler.py)和[优先队列源码](https://github.com/scrapy/scrapy/blob/master/scrapy/pqueues.py)：请求去重与存储分离、支持磁盘队列，下载感知队列按slot组织待处理请求。参考的是组件边界和调度思路；本地保留asyncio与标准库，没有复制其Twisted实现或新增运行时依赖。链接指向核对时的分支，后续源码可能变化。

robots编码和特殊字符行为参考 [RFC9309第2.2.2/2.2.3节](https://www.rfc-editor.org/rfc/rfc9309.html#section-2.2.2)：规则从路径起点匹配、较具体规则优先，同长Allow优先；`*`、结束锚点与字面百分号转义需要区分。本地保留标准库用户代理分组选取及既有下载失败放行政策，没有宣称完整实现RFC的全部分组合并和失败状态策略。

## 章节覆盖

| 书中内容 | 本地实现 | 运行边界 |
|---|---|---|
| 种子、URL队列、优先级和礼貌性 | 每主机优先堆、就绪队首堆、延迟唤醒堆；活跃主机保留；同优先级按入队顺序 | 严格优先级，并非书中概率加权选择；长期高优先级流量可能使低优先级等待 |
| 内存/磁盘混合frontier | 默认内存；state_path启用SQLite，有限可用主机窗口 | 未实现书中周期写入缓冲；持久入队以可恢复性为先，SQLite单活动owner |
| HTML下载、超时、DNS缓存 | 下载槽、总deadline、响应体上限、TTL/LRU DNS | 无连接池/HTTP2；系统getaddrinfo不能安全强制中断 |
| robots缓存与书中通配符 | origin缓存、条带锁、Crawl-delay、`*`、`$`和最长匹配 | 保留标准库分组语义；失败放行不是完整RFC失败政策 |
| 链接提取、过滤、URL去重 | 首个base href、相对URL、坏链接隔离；拒绝缺主机/坏端口；只小写scheme/host | 保留userinfo大小写；不执行JavaScript |
| 内容去重和保存 | SHA-256、在途指纹认领；重复字节仍按各自URL发现链接 | 不重复归档或回调；按字节内容而非语义相似度去重 |
| 恢复与失败处理 | 任务/快照/指纹/主机时间事务提交，恢复inflight、取消归还、预算重试 | 外部Hook与投递副作用需要幂等；不保证恰好一次 |
| 新鲜度 | 按历史变化调整重爬间隔，变化时提高优先级 | 由外部调度器启动批次，无PageRank计算 |
| 分布式抓取/一致性哈希 | 主机归属和持久路由移交接口 | 未提供消息代理、成员发现、租约或自动安全迁移 |
| 蛛网陷阱、扩展与规模 | max_pages/max_depth/URL长度、域名/扩展名/自定义过滤、内容Hook | 反垃圾模型、动态渲染、地域部署、复制分片及五年保留清理不在当前运行实现内 |

不以本地测试宣称实现原书每月十亿页或30PB存储规模。数据库复制和集群控制面是独立部署工作。

## 已完成的修复与收尾

1. 活跃主机积压不再每次轮询重扫；仅发布可用主机队首。随机操作序列与简单全扫描oracle对照，核对优先级、FIFO、保留、完成和时间推进行为。
2. 当批次仍有空闲并发槽时，每50毫秒重新检查到期主机；避免另一个慢下载阻塞已经可以开始的工作。仅有槽位和预算时轮询，满载时等待任务完成。
3. 一个格式错误的链接不再使整页失败；首个base href决定相对路径，后续base忽略。规范化不再把大小写敏感的用户名/密码转小写。
4. robots补充书中通配符、结束锚点和同长Allow优先，采用顺序子串匹配避免正则回溯。解析URL后才规范化路径编码，保留`%23`、`%2F`的边界，`%2A`/`%24`仍表示字面字符；未保留字符的转义可归一化，Unicode按UTF-8比较。
5. 新增10项编码回归，其中5项在修复前失败；修复后全部通过。另补2项空路径查询回归并先复现失败；累计本轮回归43项，原有213项，合计256项。
6. 修复测试中的SQLite连接关闭，`-W error`运行不依赖忽略ResourceWarning。
7. 补齐中英README、需求、任务、设计文档；设计中的旧实现副本改为维护中源码链接。学习路线说明调度补位、robots编码、base href与失败路径。
8. 示例种子与模拟页面统一采用带尾斜杠的根地址，主页回链可正确去重，不再因模拟数据漏项产生额外ERROR。

## 验证入口

- [本轮回归](../tests/test_open_source_review.py)：背压、调度oracle、空闲槽、robots与链接管线。
- [持久任务测试](../tests/test_durable_crawl.py)、[分片管线](../tests/test_partition_pipeline.py)、[实际下载测试](../tests/test_transport_review.py)。
- [调度基准与真实HTTP结果](../benchmarks/REPORT.md)：相同负载基线/当前各3次；真实下载验证恢复、base href、坏链接隔离和robots禁止路径未请求。
- [学习路线](learning-guide_cn.md)提供宏观图、状态表、函数入口、源码摘录和可预测结果的练习。
