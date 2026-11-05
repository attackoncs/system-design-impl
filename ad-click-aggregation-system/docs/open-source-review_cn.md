# 第22章：开源源码对照与实现核对

日期：2026-11-04。依据根目录《System Design Interview.md》第22章“Ad Click Event Aggregation”。

结论：分钟计数、精确过滤/全局Top N、去重、迟到修正、重放与独立对账的核心链路
已有实现。本轮修复两处深JSON异常分类，78项完整回归通过。复制日志、第二输出队列、
外部sink事务及原书规模仍按下文列为待扩展能力。

## GitHub开源参考

通过GitHub连接器分别检索广告CTR聚合示例与Apache Flink，并按以下固定提交阅读
实际源码。前者提供具体业务流水线，后者提供事件时间窗口的运行时机制；本轮未构建、
运行或压测上游项目，未复制实现或新增运行依赖。

| 项目与固定版本 | 实际阅读的源码 | 可对照的机制 |
| --- | --- | --- |
| [stream_analytics_adtech_ctr](https://github.com/susmitpy/stream_analytics_adtech_ctr/tree/0c4cb721a88b1e2f2978c681959a831c700c014d) | [两流时间关联](https://github.com/susmitpy/stream_analytics_adtech_ctr/blob/0c4cb721a88b1e2f2978c681959a831c700c014d/flink/ctr.py#L30)、[窗口和去重计数](https://github.com/susmitpy/stream_analytics_adtech_ctr/blob/0c4cb721a88b1e2f2978c681959a831c700c014d/flink/ctr.py#L38)、[checkpoint配置](https://github.com/susmitpy/stream_analytics_adtech_ctr/blob/0c4cb721a88b1e2f2978c681959a831c700c014d/flink/ctr.py#L68) | PyFlink按impr_id关联曝光和点击，并限定点击在曝光后15秒内；按曝光事件时间做30秒滚动窗口，再按campaign去重计数，写入文件系统表。checkpoint间隔设为30秒。 |
| 同上 | [点击源事件时间](https://github.com/susmitpy/stream_analytics_adtech_ctr/blob/0c4cb721a88b1e2f2978c681959a831c700c014d/flink/sql/tbl_clicks.sql#L6)、[曝光源事件时间](https://github.com/susmitpy/stream_analytics_adtech_ctr/blob/0c4cb721a88b1e2f2978c681959a831c700c014d/flink/sql/tbl_impressions.sql#L6)、[文件输出表](https://github.com/susmitpy/stream_analytics_adtech_ctr/blob/0c4cb721a88b1e2f2978c681959a831c700c014d/flink/sql/tbl_ctr.sql#L8) | 源SQL将毫秒时间转为TIMESTAMP_LTZ，并声明事件时间减5秒的watermark；从Kafka最早offset读起。输出CSV按campaign分目录，配置滚动及分区提交策略。 |
| [Apache Flink](https://github.com/apache/flink/tree/4cdc6d83a9db7f82a99610d6382bfb762bc65f7d) | [窗口写入/触发/晚到分流](https://github.com/apache/flink/blob/4cdc6d83a9db7f82a99610d6382bfb762bc65f7d/flink-runtime/src/main/java/org/apache/flink/streaming/runtime/operators/windowing/WindowOperator.java#L300)、[事件时间定时器](https://github.com/apache/flink/blob/4cdc6d83a9db7f82a99610d6382bfb762bc65f7d/flink-runtime/src/main/java/org/apache/flink/streaming/runtime/operators/windowing/WindowOperator.java#L453)、[晚到判定](https://github.com/apache/flink/blob/4cdc6d83a9db7f82a99610d6382bfb762bc65f7d/flink-runtime/src/main/java/org/apache/flink/streaming/runtime/operators/windowing/WindowOperator.java#L612)、[清理时间](https://github.com/apache/flink/blob/4cdc6d83a9db7f82a99610d6382bfb762bc65f7d/flink-runtime/src/main/java/org/apache/flink/streaming/runtime/operators/windowing/WindowOperator.java#L673) | 每个分配窗口先检查是否已超过清理时间，再更新窗口状态并执行trigger；事件时间定时器负责触发及清理。未被任何窗口处理的晚到记录进入已配置side output，否则增加丢弃计数；清理时间结合窗口结束和allowedLateness，并防止时间加法溢出。 |

### 业务示例应按源码核对范围

CTR项目是公开教学实现，主题是曝光与点击关联后的campaign统计，不是本章全部系统。
根据`ctr.py`的实际inner join，未匹配点击的曝光不会进入后面的窗口聚合；因此不能仅
依据README的CTR表述，把输出分母当作所有曝光。该判断来自源码路径，本轮未运行
其数据集或比较结果。

同理，窗口内`count distinct`只能说明对应窗口/关联结果上的去重计数，不能替代本地
稳定event_id、不可变内容指纹、持久回执和冲突整批回滚。checkpoint配置及文件输出
选项也不能单独证明任何外部数据库/通知/财务结算都拥有恰好一次副作用。

## 用于本地审查的检查点

1. **先确认统计对象，再参考窗口写法。** 曝光、点击、关联结果及按广告计数是不同
   对象。本地需要验证每个合法点击只按其event_id计入一次，不把同一用户的多次
   正常点击当重复，也不额外引入本章未要求的曝光流或CTR分母。
2. **区分处理时间、接收时间和事件时间。** 上游SQL显式定义事件时间；本地应逐项
   核对分钟归属、半开查询边界、未来容差、watermark推进及空闲分区。迟到修正不能
   因重新接收时刻改变而进入错误分钟。
3. **watermark不是“永远不变”的通用承诺。** Flink运行时将窗口触发、清理和晚到
   分流区分处理。对照本地应检查closed含义、迟到计数/revision，以及回放历史记录
   时是否仍能验证内容；不能因为窗口标closed就跳过晚到或历史损坏校验。
4. **去重状态和可见计数必须一起提交。** 窗口distinct与本地全事件ID去重单位不同。
   本地需检查seen、计数、输入位置回执和checkpoint同事务，崩溃前后重投不会重复
   累加；同ID换内容必须冲突，不能以“旧offset”直接忽略。
5. **分散热点后，全局Top N需要完整候选。** 选读的CTR代码没有实现全局广告Top N。
   本地按event_id将热点广告分散后，局部截断会遗漏全局赢家，必须先跨分区按广告
   合并，再选Top N，并在候选预算不足或必需分区失败时明确失败。
6. **回算与在线状态必须有明确边界。** 上游窗口清理意味着状态生命周期是协议的一
   部分。本地保留原始日志并重建到新库时，应固定源head、校验lineage/连续位点及
   内容，保证预算耗尽会报错，而不是把未完成结果当完整恢复。

## 与本地参考架构的差异

本地覆盖和扩展项以[章节映射](chapter22-review.md)、[设计](design_cn.md)及
[验证报告](../benchmarks/results.md)为准。上游选读模块不能代替本地执行证据。

- 本地是单节点SQLite原始日志、固定分区独立聚合进程和全局查询归并；CTR示例使用
  Kafka/PyFlink及文件表。没有将本地服务标成Flink物理DAG、复制Kafka或checkpoint
  协调器，也没有直接兼容上游表/日志协议。
- 本地稳定event_id指纹去重与SQL原子聚合，不等于上游关联后窗口distinct。CTR示例
  没有提供本地的IP/国家规范化、同ID内容冲突、精确AND过滤或完整全局Top N。
- Flink的WindowOperator在清理时间后忽略已晚窗口，记录可侧输出或丢弃；本地策略
  是旧窗口继续接受迟到修正，closed仅表示当前watermark判断，并非最终结算。
  本地尚无自动原始数据/去重键清理，二者资源与结果生命周期不同。
- 本地空闲worker可结合接收/墙上时钟推进watermark；本次选读Flink范围未涵盖多输入
  watermark合并和idle检测，不能由窗口算子源码推定与本地空闲策略完全一致。
- 当前同库事务中的计数、seen及输入checkpoint是本地原子可见边界；跨分区查询是
  多个分区快照的组合，不是跨机器原子快照。本轮未审计Flink完整checkpoint/source/
  sink一致性协议，更未证明任意外部系统的恰好一次写入。

复制日志/聚合存储、在线重分区与热点调度、第二输出队列、分布式批/流平台、机器/
磁盘丢失容灾、云归档/隐私保留、TLS/多租户、欺诈与财务结算以及原书规模持续验收，
仍需独立实现和验证。本轮本地代码、测试及文档结论由验证记录补充。

## 本地覆盖与源码路径

完整条目见[章节映射](chapter22-review.md)。本轮分别复核聚合/存储、文件检查点/恢复
和查询/HTTP，保留现有职责边界，仅补确认的错误处理缺口。

| 章节链路 | 实现与关键约束 | 证据范围 |
| --- | --- | --- |
| 原始事件受理/分区 | [EventLog](../src/ad_click/log.py)规范化、持久event_id/内容hash、同批冲突回滚、event_id盐化 | 真实SQLite及HTTP；同一用户不同event_id仍是不同点击 |
| 聚合/去重/位点 | [Aggregator](../src/ad_click/aggregate.py)与[records](../src/ad_click/records.py)整页验证，计数/seen/input_positions/checkpoint同事务 | 提交前实际进程退出回滚、重复重放与历史位点损坏校验 |
| 事件时间和迟到 | click_timestamp分钟桶、单调watermark、旧桶revision修正 | 原始/接收/墙钟分别处理；closed不等于财务封账 |
| 精确过滤/Top N | [QueryService](../src/ad_click/query_service.py)取全部候选、验证全部分区、按广告求和后heap选择 | hot=6×4大于各片local=9的反例；分区缺失不返回部分排名 |
| 文件接入 | [tailer](../src/ad_click/tailer.py)、[checkpoints](../src/ad_click/checkpoints.py)完整行/前缀校验、单owner、有效ACK后发布位点 | 实际文件/OS锁/硬链接和受控ACK故障；进程退出释放OS锁，sidecar可保留 |
| 回算/对账 | [recovery](../src/ad_click/recovery.py)固定head/worker checkpoint、有界重放和独立维度Counter | 独立对账匹配、故意损坏计数能检出；无跨分区原子同时点 |
| 网络/生命周期 | [HTTP](../src/ad_click/http.py)严格协议/鉴权；共享查询池、总deadline、取消/排空 | 实际HTTP与六进程；socket期限不能强杀已接受SQL/DNS/CPU |

### 两处最小修复

1. `parse_source_row`原有损坏包装遗漏RecursionError。真实SQLite中将第二条持久记录
   改为4000层、8001字节JSON后，读取可抛裸异常。现在按既有源校验失败统一503；
   不返回前面半页，不删除/跳过损坏行，重开后仍拒绝，源head保持不变。
2. `QueryService.aggregate`的peer错误包装也遗漏RecursionError。source元数据或
   worker totals的HTTP响应过深无法解码时，外层会把有效查询误报400。现在在peer
   阶段统一503；客户端参数校验仍在该阶段之前，保留400。finally继续取消/等待
   已接受任务，释放容量，恢复健康上游后同一服务可再次成功查询。

新增[存储回归](../tests/test_aggregation_source_review.py)与
[HTTP回归](../tests/test_http_reliability.py)，均先复现原行为再修复。前者使用真实
SQLite损坏；后者使用真实API/受控HTTP peer，覆盖source和worker两个路径，以及
capacity=1时恢复查询。它们不是机器/磁盘丢失或任意外部sink事务的证明。

### CodeGraph及验证范围

CodeGraph用于读取当前源码、追踪输入校验/聚合事务/归并/恢复/网络链路。图增量刷新
成功，重解析3文件、更新59节点/694边，无错误；这些是本次更新量，不是全库总量。
限定两源码的直接调用影响分析为21文件内节点、74两跳影响节点、额外13文件；显示
60个影响节点，省略14个，另有253条边截断及764处未解析调用。

源码及完整执行补充Python动态dispatch和新未跟踪测试，图统计不等于完整覆盖。
此前章节的本地未提交变更继续保留，全库差异不视为本章独立风险结论。

## 本轮执行结果

Python 3.13.9 / Windows 11，标准库/SQLite。修前76项通过、68.043秒；修后完整
**78项通过、38.131秒，无跳过，所有警告视为错误**。两个新测试分别加入存储及HTTP
专项。[命令与完整报告](../benchmarks/results.md)保留历史日期，时长变化不声明为提速。

六独立进程示例实际完成100唯一点击、100重复回执，hot=80/other=20、四分区lag=0，
独立对账一致；push/重复push至可查观察值498.17ms，非延迟分位数。

[最终原始基准](../benchmarks/review-2026-11-04.json)：3000事件、70%热点，四分区
751/738/745/766，全部计入并通过完整维度对账；15批受理1.255秒/2391.23条每秒，
全部处理1.578秒。4客户端40查询均通过总数断言，p50/p95为94.116/125.561ms，
39.78 QPS。固定范围无查询缓存，只有warm OS页；没有持续规模或金融恰好一次验收。

完整回归、示例和基准顺序执行；最终基准未与测试/图构建并发。初始基线与专项复核
可能共享主机资源，两个时长不能形成受控性能对照。自建进程已停止，私有诊断数据
留在忽略的.runtime/lab-*。中英文README/设计/需求/任务、根索引和学习指南同步更新，
导读增加完整候选归并图及故障边界。

静态验收：src及两个修改/新增测试的Ruff E9/F通过，4个修改Python文件格式通过；27个
Python文件通过3.10语法解析。16份Markdown的528个本地链接有效，基准JSON可解析；
27份学习指南生成校验通过，本章3张Mermaid图已实际渲染，限定范围的diff空白检查通过。
