# 第5章生产代码结构复审 — 2026-11-09

本轮只做src/结构与质量重构，不改测试/基准，不改HTTP状态与头、Lua语义、
fail-open/closed策略、错误类型和存储键布局。先读本文件、设计文档与README，
保留下文记录的有意决策（v2状态键、接纳日志、单Redis主节点等）。

## 审计发现

| 位置 | 问题 |
|---|---|
| decorators.py / middleware/configuration.py | 算法名解析与错误信息逐字重复 |
| core.py / middleware/configuration.py | key_type到提取函数的映射表重复两份 |
| core.py | `capacity or limit`出现5次；fail-open/closed结果内联在异常分支；三处1..8192键校验重复；解析器名为超长f-string；无规则早返回与`_most_restrictive_result`空结果重复；`reload_rules`局部导入 |
| 五个算法 | check()都重复“拼参数→追加时钟TTL→execute_algorithm→from_storage”；两种桶重复速率与时钟TTL公式 |
| 桶/固定窗口/日志的Lua与Python回调 | 允许/拒绝两个分支重复相同的SET/EXPIRE写入；滑动计数Lua用逐行字符串拼接，难读 |
| storage/memory.py | “过期即删除”在get_unlocked与_increment_raw重复；时钟键后缀`:clock-v2`在内存/Redis各写一遍 |
| rule_files.py | read_rules与RuleReloader.poll重复64KiB有界读取 |
| middleware | Flask钩子在两个分支重复写g；两个create_rate_limited_app为一条规则构造RateLimitConfig中间对象；FastAPI头开关条件重复 |

锁边界审计：配置快照在RateLimiter锁内复制、存储IO在锁外；内存算法在存储RLock内
采样时钟；RuleReloader锁→limiter锁单向获取，无反向路径。未发现需要修改的并发问题。

## 改动

- config.py：新增`parse_algorithm()`（原错误信息不变）与`RateLimitRule.effective_limit`属性。
- keys.py：新增`KEY_TYPE_FUNCTIONS`，核心与build_rule共用。
- core.py：`_checked_key`/`_callable_name`/`_rule_applies`/`_failure_result`小函数；
  算法类型表`_ALGORITHM_TYPES`；删除冗余早返回；顶层导入read_rules。
- algorithms/base.py：`BaseAlgorithm._execute()`统一执行与结果校验；
  `bucket_parameters()`与`window_clock_ttl()`集中TTL公式。
- 五个算法：check()缩为一次`_execute`调用；Lua与Python回调把写入提到分支之外，
  滑动计数Lua改为三引号脚本，日志解析提取`_parse_log`。键名与返回值逐项不变。
- storage：`clocks.clock_key()`；内存`_drop_if_expired()`替代`_is_expired`；
  Redis `execute_algorithm`补类型和文档。
- rule_files.py：`_read_bounded()`。
- middleware：`build_rule`直接构造配置；FastAPI `_headers_enabled()`；Flask钩子简化。
  `middleware.flask/fastapi.RateLimitConfig`继续作为兼容再导出。

## 有意保留

路由级Flask `rate_limit`仍返回固定429且正文不含retry_after；通用装饰器仍返回
字典；算法内`time.time()`仍作为自定义后端的客户端时间（测试也在模块上打补丁）；
`{key}:{window}:{limit}:...`冗余键段保留以免重置线上配额；`rate > 0`防御保留。
所有公共导入路径不变。

## 验证

重构前后均为265项通过（`-W error`）。另以旧/新代码对比：五算法×25组随机
规则×40步，在fakeredis上直接执行新旧Lua（固定ARGV时间）返回值完全一致
（10000次）；内存后端注入时钟时五算法新旧结果逐项一致。src行数3341→3077。

# 第5章复审记录 — 2026-10-27

后续的2026-10-31复核见[章节覆盖与开源代码对照](open-source-review_cn.md)，
包含最新265项测试、字符串键/深层JSON修复及Redis脚本缓存；以下保留历史记录。

原书《System Design Interview.md》923–1507行，Design A Rate Limiter。
先读章节，再用code-review-graph定位core/config/storage到算法、装饰器和
两个HTTP适配器的依赖，逐项检查源码并执行测试。

