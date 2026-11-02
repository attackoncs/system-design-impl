# 第11章：原书、GitHub实现与本地代码核对

核对日期：2026-11-01。范围为仓库《System Design Interview.md》3177–3570行，即“Design A Notification System”。使用CodeGraph追踪受理、领取、投递、重试和确认链路，并执行回归与受控并发验证。

## GitHub检索与阅读

| 项目 | 已阅读的材料 | 本地参考方式 |
|---|---|---|
| [novuhq/novu](https://github.com/novuhq/novu) | [队列实现](https://github.com/novuhq/novu/blob/6c5b0ea30fe5a12e740db3f3c0fc635f5fcf75dc/libs/application-generic/src/services/queues/inbound-parse-queue.service.ts)、[幂等说明](https://github.com/novuhq/novu/blob/6c5b0ea30fe5a12e740db3f3c0fc635f5fcf75dc/docs/api-reference/idempotency.mdx) | 参考把重试策略放在队列层、明确区分受理去重与后续执行 |
| [notifo-io/notifo](https://github.com/notifo-io/notifo) | [调度通道源码](https://github.com/notifo-io/notifo/blob/54da324ff6f4c51ca959751bdc6572e20c10a6e4/backend/src/Notifo.Domain/Channels/SchedulingChannelBase.cs)、[架构文档](https://github.com/notifo-io/notifo/blob/54da324ff6f4c51ca959751bdc6572e20c10a6e4/docs/architecture.md) | 参考通道/调度器/存储分层，以及发送前再次判断任务是否已处理 |

Novu所读的入站邮件队列共享尝试次数和指数退避配置，由队列后端安排重试；其API幂等说明还区分处理中的重复与已完成的缓存响应。本地继续采用已有event_id账本，重复通过异常携带原通知ID，并未照搬其HTTP响应语义。

Notifo的调度通道先过滤已处理任务，再调用发送契约；仓储与渠道集成分层。这支持继续维护本地Provider和队列契约，而不把供应商、存储和受理逻辑耦合在一起。本轮没有复制外部源码、安装这些框架或发送真实通知；参考链接固定到检索得到的提交。

## 原书覆盖与真实边界

| 原书要求 | 本地执行路径 | 状态 |
|---|---|---|
| iOS、Android、SMS、Email | Channel、Provider及每通道worker | 四通道契约和模拟投递已实现；真实APNs/FCM/短信/邮件适配未提供 |
| 用户联系方式、多设备 | ContactInfoStore、平台token筛选、端点快照 | 已实现进程内版本；共享用户/设备数据库与缓存未提供 |
| 异步受理与解耦 | NotificationService → MessageQueue/SQLiteMessageQueue → WorkerPool | 已实现；SQLite单机持久化，不是复制消息代理 |
| 高并发受理与独立通道worker | 有界send_batch、用户条带锁、通道worker池 | 默认最多16个批量消费者；通知级并发，单条多设备仍顺序投递 |
| 通知不丢、日志、重试 | 接受事务、租约、终态确认、SQLiteNotificationLog | 进程退出可恢复，至少一次；整机磁盘损坏和跨机房容灾未实现 |
| event ID去重 | event_id/用户/通道唯一账本 | 跨实例、重启、终态后保留；外部发送副作用仍需供应商幂等 |
| 模板复用 | 模板注册表、渲染后快照正文与参数 | 已实现；模板共享数据库/缓存未实现 |
| 尊重退订 | 接受时、每次provider调用前检查 | 已实现；默认偏好在内存，重启需重建 |
| 频率限制 | 用户级、用户通道级窗口 | 已实现单service实例版本；global不是全系统额度 |
| 指数退避与永久失败 | RetryHandler、延迟队列、FAILED记录 | 已实现；外部告警投递未集成 |
| 安全API | HTTP bearer鉴权、JSON对象/大小校验 | 内部共享key演示；TLS、租户授权、厂商回调签名需部署集成 |
| 队列监控 | depth、in_flight、HTTP analytics | 已实现本地快照；监控平台和自动扩容未提供 |
| 送达/打开/点击 | tracker及经过鉴权的事件入口 | 进程内生命周期；SENT仅表示provider接受，回调状态重启重建未实现 |

完整历史覆盖表见[章节核对记录](chapter11-review.md)。原需求中真实provider extras、持久偏好、平均投递延迟、自动日志清理等目标不能视为当前已完成能力；中英需求和任务清单已分别标注。

## 本轮发现并修复的问题

### 旧领取引用能够控制新任务

内存队列原先对同一个NotificationTask原地赋新receipt。旧领取重排后再被领取，旧引用也看到了新receipt，导致凭据隔离失效。现在每次claim返回独立任务快照；旧引用的ack/reschedule被拒绝，renew返回False，不影响新领取者。

验证：[旧凭据三种操作](../tests/test_queue_reliability_review.py)。只按notification_id判断身份不够，领取身份必须随每次claim变化，且不能通过共享可变对象泄漏。

### SQLite锁等待消耗了租约

原claim在申请写锁之前读取时间，锁等待超过租期时，刚返回的任务已经过期。原renew同样可能用旧时间判断有效性，错误续租已经过期的领取。现在两者先取得 `BEGIN IMMEDIATE`，再采样当前时间并判断/设置租约。

验证使用真实SQLite写锁、线程事件和可控时钟，不依赖碰巧sleep足够久：[领取与续租竞争](../tests/test_queue_reliability_review.py)。租期仍需覆盖正常数据库提交及调度开销，不能设置得无限小。

### 复用Worker读到了上次的启动信号

原Worker退出后未清理 `_started`，下一次wait_started可能在新start真正运行前就返回，紧接着的stop被新start覆盖。现在正常退出和取消退出都在finally清理启动信号。

验证：[连续启停与等待下一轮](../tests/test_worker_lifecycle_review.py)。WorkerPool仍按原契约管理自己的任务，不引入新的生命周期框架。

### 批量受理串行等待

`send_batch(requests, *, concurrency=16)` 使用固定数量消费者共享输入迭代器，而非每条请求创建任务后再套Semaphore。结果仍按输入位置返回，失败项为空串。单用户条带锁和数据库事件唯一性继续守住去重；`concurrency=1`提供串行模式。

取消批次会取消并等待所有子任务退出；已经受理的通知保留，不能把取消理解为撤销整批。业务方应使用稳定event_id处理响应不确定时的重放。验证：[任务数上限、保序、取消、first-wins](../tests/test_batch_admission_review.py)。

## 验证结果

- 原447项测试加本轮19项回归，**466 passed**；本轮新增测试在修复前均失败。
- 队列可靠性5项、Worker复用4项、批量受理10项覆盖上述问题；全套包含真实SQLite、子进程退出恢复、HTTP鉴权到worker投递等已有链路。
- 严格警告模式下执行；pytest-asyncio显式使用function fixture loop scope。直接从源码运行时向子进程传入绝对PYTHONPATH，避免把导入环境错误误判为业务失败。
- [并发验证与性能对照](../benchmarks/REPORT.md)区分受理和投递、模拟等待和真实SQLite，不把单机结果外推为原书每天千万条推送容量。
- 收尾检查：42个Python文件通过3.9语法解析，17份文档的499个本地链接有效；README持久化示例实际运行成功，两张学习图成功渲染。Ruff关键错误/格式、学习文档生成一致性与diff空白检查通过；代码图已增量更新并分析四个源文件的影响范围。
