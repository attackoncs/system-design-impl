# 需求文档

## 引言

一个 Python 通知系统库，实现了《System Design Interview》第 11 章「Design A Notification System」中的设计。该库提供了一个完整的进程内事件驱动通知管道，支持 iOS 推送（APNs）、Android 推送（FCM）、短信（SMS）和邮件（Email）通道。其特性包括：带各通道独立队列的内部消息队列、可配置的 Worker 池、通知模板、用户偏好管理（opt-in/opt-out）、限流（rate limiting）、去重（deduplication）、带指数退避的重试机制以及事件追踪与分析。核心库运行时零依赖；真实第三方服务提供商集成（SMTP 等）作为可选 extras 提供。一个轻量级的 stdlib HTTP 演示服务器暴露了 REST 端点，用于发送通知、查询投递状态和查看分析数据。

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

1.7 Notification_Service 必须支持在单次批量请求中向多个收件人发送通知。

1.8 当通知成功入队时，Notification_Service 必须返回一个唯一的通知标识符用于状态追踪。

### 需求 2：消息队列系统

**用户故事：** 作为开发者，我希望每个通知通道都有独立的进程内消息队列，这样每个通道就可以通过适当的并发度独立处理。

#### 验收标准

2.1 Message_Queue 必须为每个通知通道维护独立的内部队列：iOS 推送、Android 推送、SMS 和 Email。

2.2 Message_Queue 必须支持使用 Python asyncio 原语的异步入队和出队操作。

2.3 Message_Queue 必须支持每个通道配置可配置的最大队列深度。

2.4 如果队列达到最大深度，则 Message_Queue 必须拒绝新的入队操作并抛出 QueueFullError。

2.5 Message_Queue 必须提供方法来查询每个通道队列的当前深度和待处理数量。

2.6 Message_Queue 必须保证每个通道队列内的 FIFO（先进先出）顺序。

### 需求 3：Worker 池与投递

**用户故事：** 作为开发者，我想要一个可配置的 Worker 池来处理已入队的通知，这样通知就可以在受控的并行度下并发投递。

#### 验收标准

3.1 Worker_Pool 必须为每个通知通道启动可配置数量的 Worker 实例。

3.2 当 Worker 从 Message_Queue 中拉取一个任务时，Worker 必须调用该通道已配置的 Provider 来投递通知。

3.3 当 Provider 返回成功的投递结果时，Worker 必须通过 Event_Tracker 记录一个 SENT 事件。

3.4 如果 Provider 抛出投递错误，则 Worker 必须将失败的任务传递给 Retry_Handler 进行重新入队。

3.5 Worker_Pool 必须支持优雅关闭，允许正在进行的投递在停止前完成。

3.6 Worker_Pool 必须支持在运行时动态启动和停止 Worker。

### 需求 4：第三方 Provider 抽象

**用户故事：** 作为开发者，我想要一个可插拔的 Provider 接口，这样就可以在用于测试的模拟 Provider 和用于生产的真实集成之间进行切换。

#### 验收标准

4.1 Provider 必须定义一个抽象接口，包含一个接受通知载荷并返回投递结果的异步方法。

4.2 Simulated_Provider 必须实现 Provider 接口，带有可配置的模拟延迟和失败率。

4.3 Provider 接口必须支持四种通道类型：iOS 推送（APNs）、Android 推送（FCM）、SMS 和 Email。

4.4 Notification_Service 必须在构造时接受任何符合 Provider 接口的 Provider 实现。

4.5 当真实 Provider 集成作为可选 extras 安装时，Notification_Service 必须支持基于 SMTP 的 Email 投递、APNs HTTP/2 推送、FCM HTTP 推送和 SMS 网关投递。

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

8.3 当用户选择不接收某个通道的通知时，Notification_Settings 必须持久化该偏好并立即阻止该通道上的进一步通知。

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

10.5 Event_Tracker 必须提供按通道的投递统计数据（成功率、失败率、平均投递延迟）。

10.6 Notification_Log 必须持久化所有通知尝试及其最终状态，用于审计目的。

### 需求 11：联系信息管理

**用户故事：** 作为开发者，我希望管理用户联系信息和设备注册，这样通知就可以路由到正确的投递端点。

#### 验收标准

11.1 Notification_Service 必须维护一个 Contact_Info 存储，其中包含用户的邮箱地址、电话号码和 Device_Token。