| 章节内容 | 本轮实现/证据 |
|---|---|
| 五算法及突发容量 | 保留全部算法；桶增加独立capacity，速率仍limit/window |
| 漏桶FIFO/固定出队 | 修正虚拟占用和重试公式，新增独立有界LeakyBucketQueue |
| 每IP/用户/接口规则 | key_type生效，URL无查询路径glob，规则/身份稳定隔离 |
| 配置文件与worker | 严格64KiB/100条JSON，原子发布完整快照，非daemon轮询 |
| 超额返回/头 | Flask/FastAPI共享格式，默认429/可配置状态、整数Retry-After |
| Redis共享计数/竞争 | 单EVAL含TIME/高水位，四进程屏障/客户端偏时验证 |
| 监控策略是否有效 | 固定checks/allowed/denied/storage_errors，reload计数与错误 |
| 性能优化讨论 | 有界清理/连接池，真实比较；整体吞吐回归已报告 |
| 边缘/多地域/最终一致同步 | 部署建议，未实现跨地域全球配额或边缘路由 |

## 修复的行为问题

原先忽略直接构造规则的key_type，path_pattern只是元数据；相同参数的接口
规则共用计数。现在按路径筛选、完整策略身份隔离，同一请求内去重相同规则。
构造/更新/读取配置不暴露规则可变引用，请求快照不在IO期间持锁。

原Python hash随机化使凭证身份跨进程变化；改用SHA256。默认信任转发头
允许客户端伪造不同IP，现在默认只取对端，显式可信代理选择仍可用。
路径不包含查询参数，避免同一路由随query变化产生新配额。

旧算法在进入存储锁之前采样时间，回退后更新旧时间会多补令牌/重置窗口。
内存锁内采样并钳制高水位；Redis用脚本内TIME。状态存在期间不因回退
复位额度；回退偏差加入响应延时。时钟非法值在变更前失败并记入指标。

漏桶原条件occupancy<capacity允许小数占用时超容量；改为+1仍能放入，
重试按实际缺少空间计算。存储结果新增严格协议验证，异常按失败策略处理。
原算法不真正排队执行，现在明确准入与独立FIFO执行两种用途。

内存增加100000键上限及有限扫描，不通过逐出有效配额换空间。Redis连接
上限64、默认连接/读取2秒、可配置；关闭非幂等扣额的自动命令重试。
响应丢失仍可能留下不确定扣额，不能等同恰好一次。

HTTP两个适配器统一状态/头开关，拒绝头使用整数秒；通用装饰器仅返回字典
或抛异常，不宣称直接生成框架HTTP状态。异步存储调用保留线程池卸载。

## 测试和图证据

基线205项/10.10秒；最终249项/11.50秒。新增路径/身份/规则隔离、配置
所有权、五算法并发与回退、锁内时钟、小数漏桶、坏结果、容量/清理预算、
热更新失败保留、FIFO满队列/节奏/关闭、双框架响应策略、跨Python进程哈希和文档示例实际启动。
修复Flask示例用扩展实例覆盖包名、导致后续装饰器加载失败的问题。
独立真实Redis8.0.5实验五算法各128次/4进程，恰好放行25，存储错误0；
双HTTP共32次，允许9/拒绝23。原示例及新策略/FIFO示例通过。

初始图影响分析五文件44直接节点/100影响节点、13附加文件、1478未解析调用。
中途更新38文件/484节点，无构建错误；八文件影响90直接/106影响节点、
26附加文件、1815未解析调用，返回结果有截断。
核心复查于2026-10-27T17:37:53增量构建8文件/64节点/542边更新；
示例修复后17:41:05再增量更新6文件/62节点/502边，均无错误；
八文件影响89直接/106影响节点、26附加文件，1815未解析，仍有截断。新rule_files/queue/responses/clocks模块未被
当前图索引，0结果不能证明没有依赖；用源码导入、AST和实际执行补核。
没有为图工具暂存文件，也没有提交/推送。

## 迁移与实现边界

核心v2状态键会重置旧配额，变更规则/容量/名称也产生独立状态，滚动混跑
旧新节点会拆分配额。升级前协调发布；新前缀不会替你迁移旧状态。
旧config/Rule对象的直接修改不再动态生效，使用update_rules/reload_rules。
路径去query、代理默认不信任、auto_headers=False对拒绝头也生效均为
公开语义变化；capacity对桶响应limit表示容量。

滑动日志采用**接纳请求日志**，拒绝不追加，避免攻击流量导致无界日志；
这与书中拒绝也入日志的示例不同。滑动计数仍近似，固定窗口仍有边界突发。
多个规则不是一个事务；自定义函数/回调为可信应用代码且须有界执行。

提供单Redis主节点，不支持Redis Cluster/自动HA。服务端大跳时、状态
重启/丢失、TTL过期、管理员修改可能恢复配额。默认fail_open允许存储失败
时通过，容量满也属于失败；需要严格拒绝时显式fail_open=False。
凭证工具不提供认证，监控没有外部采集/看板/告警。FIFO为本地非持久队列，
不冒充跨地域队列、网络整形器或生产级完整限流平台。

[设计](design_cn.md) · [测量及所有原始记录](../benchmarks/REPORT.md)
