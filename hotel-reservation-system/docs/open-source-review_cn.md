# 第23章：开源源码对照与实现核对

日期：2026-11-04。依据根目录《System Design Interview.md》第23章“Hotel Reservation System”。

结论：房型/日历、原子预订、超售边界、幂等及模拟支付补偿核心已有实现。本轮修复
上游坏响应误报客户端错误的缺口，100项完整回归通过；库存与Saga专项未发现其他
确认缺陷。真实支付、复制HA及原书规模仍为后续扩展。

## GitHub开源参考

通过GitHub连接器分别检索酒店预订实现和订单Saga示例，并按以下固定提交阅读实际
源码。DeathStarBench用于业务路径、缓存与库存边界对照；Eventuate示例只用于跨服务
步骤及补偿组织。未构建、运行或压测上游项目，未复制代码或增加运行依赖。

| 项目与固定版本 | 实际阅读的源码 | 可对照的机制 |
| --- | --- | --- |
| [DeathStarBench / hotelReservation](https://github.com/delimitrou/DeathStarBench/tree/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation) | [MakeReservation](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/services/reservation/server.go#L94)、[容量判断与缓存更新](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/services/reservation/server.go#L179)、[逐晚写入](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/services/reservation/server.go#L199) | 按入住/离店日期逐晚检查；先查Memcached预订数和容量，缺失时读取MongoDB。全部检查成功后更新计数缓存，再逐晚插入预订文档。 |
| 同上 | [CheckAvailability](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/services/reservation/server.go#L221)、[批量缓存读取](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/services/reservation/server.go#L237)、[容量结果合并](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/services/reservation/server.go#L315)、[前端服务组合](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/services/frontend/server.go#L35) | 对多个酒店/日期组织缓存键，GetMulti读取，缺失分支通过goroutine/channel查询数据库并合并结果。前端组合search、profile、user、reservation等独立gRPC服务。 |
| [Eventuate订单Saga示例](https://github.com/eventuate-tram/eventuate-tram-sagas-examples-customers-and-orders/tree/894f2dbbe9aba2be6129748b6bbf5f30356deb07) | [CreateOrderSaga步骤及补偿](https://github.com/eventuate-tram/eventuate-tram-sagas-examples-customers-and-orders/blob/894f2dbbe9aba2be6129748b6bbf5f30356deb07/order-service/order-service-sagas/src/main/java/io/eventuate/examples/tram/sagas/customersandorders/orders/sagas/CreateOrderSaga.java#L31)、[本地事务创建Saga](https://github.com/eventuate-tram/eventuate-tram-sagas-examples-customers-and-orders/blob/894f2dbbe9aba2be6129748b6bbf5f30356deb07/order-service/order-service-sagas/src/main/java/io/eventuate/examples/tram/sagas/customersandorders/orders/sagas/OrderSagaService.java#L23) | DSL显式组织本地create、远程reserveCredit、本地approve；create步骤注册reject补偿。业务回复区分未知客户与额度不足，保留拒绝原因。入口方法在本地@Transactional边界创建Saga，再返回订单。 |

### 业务示例的并发保证需要另外证明

DeathStarBench提供真实酒店业务代码和微服务调用路径，但选读的MakeReservation是
“读取占用/容量→检查→更新缓存→逐晚插入”。**仅从此函数不能推定多夜原子提交或
并发防超卖**：所读路径未展示覆盖全部夜晚的数据库事务、条件原子扣减或稳定请求
幂等身份。本轮没有运行其并发工作负载，也没有审计所有部署配置，故不把它评为
经过验证的预订一致性方案。

该实现的容量主要按酒店组织；本地库存维度为hotel/room-type/day，且待支付占用、
最多10%超售、报价复核、取消/过期和退款相互关联。可借鉴查询职责和批量缓存读取，
不照搬缓存中的“先查再写”作为最后一间房的最终仲裁。

Eventuate选读示例是订单/客户额度，**不是酒店或真实支付实现**。Saga定义清楚表达
本地步骤、远程命令及补偿；但不能由此声称本地具备该框架完整的消息/协调协议，
也不能把reject订单解释为已经实现酒店扣款后的退款。此处未审计框架运行时、所有
参与者或重试/崩溃持久化实现。

## 用于本地审查的检查点

1. **报价与占房不是同一个操作。** 缓存/可用量只是参考，最后提交必须重新检查
   每晚库存和expected_total；任一晚缺失、不足或价格改变，不能留下部分占用。
2. **同一事务覆盖全部库存影响。** 对照上游逐晚业务路径，核对本地订单、每晚价格
   快照、reserved及支付意图是否一起提交。10%超售应按整数floor计算，不能靠读取
   可用量后在事务外加一；取消和过期也只能释放一次。
3. **缓存加速读，不授予预订权。** 核对revision与内容是否来自同一读快照，外部任务
   改库存后缓存能否失效、返回对象是否隔离，以及最终预订是否绕过缓存回数据库。
   上游有GetMulti或并发分支，不证明任意请求数量都受到工作预算约束。
4. **步骤失败与结果未知分开处理。** Saga示例明确本地create/approve/reject职责。
   本地支付超时不能直接视为拒付；应保留相同operation/内容重试。取消或过期后才
   得知扣款成功时，应持久安排退款，不能把订单重新确认。
5. **幂等身份和领取身份不能混淆。** 同用户/酒店/key应重放原订单，旧key不能重占房；
   worker lease/token只表示当前任务拥有者，旧执行结果不能覆盖新领取者。金额、
   booking、operation、action及币种均应与冻结意图匹配。
6. **后台日历与资源生命周期也要验收。** 补日历只填空日，不覆盖人工价格、既有占用
   或订单快照；更新/删除重建策略后，旧计算不得发布到新generation。网络任务有界，
   绑定成功后才启动，关闭排空已接受工作，不能由微服务数量推定生产容量。

## 与本地参考架构的差异

覆盖范围和生产扩展以[章节映射](chapter23-review.md)、[设计](design_cn.md)及
[验证报告](../benchmarks/results.md)为准；选读上游源码不能替代本地执行证据。

- 本地按酒店哈希到固定SQLite分片，同片多夜订单/库存采用SQL事务、条件更新及CHECK。
  DeathStarBench选读的是MongoDB/Memcached/gRPC业务路径，不能直接互换存储协议，
  也不能把本地version字段称为上游或完整OCC实现。
- 本地缓存为进程内有界缓存加持久revision，不是Memcached/Redis/CDC；酒店内跨房型
  查询也不等于上游按位置搜索或推荐。酒店搜索和具体房号分配仍不在本章收窄范围内。
- 本地支付是独立模拟账本和持久任务，Saga补偿保证的是明确的本地演示边界；
  Eventuate示例的远程额度预留不等于扣款/退款或任意外部系统恰好一次副作用。
- 本地静态分片、共享单写者及非复制支付账本仍有限制。微服务示例或Saga DSL的存在
  不能证明自动故障转移、跨机持久性、生产吞吐或真实资金安全。

真实支付、酒店当地时区/取消罚金/税费/多币种、员工按酒店授权、完整前端、全局用户
索引、缓存/CDC、复制/共识/在线迁移、TLS/限流、隐私与回执GC、备份恢复及原书规模
持续验收仍需独立实现和验证。本轮本地代码、测试和文档结果由验证记录补充。

## 本地覆盖与源码路径

完整对照见[章节映射](chapter23-review.md)。本轮分别复核库存/日历、支付Saga和
网关/传输，保留已验证的模块边界，仅修改确认的响应错误分类。

| 章节链路 | 本地实现与关键约束 | 验证范围 |
| --- | --- | --- |
| 酒店/房型/日价格 | [Catalog](../src/hotel_reservation/catalog.py)、[available_rooms](../src/hotel_reservation/catalog_queries.py)固定revision/active/内容同快照 | 房型而非房号；最多100源房型，报价只是参考，不是预订权 |
| 多夜库存与幂等 | [Bookings](../src/hotel_reservation/bookings.py)条件更新/CHECK，多夜库存/订单/价格快照/charge intent同事务 | 锁后价格和日期校验；原键重放先于新订单日期校验；取消只释放一次 |
| 滚动库存 | [CalendarPolicies](../src/hotel_reservation/calendar_policy.py)只补空日，检查generation/version/cursor/UTC日 | 730天×两房型真实后台补齐、人工日价保留、旧roller防ABA |
| 缓存 | [Cache](../src/hotel_reservation/cache.py)固定条带冷加载合并，持久revision和返回值隔离 | 进程内有界参考；最终预订仍按数据库约束复核，无Redis/CDC |
| 支付/补偿 | [Saga](../src/hotel_reservation/saga.py)、[contracts](../src/hotel_reservation/payment_contracts.py)、[模拟账本](../src/hotel_reservation/payment.py)冻结意图/回执、lease token和operation幂等 | unknown不转decline；取消/过期后成功退款；无真实资金/外部恰好一次承诺 |
| worker/网关 | [Worker](../src/hotel_reservation/worker.py)、[HTTP](../src/hotel_reservation/http.py)、[transport](../src/hotel_reservation/transport.py)总预算、单次转发、协议校验、取消及排空 | 实际本机HTTP/六独立进程与受控peer分别标明；SQL/DNS/CPU不受socket强杀 |

### 修复：坏上游响应保留结果未知语义

网关转发一个有效写请求后，上游2xx响应可能有损坏JSON、重复字段、非有限数、过深
嵌套或截断长度。原先ValueError/RecursionError会落入HTTP客户端参数错误分支，返回
400，无法准确表达分片可能已经完成写入。

最小修改集中在transport.request：仅在response已存在时，将这些响应校验错误归为
通用OSError，再复用网关既有503/原键重试路径。未收到响应时的本地配置/编码校验
保留ValueError；HTTPError不在此捕获中，明确的409等业务4xx继续保留。连接、响应及
Deadline线程仍在finally清理，网关不新增自动写重试，worker也不把未知错误视为拒付。

新增[HTTP回归](../tests/test_http_reliability.py)用真实网关PUT和受控TCP HTTP peer
复现上述5类响应，修前全部400而非503，修后脱敏503且每个写请求只转发一次。
同时保留本地无效请求编码的ValueError，并将直接坏响应断言收紧为OSError。
该回归不模拟真实预订提交；持久预订幂等及库存/支付状态仍由Store与真实独立进程
场景另行验证。

### CodeGraph与独立复核

使用CodeGraph读取当前库存、日历、Saga/账本、worker、路由/HTTP及传输源码。独立
库存与Saga复核未发现需修改的确认缺陷；传输错误边界另经独立只读复核。
图增量刷新重解析3文件、更新91节点/1097边，无错误；这些是更新量，不是全库总量。
限定transport的直接调用影响分析为17文件内节点、46个两跳影响节点、额外22文件，
109条边截断及558处未解析调用。

动态分派及图缺边仍以源码和执行交叉确认，图统计不是完整测试覆盖；其他章节未提交
更改继续保留，不将全库差异当成本章独立风险结论。

## 本轮执行结果

Python 3.13.9 / Windows 11，标准库/SQLite。修前99项通过、68.935秒；修后完整
**100项通过、33.604秒，无跳过，所有警告视为错误**。HTTP/RPC从17增至18项，其他
测试分组不变。初始基线与专项复核可能共享主机资源，不能把两次时长解释为提速。
[完整命令与报告](../benchmarks/results.md)。

两个六进程示例均通过，全部为模拟支付：

- network_demo同键返回同ID，两晚20000美分确认观察值409.51ms；取消后退款完成，
  2次账本操作、可用量恢复11。
- [章节示例](../benchmarks/features-2026-11-04.json)验证后台730天×2房型、管理员权限、
  同策略version、数量筛选、人工12000日价、44000订单快照及退款后11间，停策略保留库存。

[热点基准](../benchmarks/review-2026-11-04.json)：16客户端100请求、20物理房间/10%
超售/两晚，接受22单、容量冲突78单；每晚reserved=22，唯一模拟charge=22，全部订单
读回CONFIRMED。受理1.765秒、全部settled 2.698秒；56.65尝试/秒与8.15成功单/秒
含义不同。p50/p95为51.072/921.567ms，包含预期拒绝，不代表成功订单支付时延。

最终回归、示例和基准顺序执行，基准未与测试/图构建并发；短时单热点不证明持续或
跨分片扩展能力。自建进程已停止，诊断配置/磁盘/日志留在忽略的.runtime。中英文
README、设计、需求、任务、根索引和学习指南同步，新增未知响应/原键重试流程图。

静态验收：src及修改测试的Ruff E9/F通过，两个修改Python文件格式通过；31个Python
文件通过3.10语法解析。16份Markdown的534个本地链接有效，基准JSON可解析，27份
学习指南生成校验通过；本章3张Mermaid图已实际渲染，限定范围的diff空白检查通过。
