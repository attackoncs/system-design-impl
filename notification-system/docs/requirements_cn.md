# 需求文档

## 引言

依据《System Design Interview》第11章的Python asyncio参考实现，提供iOS、Android、SMS、Email通道契约和模拟投递，以及受理、按通道排队、Worker、模板、偏好、限流、去重、重试和跟踪。内存后端用于进程内演示，可选SQLite保存待处理任务、事件ID账本和审计。核心运行时零依赖，真实APNs/FCM/SMS/SMTP适配与对应extras未提供。标准库HTTP示例调用实际服务和后台Worker。

## 实现状态与验收范围

保留原需求编号。未特别标注的条目描述当前参考实现的契约；标注“部分实现”或“部署目标”的条目不能视为已完成。完整运行结果和边界见[第11章审查记录](chapter11-review.md)。性能数值是待按环境测量的目标，不是默认模拟Provider配置下的容量保证。

| 范围 | 当前状态 |
|---|---|
| 批量受理、四通道模拟、可调度队列、Worker和重试 | 已实现；批量与投递分别限制并发 |
| SQLite任务/事件账本/审计、receipt隔离和租约恢复 | 可选已实现；单机文件数据库，不是复制型消息代理 |
| 用户偏好、联系人、模板、实时tracker和内容TTL去重 | 当前进程内存，启动/恢复时需要重载元数据 |
| 真实渠道适配及extras、共享偏好、厂商签名回调、集群限额和容灾 | 部署目标，未实现 |

## 术语表

- **Notification_Service**：核心编排器，负责校验通知请求、查询用户偏好并将通知分发到相应的消息队列
- **Message_Queue**：进程内异步队列，用于缓冲通知任务，并为每个通知通道（iOS、Android、SMS、Email）维护独立队列
- **Worker_Pool**：异步 Worker 池，从消息队列消费任务并通过第三方服务提供商投递通知
- **Worker**：单个异步消费者，从队列中拉取通知任务并调用相应的 Provider 完成投递
- **Provider**：抽象接口，表示第三方通知投递服务（APNs、FCM、SMS 网关、Email 服务）
- **Simulated_Provider**：默认的 Provider 实现，以可配置的延迟和失败率模拟投递过程，用于测试
- **Notification_Template**：预格式化的通知结构，带有命名占位符，可使用用户特定参数进行渲染
- **Rate_Limiter**：在可配置时间窗口内对发送给指定用户的通知数量施加频率上限的组件
- **Deduplicator**：基于内容哈希和收件人，在可配置时间窗口内检测并抑制重复通知请求的组件
- **Notification_Settings**：按用户、按通道的偏好存储，记录每个通知通道的 opt-in 和 opt-out 状态
- **Retry_Handler**：以指数退避的方式将失败的通知任务重新入队，最多重试到可配置的最大次数的组件
- **Event_Tracker**：记录每个通知生命周期事件（created、queued、sent、delivered、failed、clicked、unsubscribed）的组件
- **Notification_Log**：所有通知尝试及其结果的持久化记录，用于审计和分析
- **Contact_Info**：用户联系数据，包括邮箱地址、电话号码以及用于推送通知的设备令牌
- **Device_Token**：移动设备注册接收推送通知的唯一标识符
- **Notification_Request**：表示发送通知请求的数据结构，包含收件人、通道、内容和元数据
- **Notification_Status**：通知生命周期状态的枚举（CREATED、QUEUED、SENDING、SENT、DELIVERED、FAILED、CLICKED、UNSUBSCRIBED）

## 需求

### 需求 1：通知服务编排

**用户故事：** 作为开发者，我想要一个统一的通知服务来校验和分发通知，这样就可以通过单一的统一接口发送通知。

#### 验收标准

1.1 当提交一个带有有效收件人和通道的 Notification_Request 时，Notification_Service 必须校验请求、检查用户偏好，并将通知入队到相应的通道专属 Message_Queue。

1.2 当提交一个 Notification_Request 时，Notification_Service 必须查询收件人的 Notification_Settings，如果用户已选择不接收指定通道的通知则拒绝该通知。

1.3 当提交一个 Notification_Request 时，Notification_Service 必须在入队前将请求经过 Rate_Limiter 处理。

1.4 当提交一个 Notification_Request 时，Notification_Service 必须在入队前将请求经过 Deduplicator 处理。

