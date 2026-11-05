# 第29章：开源源码对照与实现核对

日期：2026-11-05。依据根目录《System Design Interview.md》15039–15986行的
“Stock Exchange”。章节要求限价新单/撤单、价格—时间优先、资金预留、确定性恢复、
行情与成交报告，并进一步讨论低延迟、主备、共识复制和行情分发公平性。不能因为
已有订单簿或HTTP接口，就把这些生产架构和指标全部标为完成。

## GitHub检索与固定版本

通过GitHub连接器分别检索`exchange-core matching engine`和`CoinTossX`，从结果中
确认主仓库、默认分支及提交，再读取实际Java实现和测试；以下链接均固定到所读提交。

| 项目 | 固定提交 | 选读范围 |
|---|---|---|
| [exchange-core/exchange-core](https://github.com/exchange-core/exchange-core/tree/2f8548749839e9095c8dc597e4b61521d259fa5d) | `2f8548749839e9095c8dc597e4b61521d259fa5d` | Direct订单簿、撤单/价位链、磁盘指令日志与恢复测试 |
| [dharmeshsing/CoinTossX](https://github.com/dharmeshsing/CoinTossX/tree/89090edcd15a06f4ed821890adfc8f377ed7d7c7) | `89090edcd15a06f4ed821890adfc8f377ed7d7c7` | 价格—时间策略、B+树价位/订单列表、撤单及订单列表测试 |

只阅读和比较源码，没有安装、运行、压测或复制上游代码，也没有增加本地运行依赖。
这些固定版本有各自的策略和约束，不能将仓库描述、测试文件名或性能宣传当成已验证的
本地行为；这里也未审计两者的完整部署、安全、资金或容灾体系。

## exchange-core：价位索引、链和恢复

1. **先选最佳价，再沿链匹配。** `OrderBookDirectImpl`保存买卖价位树、订单ID索引和
   最佳买卖单指针；`tryMatchInstantly`从最佳对手单开始，以双方未成交量的最小值
   生成成交，更新价位总量，删除完全成交的订单及空价位，再进入下一单/价位。
   `insertOrder`把同价新单连到价位尾部。可借鉴的是明确维护索引、FIFO顺序和数量，
   不是每次重新排序历史订单。[匹配循环](https://github.com/exchange-core/exchange-core/blob/2f8548749839e9095c8dc597e4b61521d259fa5d/src/main/java/exchange/core2/core/orderbook/OrderBookDirectImpl.java#L230-L335)、
   [价位尾插入](https://github.com/exchange-core/exchange-core/blob/2f8548749839e9095c8dc597e4b61521d259fa5d/src/main/java/exchange/core2/core/orderbook/OrderBookDirectImpl.java#L485-L560)
2. **撤单同步维护身份与派生结构。** `cancelOrder`先从索引取单并核对uid，删除订单，
   调整链/价位，随后生成剩余量减少事件；未知或其他用户的订单被拒绝。其内部校验
   还把链中订单和订单ID索引相互核对。[撤单](https://github.com/exchange-core/exchange-core/blob/2f8548749839e9095c8dc597e4b61521d259fa5d/src/main/java/exchange/core2/core/orderbook/OrderBookDirectImpl.java#L338-L359)、
   [链及索引校验](https://github.com/exchange-core/exchange-core/blob/2f8548749839e9095c8dc597e4b61521d259fa5d/src/main/java/exchange/core2/core/orderbook/OrderBookDirectImpl.java#L585-L603)
3. **订单簿去重不等于客户端请求幂等。** 所读GTC路径先立即撮合，再检查剩余挂单ID
   是否已存在；源码注释明确重复ID可以成交但不能再次挂入余量。因此不能照搬这一
   局部检查来替代本地网关的请求键/内容校验与全生命周期订单ID约束。
   [先撮合再检查ID](https://github.com/exchange-core/exchange-core/blob/2f8548749839e9095c8dc597e4b61521d259fa5d/src/main/java/exchange/core2/core/orderbook/OrderBookDirectImpl.java#L124-L159)
4. **日志要保留重演所需输入。** `writeToJournal`受启用序号控制，跳过查询，记录
   指令类型、序号、原始timestamp及订单字段；`replayJournalFull`按文件读取指令并
   通过API恢复。所读`readCommands`遇到序号缺口仅记录警告，不能将其概括成本地
   所用的“缺口一律拒绝”。日志启用、缓冲刷新及恢复入口是分别需要核实的环节，
   文件中存在journal代码并不自动意味着每次受理均已耐久或具备跨机共识。
   [序列化输入](https://github.com/exchange-core/exchange-core/blob/2f8548749839e9095c8dc597e4b61521d259fa5d/src/main/java/exchange/core2/core/processors/journaling/DiskSerializationProcessor.java#L241-L320)、
   [回放与缺口处理](https://github.com/exchange-core/exchange-core/blob/2f8548749839e9095c8dc597e4b61521d259fa5d/src/main/java/exchange/core2/core/processors/journaling/DiskSerializationProcessor.java#L407-L512)

上游行为测试显式检查跨价成交及同价多单的成交次序/数量；恢复测试保存快照、执行后续
指令、重建实例，再比较状态哈希与余额汇总。本次只阅读这些断言，没有运行上游测试。
[成交测试](https://github.com/exchange-core/exchange-core/blob/2f8548749839e9095c8dc597e4b61521d259fa5d/src/test/java/exchange/core2/core/orderbook/OrderBookBaseTest.java#L440-L482)、
[快照加日志恢复测试](https://github.com/exchange-core/exchange-core/blob/2f8548749839e9095c8dc597e4b61521d259fa5d/src/test/java/exchange/core2/tests/util/JournalingTestsModule.java#L53-L95)

## CoinTossX：结构和策略应分开判断

价格—时间策略按B+树价位和对应`OrderList`遍历对手方，检查限价及可执行量，部分成交
后保留余量，完全成交时移除。它还含隐藏单、最小执行量、价格改善和熔断等额外规则；
不能把完整策略简化为本项目的单一可见限价单规则。
[策略与成交生成](https://github.com/dharmeshsing/CoinTossX/blob/89090edcd15a06f4ed821890adfc8f377ed7d7c7/MatchingEngine/src/main/java/crossing/strategy/PriceTimePriorityStrategy.java#L210-L315)

`OrderListImpl`采用堆外连续内存，按提交时间/隐藏属性比较，二分确定插入位置并移动
元素；删除也会移动后续元素。这与本地同价FIFO双向链接不同，不能宣称两者都提供
同样的常数时间撤单。所读撤单处理器按价位遍历并查找原客户端订单ID，也不是本地
订单ID节点索引的直接删除路径。
[列表插入/移除与比较](https://github.com/dharmeshsing/CoinTossX/blob/89090edcd15a06f4ed821890adfc8f377ed7d7c7/LimitOrderBook/src/main/java/leafNode/OrderListImpl.java#L49-L138)、
[撤单查找](https://github.com/dharmeshsing/CoinTossX/blob/89090edcd15a06f4ed821890adfc8f377ed7d7c7/MatchingEngine/src/main/java/crossing/preProcessor/CancelOrderPreProcessor.java#L28-L50)

还有两个不能直接套用的边界：

- 成交生成在策略内读取`Instant.now()`，使用实例`AtomicLong`递增tradeId。这段代码
  本身不能证明重新运行相同输入就能得到相同成交时间/ID；本地纯reducer从已定序
  输入派生时间与成交身份，应保留这种区别，而非引入实时钟或进程计数器。
- 同名`PriceTimePriorityStrategyTest`中的`testAddOrder`是空方法，不能当作上游撮合
  行为已有断言的证据。实际`OrderListTest.testOrder`会乱序插入三个不同时刻的订单
  并检查取出顺序；它验证列表顺序，并不验证完整交易所或等时间戳时的稳定FIFO。
  [空策略测试](https://github.com/dharmeshsing/CoinTossX/blob/89090edcd15a06f4ed821890adfc8f377ed7d7c7/MatchingEngine/src/test/java/crossing/strategy/PriceTimePriorityStrategyTest.java#L24-L27)、
  [实际列表断言](https://github.com/dharmeshsing/CoinTossX/blob/89090edcd15a06f4ed821890adfc8f377ed7d7c7/LimitOrderBook/src/test/java/leafNode/OrderListTest.java#L138-L167)

## 对本地实现的检查点

CodeGraph用于读取[order_book.py](../src/stock_exchange/order_book.py)和
[matching.py](../src/stock_exchange/matching.py)的当前源码；调用图辅助定位，实际
正确性仍由输入契约、状态不变量、重演比对及执行测试验证。

| 检查点 | 本地实现及本轮应保持的约束 |
|---|---|
| 价格—时间与部分成交 | 价格堆选最佳对手价，同价FIFO链接及节点索引；更新成交余量、价位总量和空价位。整体仍有全状态复制/审计开销，不能由链操作复杂度推导整笔处理复杂度 |
| 钱与持仓 | 买方按限价冻结现金、卖方冻结股份，按挂单价成交并返还买方价差；取消释放余量；自成交防护不应绕开旧单优先级 |
| 请求和订单身份 | [storage.py](../src/stock_exchange/storage.py)将请求键及内容与定序日志持久关联；不能借用上游订单簿局部去重来替代请求重试语义 |
| 确定性与提升 | 时间来自已定序指令，成交ID由指令序号/成交索引派生；人工提升检查完整候选状态与独立重放，不只检查seq/head或Python宽松数值相等 |
| 状态与快照比较 | [snapshots.py](../src/stock_exchange/snapshots.py)及审计/提升需要保留JSON类型差异；`false`、`0.0`与整数`0`不能因Python相等而被当作同一状态 |
| 行情和报告 | [market.py](../src/stock_exchange/market.py)消费有序批次；同ID订单身份字段不可改变、终态不可回退、filled不可倒退、remaining不可增加；不能仅凭总量相同接受改写 |
| 验证证据 | 本轮修改与原书覆盖矩阵见[章节审查](chapter29-review.md)，实际测试/演示/测量见[验证报告](../benchmarks/REPORT.md)；不使用上游空测试或旧本地数字代替当前验证 |

## 仍未实现的生产架构

本地是一个未复制的定序器、两个异步撮合副本和独立行情/报告进程，人工选择活动副本。
每股票有逻辑订单簿，但一个副本内仍统一串行处理账户与撮合；这不是每股票独立进程
扩展，也不是Raft、自动故障切换、定序器丢盘恢复或跨机容灾。快照仍验证完整历史，
不能称为跳过历史扫描的快速启动或日志裁剪协议。

本轮没有新增mmap、FIX/SBE、CPU绑核、独立出站事件总线、L3或等时多播。原书
十亿订单/日、毫秒级尾延迟和99.99%可用性是设计目标，不能从本机小样本或上游
实现方式推导已达到。账户、持仓、市场时钟和成交均为模拟数据，没有真实场所下单、
银行/清算结算或监管合规认证。
