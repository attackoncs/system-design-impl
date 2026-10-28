# 酒店预订设计：第23章

核心审查日期2026-10-26。功能映射见[章节记录](chapter23-review.md)，实际执行证据见
[验证报告](../benchmarks/results.md)；Python 3.10+标准库参考，全部支付为模拟。

## 服务和分片

实验有网关、两个分片HTTP、两个Saga worker和独立模拟支付账本六个进程。
网关/worker走HTTP，不直接打开分片数据库。酒店catalog、房型、日库存、订单、日价快照、
jobs/events同库，按hotel ID的规范JSON SHA256前64位取模路由。分片拓扑持久校验，
不支持直接改分片数或在线迁移；也无复制/自动故障转移。酒店搜索和具体房号分配不在范围。

## 库存、价格和预订

按房型预订，库存主键为酒店/房型/UTC日。上限是capacity+floor(capacity*overbook/100)，
overbook为0..10。待付款和已确认都占用。SQLite BEGIN IMMEDIATE、条件递增和CHECK共同
保护多夜事务；缺日、满房、报价不符或提交前退出使库存、订单、价格快照和charge intent
全部回滚。同分片只有一个写者，version字段不是已实现的行锁或乐观重试算法。

日期为[入住,离店)，1..30晚/1..10间、未来730个UTC日。USD整数美分，expected_total在
writer内重新核对；已接受日价不因后续调价改变。管理员不能把容量/超售上限降到占用以下。
软删除阻止新预订，保留历史订单与退款能力。缓存报价可过时，最终写入永远验证数据库。

## 查询、缓存与滚动日历

缓存有TTL/LRU、固定条带冷加载合并和返回值隔离。键含持久revision，loader用同一快照读取
版本/catalog/库存；独立Saga进程改库存也可失效。酒店级可用查询最多100活跃房型、30日期，
仅返回日历完整且每晚足量的房型并标advisory/revision，不逐类型发HTTP请求。

CalendarPolicies需管理员显式启用：capacity/overbook/price必填，horizon_days为1..730，默认730。
分片自有维护默认每5秒一轮、最多32策略/4096检查日期。writer外准备日期，短事务内检查UTC日、
持久generation/version/cursor后写入并推进。generation不复用，防删除重建ABA；同配置PUT不重置
进度。只插空日，既有手工日价、占用和历史库存一律保留；缩短范围/删除策略也不删除旧日历。
停用酒店/房型不补新行。内部roll接口执行同一有限实现，网关不公开转发。

## 身份和幂等

用户来自Bearer映射，不能由body冒充。订单ID由hotel+[user,key]摘要前128位组成，数据库
UNIQUE(hotel,user,request_key)+指纹绑定负载。同键重放返回现有订单当前状态，即使已取消/
过期或日期/价格变动也不重新占房；改内容409。缺失和他人订单都404。历史按酒店分页100项，
不是跨分片全局用户索引，也不是并发插入下的固定快照。本地admin可管理全部酒店，未做租户员工ACL。

## 支付Saga和资源拥有权

PENDING只表示占用。截止前全额模拟付款才CONFIRMED/PAID；明确拒付释放并拒绝，超时/HTTP
错误是未知结果，不能当作拒付。取消、过期、finish用held标志保证仅释放一次；晚到成功补偿退款，
不确认住宿。已确认不按hold过期。本参考全额无罚金取消，真实政策未实现。

charge/refund使用固定操作ID和冻结intent。claim先持久attempt/lease随机token；finish/retry
重查持久身份，替换后的旧worker不能覆盖。网络不占库存SQL事务。未知结果持续幂等重试，不丢进
死信后当作完成。每轮最多清100个过期hold，停机可能保守占库存到恢复。worker拥有stop/cancel/
drain；不支持取消的旧自定义adapter仍需等真实执行结束。模拟receipt/金额/状态经校验后结算。

模拟账本独立持久化指纹/回执；退款必须匹配原已付款。控制开关可模拟“已提交后503”和暂时退款
不可用，但不撤销已完成操作的幂等回执。这不是银行、真实卡SDK或真实资金exactly-once保证。
events是持久轮询日志，不是外部必达通知；回执/账务历史仍需生产GC策略。

## HTTP和生命周期

默认32活跃handler、128 backlog、10秒绝对socket I/O、1MiB请求/4MiB响应。gateway/shard
共用显式method/path/query契约，正文前鉴权；网关保持原身份、只转发公共路由、不盲目重试写。
严格JSON/framing、未知query/不支持正文拒绝；RPC不走环境代理/redirect，HTTPS正常验链。
过载回应有限drain，避免未读TCP正文使503被Windows RST覆盖。

CLI私有config≤1MiB，验证角色凭据独立、origin URL、拓扑和期限。绑定成功才start维护，关闭先
排空handler再停/join自有维护。错误脱敏、health/status可见并重试。socket期限不能强杀DNS/
SQLite/CPU，名额保留至实际退出。

## 尚未完成

复制/共识/自动HA、在线迁移、真实Redis/CDC、分布式限流、真实支付、TLS/租户ACL部署、酒店当地
时区/税费/多币种、隐私/备份恢复及5000酒店/100万房间持续验收均未实现。保留磁盘的进程测试不证明
机器丢失恢复或所有Windows文件系统掉电持久性。