1.5 如果 Notification_Request 存在无效收件人（指定通道缺少 Contact_Info），则 Notification_Service 必须抛出带有描述性信息的 ValidationError。

1.6 如果 Notification_Request 指定的通道是收件人已选择不接收的，则 Notification_Service 必须拒绝该请求并记录一条状态为 UNSUBSCRIBED 的 Event_Tracker 条目。

1.7 Notification_Service 必须提供 `await send_batch(requests, *, concurrency=16)`，按输入顺序返回每一项的通知ID或失败空字符串。

1.8 当通知成功入队时，Notification_Service 必须返回一个唯一的通知标识符用于状态追踪。

1.9 `send_batch` 必须使用至多 `concurrency` 个固定受理协程，而不是为每条输入创建任务；`concurrency=1` 保留串行受理。结果保序不等于整个批次原子提交或跨收件人投递保序。

1.10 单项受理失败保留该位置的空字符串，其他项继续受理；成功项返回其通知ID。

1.11 批量调用取消时，必须取消并等待内部受理协程退出，然后传播取消；已经成功受理的任务不会被回滚或撤销。

### 需求 2：消息队列系统

**用户故事：** 作为开发者，我希望每个通知通道都有独立的进程内消息队列，这样每个通道就可以通过适当的并发度独立处理。

#### 验收标准

2.1 Message_Queue 必须为每个通知通道维护独立的内部队列：iOS 推送、Android 推送、SMS 和 Email。

2.2 Message_Queue 必须支持使用 Python asyncio 原语的异步入队和出队操作。

2.3 Message_Queue 必须支持每个通道配置可配置的最大队列深度。

2.4 如果队列达到最大深度，则 Message_Queue 必须拒绝新的入队操作并抛出 QueueFullError。

2.5 Message_Queue 必须提供方法来查询每个通道队列的当前深度和待处理数量。

2.6 Message_Queue 必须按任务可用时刻调度，未来计划通知和延迟重试不能阻塞已就绪工作。内存堆对相同可用时刻的任务按入队顺序排序，不保证多个并发Worker的完成顺序。

2.7 内存 `claim` 必须为每次领取返回独立任务对象和receipt；重新领取不得改写旧领取者所持的凭据，旧receipt不得确认、续租或重新调度新领取。

2.8 SQLite的 `claim` / `renew` 必须在取得 `BEGIN IMMEDIATE` 写事务后再采样租约时钟，避免把等待写锁的时间算入新租期。`renew` 必须同时验证receipt与未过期租约；过期领取不得复活。

### 需求 3：Worker 池与投递

**用户故事：** 作为开发者，我想要一个可配置的 Worker 池来处理已入队的通知，这样通知就可以在受控的并行度下并发投递。

#### 验收标准

3.1 Worker_Pool 必须为每个通知通道启动可配置数量的 Worker 实例。

3.2 当 Worker 从 Message_Queue 中拉取一个任务时，Worker 必须调用该通道已配置的 Provider 来投递通知。

3.3 当 Provider 返回成功的投递结果时，Worker 必须通过 Event_Tracker 记录一个 SENT 事件。

3.4 如果 Provider 抛出投递错误，则 Worker 必须将失败的任务传递给 Retry_Handler 进行重新入队。

3.5 Worker_Pool 必须支持优雅关闭，允许正在进行的投递在停止前完成。

3.6 Worker 和 Worker_Pool 支持在生命周期中启动和停止。复用同一个Worker时，退出或取消必须清理就绪信号，防止 `wait_started()` 读取上一轮启动状态；当前不提供运行中热调整Worker池大小。

### 需求 4：第三方 Provider 抽象

**用户故事：** 作为开发者，我想要一个可插拔的 Provider 接口，这样就可以在用于测试的模拟 Provider 和用于生产的真实集成之间进行切换。

#### 验收标准

4.1 Provider 必须定义一个抽象接口，包含一个接受通知载荷并返回投递结果的异步方法。

4.2 Simulated_Provider 必须实现 Provider 接口，带有可配置的模拟延迟和失败率。

4.3 Provider 接口区分iOS推送、Android推送、SMS和Email四种通道。当前内置实现为模拟投递，通道名称不表示已连通APNs、FCM或其他外部服务。

4.4 通过 `create_worker_pool(providers, ...)` 注入Provider映射并连接Worker_Pool，而不是在NotificationService构造函数中传入Provider。

