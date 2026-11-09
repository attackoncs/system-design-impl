# 第22章核对：广告点击事件聚合

## 2026-11-09 热路径效率复核

问题：`Aggregator.status()`除检查点行外还执行`SELECT count(*) FROM buckets`。
`apply()`返回`self.status()`；`WorkerLoop.run_once`每次拉取前（空闲时约10次/秒）都调用
`status()["seq"]`；`replay()`每个200事件页调用两次`status()`，桶数随重放增长，整体约O(N²)。

修复：`status(counts=True)`增加`counts`参数，`counts=False`只读检查点行；`apply()`直接返回
同一写事务内已提交的检查点（不含桶计数），一致性不弱于原先提交后再读。轮询和`replay()`
改用`status(counts=False)`/`apply()`返回值；`/internal/status`仍返回`bucket_rows`，HTTP形状不变。
tailer每次运行重新哈希[0, offset)前缀是有意的篡改/截断校验，本轮保持不变。

验证：`-W error -p no:asyncio`完整套件修前74项通过、4项失败，修后同样74通过/4失败，失败集合一致
（HTTP ResourceWarning及真实网络用例的环境问题，非本次改动引起）。临时测量（/tmp，非仓库）：
20万桶时`status()`约5.98ms，`status(counts=False)`约0.53ms；1万桶时0.40ms/0.30ms。

### 去重/状态GC

问题：worker的`seen(event_id,hash)`与`input_positions`回执永久增长。原始日志无保留期、
可从0重放，且入口`event_id`全局唯一；worker的`seen`是兼容源的纵深防御。

策略：`dedup_horizon`（秒，默认7天，`null`关闭=原行为）以`max_event`（当前位置之前的
分区最大事件时间）为基准；新位置`click_timestamp < max_event - dedup_horizon`时不计数、
不写键，只增加检查点新计数`expired`，判断先于任何ID查找。要求视界≥`lateness`，故过期
事件必在watermark之后。`replay_horizon`（位置数，默认100000，`null`关闭）：每次apply先
把`worker_meta.replay_floor`抬到`seq-replay_horizon+1`，再删其下回执；低于下限的重放返回
409，不回落到旧`position_floor`的按ID验证。迁移：`ALTER TABLE seen ADD COLUMN
click_timestamp`（旧行NULL=永久保留）、`checkpoint.expired DEFAULT 0`、索引
`seen_by_time`。GC在apply事务内、`before_commit`之前，每表每次最多512行，走索引/主键范围。

安全论证：`max_event`单调不减，键被删时其事件时间低于当前截止点，之后同ID副本必然过期，
只会少计（记入`expired`）不会重复计数；视界外仅丢失冲突负载检测（入口已全局拒绝ID复用）。
`max_event`是源日志的确定性前缀最大值（不含墙钟/接收时间），`reconcile`以同一
`records.expired()`镜像，`rebuild`复用同一`Aggregator`，故视界内外计数均与在线一致、与GC
进度无关。差异仅在中途修改视界或旧库迁移前的历史，需重建新结果代。

验证：/tmp临时脚本（非仓库）——3组随机负载（15%重复ID、10%最长30天迟到）：关闭视界时
与HEAD版`Aggregator`的审计哈希和检查点逐项一致；开启时7事件小页+陈旧页重放的在线库与
200事件页重建库哈希/检查点一致，计数与`expired`等于独立参考模型，伪造远端的`reconcile`
匹配；回执稳定在1000条、旧页重放409、`seen`最小事件时间≥截止点；旧schema库迁移后NULL
键保留。`EXPLAIN QUERY PLAN`确认两条删除均为索引范围。现有套件78项通过。

## 2026-11-04 源码复核

重读本章，并用CodeGraph复核原始日志/聚合事务、文件检查点/恢复、精确归并及HTTP。
实际阅读PyFlink广告业务示例和Flink窗口运行时，固定版本和语义差异见
[开源对照](open-source-review_cn.md)。原有核心覆盖如下，第二输出队列等生产扩展保持未完成。

修复两处错误边界：持久原始行的深JSON损坏统一报告503，保留源行/head，不返回部分页；
必要source或worker响应的深JSON无法解析时，有效查询也报告503，避免误报客户端400。
查询finally仍取消/排空并释放容量，上游恢复后同服务可继续查询；无效客户端输入仍按400处理。

修前基线76项通过、68.043秒；两个新增测试先复现上述问题。修后完整
**78项通过、38.131秒，无跳过，使用`-W error`**。六进程示例通过：100唯一点击、
100重复回执、hot=80/other=20及lag=0，对账一致。3000点击全部计入、完整维度对账一致；
40次查询均通过总数断言。[最终报告](../benchmarks/results.md)保留实际命令及原始数据。

学习指南补充“完整候选归并后再取Top N”的调用图及两处故障边界，中英文文档同步。
PyFlink示例的inner join分母与本章单点击计数不同，不照搬为完整CTR或金融结算实现。

## 2026-10-26 历史核心审查

核对日期2026-10-26（Asia/Shanghai），依据
[《System Design Interview.md》](<../../System Design Interview.md>)9664–10506行。
本轮保持标准库、单节点原始日志、独立聚合进程和精确归并的可执行参考；不将原书规模当作已验收容量。

## 原书映射