11.2 Contact_Info 存储必须支持为每个用户的推送通知通道存储多个 Device_Token。

11.3 当通知被分发时，Notification_Service 必须根据收件人和通道从 Contact_Info 存储中解析投递端点。

11.4 Contact_Info 存储必须支持添加、更新和删除联系条目及设备令牌。

11.5 如果 Device_Token 被 Provider 报告为无效，则 Notification_Service 必须从 Contact_Info 存储中移除该无效令牌。

### 需求 12：通知日志与持久化

**用户故事：** 作为开发者，我想要一个持久化的通知日志，这样就可以审计投递历史并调试失败的通知。

#### 验收标准

12.1 Notification_Log 必须记录每次通知尝试，包括：通知标识符、收件人、通道、内容摘要、时间戳和最终状态。

12.2 Notification_Log 必须支持按通知标识符、收件人、通道、状态和时间范围进行查询。

12.3 Notification_Log 必须支持可配置的数据保留策略，并自动清理超过保留期的条目。

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

14.6 HTTP 演示服务器必须暴露用于管理通知模板的端点（创建、列出、渲染预览）。

14.7 HTTP 演示服务器必须返回合适的 HTTP 状态码：成功提交通知返回 201，查询返回 200，校验错误返回 400，未知资源返回 404，限流违反返回 429。

14.8 HTTP 演示服务器必须使用 Notification_Service 库作为核心逻辑，不重复实现通知处理功能。

## 非功能性需求

### NFR-1：项目结构

- **NFR-1.1**：项目必须使用带有 hatchling 构建系统的 `pyproject.toml`。
- **NFR-1.2**：项目必须采用 src 布局（`src/notification_system/`）。
- **NFR-1.3**：项目必须要求 Python >= 3.9。
- **NFR-1.4**：核心库必须零运行时依赖（仅使用 stdlib）。
- **NFR-1.5**：真实 Provider 集成必须作为可选 extras 提供（例如 `pip install notification-system[smtp]`）。
- **NFR-1.6**：项目必须包含 dev 依赖：pytest 和 hypothesis。
- **NFR-1.7**：项目必须包含一个 `docs/` 目录用于设计文档。
- **NFR-1.8**：项目必须包含一个 `examples/` 目录，其中存放 HTTP 演示服务器。
- **NFR-1.9**：项目必须提供干净的 `__init__.py`，导出公共 API。

### NFR-2：代码质量

- **NFR-2.1**：项目必须在所有公共接口中包含类型提示。
- **NFR-2.2**：项目必须使用 pytest 包含单元测试。
- **NFR-2.3**：项目必须使用 Hypothesis 包含基于属性的测试，用于队列顺序、去重正确性和限流不变量。
- **NFR-2.4**：项目必须为所有可插拔接口（Provider、Storage 等）使用抽象基类（ABC）。
- **NFR-2.5**：项目必须定义一个自定义异常模块，涵盖所有错误类型。
- **NFR-2.6**：项目必须在所有内部并发（队列、Worker、投递）中使用 Python asyncio。

### NFR-3：架构

- **NFR-3.1**：系统必须采用进程内事件驱动架构，在单个进程内模拟分布式通知管道。
- **NFR-3.2**：内部 Message_Queue 必须使用 asyncio.Queue 实现非阻塞异步操作。
- **NFR-3.3**：Worker_Pool 必须使用 asyncio 任务实现并发通知投递。
- **NFR-3.4**：系统必须支持按通道配置并发级别（每个队列的 Worker 数量）。

### NFR-4：性能

- **NFR-4.1**：对于内存后端，通知入队操作必须在 5 毫秒内完成。
- **NFR-4.2**：系统在使用模拟 Provider 时必须支持每秒处理至少 1000 条通知。
- **NFR-4.3**：限流检查必须在 1 毫秒内完成。
- **NFR-4.4**：去重检查必须在 1 毫秒内完成。

### NFR-5：可靠性

- **NFR-5.1**：即使 Worker 在投递过程中发生故障，系统也不得丢失已成功入队的通知。
- **NFR-5.2**：重试机制必须使用带抖动（jitter）的指数退避，以防止 Provider 恢复时的惊群效应（thundering herd）。
- **NFR-5.3**：系统必须支持优雅关闭，在终止 Worker 前完成正在进行的投递。