4.5 **部署目标，未实现：** 真实SMTP、APNs HTTP/2、FCM和SMS网关适配器可实现Provider契约；当前包未提供这些适配器，也未声明 `[smtp]` 等真实渠道extras。

4.6 Provider 必须返回结构化的投递结果，包含成功状态、Provider 消息 ID 以及可选的错误详情。

### 需求 5：通知模板

**用户故事：** 作为开发者，我想要带有可定制参数的预格式化通知模板，这样就可以在保持消息一致性的同时为每个收件人个性化内容。

#### 验收标准

5.1 Notification_Template 必须使用已定义的语法（例如 `{variable_name}`）支持命名占位符。

5.2 当使用参数字典渲染 Notification_Template 时，Notification_Template 必须用提供的值替换所有占位符。

5.3 如果渲染时必需的占位符在参数字典中缺失，则 Notification_Template 必须抛出 TemplateRenderError。

5.4 Notification_Template 必须支持 title 和 body 的独立内容字段。

5.5 Notification_Service 必须在 Notification_Request 中接受预渲染的内容字符串，或者接受带有参数的模板标识符。

5.6 Notification_Template 必须支持通过唯一的模板标识符进行注册和检索。

### 需求 6：限流

**用户故事：** 作为开发者，我希望对每个用户的通知施加频率上限，这样用户就不会被过多消息淹没。

#### 验收标准

6.1 Rate_Limiter 必须在可配置的时间窗口内对每个用户执行可配置的最大通知数量。

6.2 Rate_Limiter 必须支持按通道设置限流（iOS、Android、SMS 和 Email 各自独立的限制）。

6.3 Rate_Limiter 必须支持适用于所有通道的全局按用户限流。

6.4 如果通知超出收件人的限流阈值，则 Rate_Limiter 必须拒绝该通知并返回 RateLimitExceededError。

6.5 当通知因限流被拒绝时，Notification_Service 必须记录一条 Event_Tracker 条目指明限流拒绝。

6.6 Rate_Limiter 必须使用滑动窗口算法进行基于时间的频率追踪。

### 需求 7：去重

**用户故事：** 作为开发者，我希望能够检测重复通知，这样同一通知就不会在短时间内多次发送给用户。

#### 验收标准

7.1 Deduplicator 必须根据收件人标识符、通知通道和内容哈希的组合来计算去重键。

7.2 当在可配置的去重窗口内已发送过具有匹配去重键的通知时，Deduplicator 必须抑制该重复通知。

7.3 Deduplicator 必须支持为去重条目设置可配置的存活时间（TTL）。

7.4 如果通知作为重复被抑制，则 Deduplicator 必须返回带有原始通知标识符的 DuplicateNotificationError。

7.5 Deduplicator 必须自动过期旧的去重条目，以防止内存无限增长。

### 需求 8：通知设置（Opt-in/Opt-out）

**用户故事：** 作为开发者，我想要按用户、按通道的通知偏好设置，这样用户就可以控制他们接收哪些类型的通知。

#### 验收标准

8.1 Notification_Settings 必须存储按用户和按通知通道的 opt-in/opt-out 偏好。

8.2 当新用户注册时，Notification_Settings 必须默认所有通道均为已选择（opted-in）。

8.3 用户退订后，Notification_Settings 在当前进程保存偏好，并在新通知受理及每次后续Provider调用前阻止该通道投递。跨重启持久化、共享偏好存储属于部署目标；已经开始的外部调用无法撤回。

8.4 当用户重新选择接收某个通道的通知时，Notification_Settings 必须恢复该通道的通知投递。

8.5 Notification_Settings 必须提供方法来检索给定用户的所有通道偏好。

8.6 Notification_Settings 必须支持在单次操作中为用户跨多个通道批量更新偏好。

### 需求 9：重试机制

**用户故事：** 作为开发者，我希望对投递失败进行带指数退避的自动重试，这样临时的 Provider 故障不会导致通知丢失。

#### 验收标准

9.1 当通知投递失败时，Retry_Handler 必须以指数增长的延迟重新入队该通知。

9.2 Retry_Handler 必须支持为每个通知设置可配置的最大重试次数。

9.3 Retry_Handler 必须支持为指数退避设置可配置的基础延迟和最大延迟上限。

9.4 如果超过最大重试次数，则 Retry_Handler 必须将通知标记为永久失败，并通过 Event_Tracker 记录 FAILED 事件。