| 原书内容 | 当前实现 | 边界/差距 |
| --- | --- | --- |
| 分布在服务器上的追加点击日志 | JSONL tailer，完整行/前缀校验、远程确认后检查点 | 每源单owner；无全局文件发现或自动轮转迁移 |
| 原始事件和可回算数据 | event_id去重、规范化IP/国家、持久分区原始日志 | 本机SQLite，不是复制Kafka/Cassandra或对象存储集群 |
| 分钟广告计数和近M分钟Top N | 分钟桶、半开范围、完整候选全局归并、heap选择Top N | 10000候选上限，无亿级持续查询验收 |
| user/IP/country过滤维度 | 精确AND维度桶及索引 | 显式过滤条件，不是书中预定义filter_id管理服务 |
| Map/Aggregate/Reduce与热点 | 入库规范化、按event_id加盐散到分区、独立聚合、查询归并 | 固定物理分区；非Flink/YARN DAG、无动态申请热点资源 |
| 局部Top N优化 | 加盐后必须取完整局部候选再求全局Top N | 保留反例回归：每片hot=6/local=9，全局hot=24 |
| 事件时间和迟到窗口 | click_timestamp分钟桶、单调watermark、晚到修正及revision | 闭合可继续修正；不是最终结算或永不变化的承诺 |
| 重复和中途故障 | 原始ID指纹回执、聚合seen、计数/去重/位点同事务 | 恰好一次仅同库聚合可见性，不含金融结算/任意外部副作用 |
| 重放位点完整性 | 新input_positions保存seq/ID/hash/accepted，旧位点也校验 | 历史旧库无完整位点回执时只能按seen ID/hash迁移回退 |
| 输出队列和数据库writer | 本参考计数直接与checkpoint同SQLite事务提交 | 没有书中第二条独立输出队列/分布式事务管线 |
| 快照与故障恢复 | 本地事务持久检查点、进程恢复、原始日志重建到新库 | 无异地快照、机器/磁盘丢失恢复或自动代切换 |
| Kappa重算 | replay调用同Aggregator.apply，固定源head、单独输出库 | 有限输入/期限，不是无限历史分布式回算任务 |
| 独立批对账 | 按固定worker checkpoint重读raw，以独立维度Counter计算摘要 | 每worker快照组合，非跨分区同时点；预算不足明确失败 |
| 延迟/积压/状态监控 | accepted/事件时间、checkpoint/head差、watermark、late、worker错误 | 只暴露本参考状态，无完整生产资源监控/告警平台 |
| 横向规模与容灾 | 多进程分区、有界并发查询、身份验证和全部worker必要 | 单日志节点、无复制/自动选主/在线重分区/多地域 |

## 工程修复

纯records验证、聚合schema、检查点文件操作、查询归并、recovery、transport与worker生命周期
各有独立职责，保留原log/Aggregator/service/tailer公开入口。事件规范化/指纹计算在writer外，
接受时间在获取writer后采样；幂等冲突整批回滚，重复请求在容量已满时仍可重放。
源页数量/结构/head/连续offset/hash/accepted完整验证后再写；不能因seq已经提交就跳过损坏内容。
计数与seen/输入回执/checkpoint同一事务，audit用兼容原digest(rows)的流式编码，最多100万维度行。

Tailer验证有界检查点及ACK、用OS锁防同检查点并发、拒绝源与检查点同文件/硬链接、
唯一临时文件fsync后replace。原始行最多8192字节，批最多200条，单次默认1000批，
`limit_reached`提示继续；只消费完整行。进程退出释放OS锁，sidecar保留。Windows目录掉电
语义仍不作普遍保证；检查点确认和重放不等于队列HA。

HTTP API拥有一个QueryService共享池（默认8线程/8查询/5秒），不按每个请求创建分区线程池。
元数据与全部worker RPC共用总截止期限；失败取消待调度/在途工作，名额保留至实际任务退出。
检查每个worker的身份、序号、有限watermark和非负整数计数，再精确合并；worker及全局候选
预算均返回422，peer故障返回503，不返回部分排名。没有跨分区原子读快照，lag仍是估计。

reconcile/replay默认60秒、20万事件，独立对账最多10万维度；检查空页、倒序、越界、
身份和内容校验后推进，拒绝不可达target，不悄悄输出不完整重建。更大任务需显式配置或分块扩展。
网络层直连、拒绝代理/重定向、严格framing/JSON和有界响应；handler/worker排空，绑定成功后才
启动工作线程，失败可观察重试。socket期限不能强杀DNS/SQL/CPU，关闭等待真实接受工作退出。

## 验证与图分析

最终 **76项通过（22.816秒），无跳过/ResourceWarning**。其中既有11项覆盖真实6进程、
事务前退出、服务重启、精确Top N及对账；新增30项存储/文件检查点、14项查询/恢复、
14项真实HTTP/RPC、7项CLI/后台回归。controlled RPC/provider故障与真实进程实验分别标明。

原6进程demo成功：100唯一点击、100重复回执、hot=80/other=20、所有lag=0，对账一致。
3000点击实测全部计入且完整维度对账一致；发布5631.68条/秒，40查询p50/p95为
60.902/78.452ms。单次小样本不是稳定提升比或生产容量证明。
[验证报告](../benchmarks/results.md)和[原始数据](../benchmarks/review-2026-10-26.json)。

code-review-graph最小上下文、增量更新及影响分析成功，无构建错误；7个既有核心文件为
57直接改动节点、25受影响节点、额外5文件，1244调用未解析，边有截断。新未跟踪
query_service.py明确未索引；新模块用源码、独立复核和实际执行补核。图上下文还包含此前
章节的脏改动，不以空边或未索引结果证明没有调用/测试。

## 仍未实现的生产能力

复制日志/存储、机器故障容灾、自动重分区/热点调度、第二输出队列、分布式事务、
大规模流/批平台、原始数据云归档及隐私保留、TLS/topic身份/租户隔离、欺诈识别、
财务结算、持续50k QPS与十亿事件验收均为后续，不能由本地进程保留磁盘的测试替代。
