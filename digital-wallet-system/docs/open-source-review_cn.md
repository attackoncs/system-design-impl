# 第28章：开源源码对照与实现核对

日期：2026-11-05。依据根目录《System Design Interview.md》14171–15038行的
“Digital Wallet”。本章要求钱包间转账、分布式事务、历史可重建，并讨论本地事件日志、
Raft和CQRS。当前项目选择固定分片上的TCC与事实重放；章节中的备选方案和最终生产
架构需要分别说明，不能由“有转账接口”推导整章已经完整落地。

## GitHub搜索与固定版本

通过GitHub连接器分别检索`formance ledger`、`seata TCC`，再读取仓库元数据、固定
提交及实际源码。第一组搜索返回了相关SDK/客户端，随后直接核实Formance主仓库；
第二组命中的是`apache/incubator-seata`，以下使用该次查询确认的仓库地址。

| 项目 | 固定提交 | 选读范围 |
| --- | --- | --- |
| [Formance Ledger](https://github.com/formancehq/ledger/tree/685e78695f20e06b6c81a8fa1c50dfb56fc096de) | `685e78695f20e06b6c81a8fa1c50dfb56fc096de` | Go记账处理器、posting余额更新、操作指令日志/哈希、FSM状态结构 |
| [Apache Seata](https://github.com/apache/incubator-seata/tree/cba73e180fd8896a99df6415b859d275122feaa7) | `cba73e180fd8896a99df6415b859d275122feaa7` | Java CommonFence、SpringFenceHandler、SQL/表约束、TCC配置和行为测试 |

只阅读和比较源码，没有安装、运行、压测或复制上游代码，也没有增加运行依赖。
下面的结论针对这些固定文件；未审计上游全部协议、部署或安全配置。

## Formance：记账影响、确定性与日志

1. **一次posting同时表达两端影响。** `applyPosting`按账户、asset、color定位余额桶，
   源端增加Output、目标端增加Input；源端检查支出加金额是否超出Input，两端累计量
   均检测uint256溢出。`world`源账户和显式跳过余额校验路径是例外，不能把这个函数
   概括成任何输入下都禁止透支。[源码与例外](https://github.com/formancehq/ledger/blob/685e78695f20e06b6c81a8fa1c50dfb56fc096de/internal/domain/processing/processor_posting.go#L77-L201)
2. **交易引用唯一与请求幂等分开看。** 处理器在任何Scope写入前查询非空reference；
   已存在就返回带原交易ID的冲突，未冲突时才继续生成posting、交易状态及日志。这一引用冲突
   检查本身不等于“同键同内容直接返回首次完整响应”。提案哈希另外按操作指令内容与顺序
   计算；所读注释明确不同批次组合的哈希不同，不能套用到本地单笔transaction_id。
   [引用检查](https://github.com/formancehq/ledger/blob/685e78695f20e06b6c81a8fa1c50dfb56fc096de/internal/domain/processing/processor_transaction.go#L22-L43)、
   [提案哈希](https://github.com/formancehq/ledger/blob/685e78695f20e06b6c81a8fa1c50dfb56fc096de/internal/domain/processing/processor.go#L418-L432)
3. **可重放需要确定性的数据和错误。** 交易日志包含posting、reference、时间及交易后
   volumes；元数据校验特意选择字典序最小的错误账户，避免Go map遍历顺序令不同副本
   产生不同错误。指令处理器把处理结果装入带sequence的日志，同时区分可丢弃的单条指令
   overlay和整个提案失败，不能把Scope中的暂存写入当成已经落盘。
   [交易日志与确定性错误](https://github.com/formancehq/ledger/blob/685e78695f20e06b6c81a8fa1c50dfb56fc096de/internal/domain/processing/processor_transaction.go#L280-L337)、
   [暂存与日志生成](https://github.com/formancehq/ledger/blob/685e78695f20e06b6c81a8fa1c50dfb56fc096de/internal/domain/processing/processor.go#L281-L415)

可借鉴的是精确金额、成对记账影响、稳定身份和确定性日志。所读处理器使用Scope、
FSM及提案模型，本地使用SQLite事件和JSON投影；这里没有验证Formance完整的Raft
提交/恢复链路，也不能由其文件中的Raft字段推定本地复制协议具备同样保证。

## Seata：TCC的防重、空回滚与悬挂

该固定版本的入口是CommonFence/SpringFenceHandler；本次没有沿用其他版本中的
`TCCFenceHandler`路径。TCC配置把`useTCCFence`映射到`useCommonFence`，以下保证应
限定在启用并正确配置这条fence路径时。[配置映射](https://github.com/apache/incubator-seata/blob/cba73e180fd8896a99df6415b859d275122feaa7/tcc/src/main/java/org/apache/seata/rm/tcc/interceptor/TccActionInterceptorHandler.java#L149-L155)

| 情况 | 实际处理 | 固定源码 |
| --- | --- | --- |
| Try | TransactionTemplate内先插入TRIED记录，再调用业务callback；重复主键或异常使事务回滚 | [prepareFence](https://github.com/apache/incubator-seata/blob/cba73e180fd8896a99df6415b859d275122feaa7/spring/seata-spring/src/main/java/org/apache/seata/rm/fence/SpringFenceHandler.java#L116-L149) |
| 重复Confirm | 已COMMITTED直接成功；已ROLLBACKED或SUSPENDED返回失败；缺少记录拒绝提交 | [commitFence](https://github.com/apache/incubator-seata/blob/cba73e180fd8896a99df6415b859d275122feaa7/spring/seata-spring/src/main/java/org/apache/seata/rm/fence/SpringFenceHandler.java#L161-L210) |
| Cancel先于Try | 无记录则插入SUSPENDED并成功返回，不调用业务回滚；后到Try碰到同一主键，不能再执行业务callback | [空回滚与终态判断](https://github.com/apache/incubator-seata/blob/cba73e180fd8896a99df6415b859d275122feaa7/spring/seata-spring/src/main/java/org/apache/seata/rm/fence/SpringFenceHandler.java#L224-L275)、[复合主键](https://github.com/apache/incubator-seata/blob/cba73e180fd8896a99df6415b859d275122feaa7/script/client/tcc/db/mysql.sql#L19-L30) |
| 竞争二阶段 | 查询使用SELECT FOR UPDATE；更新要求旧状态TRIED，只有更新成功才调用业务方法；业务返回false标记回滚 | [锁与条件更新SQL](https://github.com/apache/incubator-seata/blob/cba73e180fd8896a99df6415b859d275122feaa7/integration-tx-api/src/main/java/org/apache/seata/integration/tx/api/fence/store/db/sql/CommonFenceStoreSqls.java#L52-L82)、[调用与回滚](https://github.com/apache/incubator-seata/blob/cba73e180fd8896a99df6415b859d275122feaa7/spring/seata-spring/src/main/java/org/apache/seata/rm/fence/SpringFenceHandler.java#L327-L358) |

同版本行为测试还检查了重复二阶段不调用业务、空回滚不调用业务、旧状态更新失败不
调用业务，以及false结果标记回滚；本次只阅读了这些测试，没有执行上游测试。
[行为测试](https://github.com/apache/incubator-seata/blob/cba73e180fd8896a99df6415b859d275122feaa7/spring/seata-spring/src/test/java/org/apache/seata/rm/fence/SpringFenceBehaviorTest.java#L136-L236)

这里的本地事务保证依赖fence表和业务数据库正确参与同一事务管理器。任意HTTP、
外部扣款或非事务资源不会因为这段Java callback自动获得数据库回滚语义。fence以
`xid/branch_id`防重，所读路径没有比较本地钱包所用的完整金额、账户和role意图，
不能拿分支状态直接替代本地完整回执校验。TCC fence也不同于本地epoch/leader隔离；
两者分别处理分支执行与旧协调者写入问题。

## 对本地实现的检查点

| 检查点 | 当前代码与约束 |
| --- | --- |
| 金额及扣款竞争 | [projection.py](../src/digital_wallet/projection.py)只处理校验后的整数美分；source Try把available移到held，拒绝不足；[shard.py](../src/digital_wallet/shard.py)在同一SQLite事务内执行校验、追加事件及更新投影 |
| 防重与内容冲突 | Shard.action按transaction_id查询历史，并同时核对完整intent和role；匹配重试返回已存参与者状态，内容不同拒绝。重放reducer则拒绝重复事实，不能把RPC重试再写一遍事件 |
| 空取消与迟到Try | cancel没有旧参与者也写CANCELED墓碑；后到prepare返回原终态。confirm必须有PREPARED；已CONFIRMED不能cancel。对应Seata空回滚/悬挂问题，但采用独立事件模型 |
| 全局决定与回执 | [coordinator.py](../src/digital_wallet/coordinator.py)、[contracts.py](../src/digital_wallet/contracts.py)将完整意图和角色回执与持久COMMIT/CANCEL决定关联；RPC结果未知应保留相同身份恢复，不能直接释放冻结款 |
| 事实重放 | projection.py与[coordinator_projection.py](../src/digital_wallet/coordinator_projection.py)从已记录事实重建状态；重放本身没有网络、时钟或新扣款。重建投影与恢复后继续未完成协议属于不同动作 |
| 资金守恒 | [audit.py](../src/digital_wallet/audit.py)核对参与者、事件及投影；跨片source已确认而destination未确认时把在途款计入总额，不能只相加available就误判丢款 |

这些源码对照是本地审查清单，不替代执行证据。本轮实际修改、图工具覆盖和测试结果
由[章节审查](chapter28-review.md)与[测试/性能报告](../benchmarks/REPORT.md)记录，不能把
旧报告的测试数量当作本轮重新验证的结果。

## 不能由开源参考补齐的生产边界

- 原书目标为1,000,000 TPS与99.99%可靠性。本地最多100个初始账户、1000笔受理命令，
  资金推进仍有串行点；上游使用Go、Java或并行框架不证明本地达到同样性能。
- 本地三副本仅复制协调日志，账户分片事件没有复制。2/3确认、人工epoch和固定单写者
  不等于Raft自动选举、完整账户持久性或权威原盘丢失后的安全提升。
- 本地快照仍核验完整历史，不能称为跳过历史扫描的快速启动；同进程投影和有界wait
  也不等于原书独立CQRS读服务、反向代理回推通道及跨主机HA。
- 选择TCC不要求把2PC、Saga全部实现，但原书每个账户分区的共识复制、在线分片迁移、
  独立读路径及目标规模验证仍未覆盖。Formance记账处理器与Seata分支fence各解决部分
  问题，二者相加不会自动组成这些能力。

项目持续使用合成余额，不连接银行或真实资金；内部事件守恒与外部银行对账是不同的
验证范围。这里没有审计或引入任何真实资金接入方案。