9.5 Retry_Handler 必须在 Notification_Log 中记录每次重试的尝试次数和错误详情。

9.6 Retry_Handler 必须区分可重试错误（网络超时、限流）和不可重试错误（无效令牌、永久拒绝）。

### 需求 10：事件追踪与分析

**用户故事：** 作为开发者，我想要为每个通知进行生命周期事件追踪，这样就可以监控投递成功率和用户参与度。

#### 验收标准

10.1 Event_Tracker 必须记录每个通知在整个生命周期中的状态转换：CREATED、QUEUED、SENDING、SENT、DELIVERED、FAILED、CLICKED、UNSUBSCRIBED。

10.2 Event_Tracker 必须为每个事件存储时间戳、通知标识符、通道和新状态。

10.3 Event_Tracker 必须提供方法来检索给定通知标识符的完整事件历史。

10.4 Event_Tracker 必须提供聚合分析：在可配置时间范围内的总发送数、总投递数、总失败数和总点击数。

10.5 **部分实现：** Event_Tracker 提供各通道总数、成功率和失败率；平均投递延迟和外部指标后端仍是扩展目标。

10.6 Notification_Log 记录尝试及队列终态事实；一次FAILED尝试不代表整条通知最终失败。显式选择SQLite后端时可持久化；Event_Tracker中的送达/点击回调历史仍属于当前进程。

### 需求 11：联系信息管理

**用户故事：** 作为开发者，我希望管理用户联系信息和设备注册，这样通知就可以路由到正确的投递端点。

#### 验收标准

11.1 Notification_Service 必须维护一个 Contact_Info 存储，其中包含用户的邮箱地址、电话号码和 Device_Token。

11.2 Contact_Info 存储必须支持为每个用户的推送通知通道存储多个 Device_Token。

11.3 当通知被分发时，Notification_Service 必须根据收件人和通道从 Contact_Info 存储中解析投递端点。

11.4 Contact_Info 存储必须支持添加、更新和删除联系条目及设备令牌。

11.5 **部署目标，未实现：** 根据真实Provider的错误分类自动从Contact_Info删除无效设备令牌。当前Worker在任务上保存永久拒绝端点并在普通重试中跳过，不自动删除联系人中的注册令牌。

### 需求 12：通知日志与持久化

**用户故事：** 作为开发者，我想要一个持久化的通知日志，这样就可以审计投递历史并调试失败的通知。

#### 验收标准

12.1 Notification_Log 记录通知ID、收件人、通道、内容摘要、时间戳、尝试次数、该次尝试状态与错误详情；单条FAILED尝试记录不能直接推断最终投递失败。

12.2 Notification_Log 必须支持按通知标识符、收件人、通道、状态和时间范围进行查询。

12.3 Notification_Log 提供显式 `cleanup(retention_seconds)` 清理接口；周期调度和事件账本保留策略由部署方负责。清理审计行不会删除event_id去重账本。

12.4 Notification_Log 必须默认使用内存存储，并提供抽象接口以支持可插拔的持久化后端。

### 需求 13：自定义异常

**用户故事：** 作为开发者，我希望所有错误条件都有清晰、具体的异常，这样就可以优雅地处理通知失败。

#### 验收标准

13.1 Notification_Service 必须定义一个基础 NotificationError 异常，所有自定义异常均继承自该异常。

13.2 Notification_Service 必须为无效的通知请求定义 ValidationError。

13.3 Notification_Service 必须为队列容量超出的情况定义 QueueFullError。

13.4 Notification_Service 必须为违反限流的情况定义 RateLimitExceededError。

13.5 Notification_Service 必须为被去重抑制的通知定义 DuplicateNotificationError。

13.6 Notification_Service 必须为模板渲染失败定义 TemplateRenderError。

13.7 Notification_Service 必须为 Provider 投递失败定义 DeliveryError。

13.8 Notification_Service 必须在所有自定义异常中包含描述性错误信息和相关上下文。

### 需求 14：HTTP 演示服务器

**用户故事：** 作为开发者，我想要一个轻量级的 HTTP 演示服务器，这样就可以通过 REST 端点与通知系统交互，而无需引入额外的运行时依赖。

#### 验收标准

14.1 HTTP 演示服务器必须仅使用 Python 标准库模块（http.server、asyncio）。

14.2 HTTP 演示服务器必须暴露一个 POST 端点，用于根据给定的收件人、通道和内容发送通知。

14.3 HTTP 演示服务器必须暴露一个 GET 端点，用于根据通知标识符检索投递状态和事件历史。

14.4 HTTP 演示服务器必须暴露一个 GET 端点，用于检索按通道的聚合分析数据（投递计数、成功率）。

14.5 HTTP 演示服务器必须暴露用于管理用户通知设置的端点（获取偏好、opt-in、opt-out）。

14.6 **部分实现：** 演示支持POST `/templates` 注册和GET `/templates` 列举；模板渲染预览没有HTTP端点，渲染可通过库接口执行。

14.7 通知受理返回202，查询/回调/设置返回200，仅模板创建返回201。无效输入返回400，缺少或错误bearer凭据返回401，未知资源404，重复通知409，无效或过大请求体413，限流429，队列容量拒绝503。

14.8 HTTP 演示服务器必须使用 Notification_Service 库作为核心逻辑，不重复实现通知处理功能。

## 非功能性需求

### NFR-1：项目结构

- **NFR-1.1**：项目必须使用带有 hatchling 构建系统的 `pyproject.toml`。
- **NFR-1.2**：项目必须采用 src 布局（`src/notification_system/`）。
- **NFR-1.3**：项目必须要求 Python >= 3.9。
- **NFR-1.4**：核心库必须零运行时依赖（仅使用 stdlib）。
- **NFR-1.5**：**部署目标，未实现：** 将真实Provider适配器打包为可选extras。当前pyproject只声明开发extra，不支持 `notification-system[smtp]` 安装命令。
- **NFR-1.6**：项目包含开发依赖pytest、pytest-asyncio和hypothesis。
- **NFR-1.7**：项目必须包含一个 `docs/` 目录用于设计文档。
- **NFR-1.8**：项目必须包含一个 `examples/` 目录，其中存放 HTTP 演示服务器。
- **NFR-1.9**：项目必须提供干净的 `__init__.py`，导出公共 API。

### NFR-2：代码质量

- **NFR-2.1**：项目必须在所有公共接口中包含类型提示。
- **NFR-2.2**：项目必须使用 pytest 包含单元测试。
- **NFR-2.3**：项目必须使用 Hypothesis 包含基于属性的测试，用于队列顺序、去重正确性和限流不变量。
- **NFR-2.4**：项目必须为所有可插拔接口（Provider、Storage 等）使用抽象基类（ABC）。
- **NFR-2.5**：项目必须定义一个自定义异常模块，涵盖所有错误类型。
- **NFR-2.6**：队列协调、Worker和批量受理由asyncio驱动；阻塞SQLite/日志工作可通过 `asyncio.to_thread` 交给线程，不要求所有I/O在事件循环线程执行。

### NFR-3：架构

- **NFR-3.1**：系统必须采用进程内事件驱动架构，在单个进程内模拟分布式通知管道。
- **NFR-3.2**：内存Message_Queue采用各通道独立的可调度堆和asyncio唤醒；SQLiteMessageQueue提供持久计划任务和带receipt的租约。当前实现不是asyncio.Queue。
- **NFR-3.3**：Worker_Pool 必须使用 asyncio 任务实现并发通知投递。
- **NFR-3.4**：系统必须支持按通道配置并发级别（每个队列的 Worker 数量）。

### NFR-4：性能

以下为本地测量目标，实际吞吐取决于Provider延迟、Worker配置和后端；不能把目标数值当作本轮已实测的生产容量。

- **NFR-4.1**：对于内存后端，通知入队操作必须在 5 毫秒内完成。
- **NFR-4.2**：系统在使用模拟 Provider 时必须支持每秒处理至少 1000 条通知。
- **NFR-4.3**：限流检查必须在 1 毫秒内完成。
- **NFR-4.4**：去重检查必须在 1 毫秒内完成。

### NFR-5：可靠性

- **NFR-5.1**：同一进程中的内存任务在可恢复Worker故障后归还；选择SQLite后，待处理任务可跨进程重启并在租约到期后恢复。未提供磁盘/整机容灾或外部副作用恰好一次保证。
- **NFR-5.2**：重试机制必须使用带抖动（jitter）的指数退避，以防止 Provider 恢复时的惊群效应（thundering herd）。
- **NFR-5.3**：系统必须支持优雅关闭，在终止 Worker 前完成正在进行的投递。
