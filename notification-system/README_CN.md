# 通知系统

<!-- LEARNING_GUIDE_START -->
## 从这里开始读代码

[跟着一个案例读懂系统：架构、状态变化与关键代码详解](docs/learning-guide_cn.md)。
<!-- LEARNING_GUIDE_END -->

依据本地《System Design Interview.md》第 11 章实现的 Python 3.9+ asyncio 参考项目，包含 iOS 推送、Android 推送、短信与邮件通道。内置 `SimulatedProvider` **只模拟投递**，不会调用 APNs、FCM、短信或邮件外部服务。

[English](README.md) · [设计文档](docs/design_cn.md) · [章节核对记录](docs/chapter11-review.md)

## 安装和启动

```powershell
python -m pip install -e ".[dev]"
$env:NOTIFICATION_API_KEY = 'local-demo-key'
python examples/demo_server.py --database notifications.sqlite3 --port 8000
```

从本项目目录运行，默认监听 `127.0.0.1`。HTTP 示例会启动独立事件循环和真正运行的 worker 池。所有接口均要求 `Authorization: Bearer local-demo-key`。数据库保存待处理通知、事件去重账本和审计日志；再次启动同一文件会恢复待处理任务。

```powershell
$headers = @{ Authorization = 'Bearer local-demo-key' }
$body = @{recipient_id='user_1'; channel='email'; body='你好'; event_id='demo-001'} | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8000/notifications -Method Post -Headers $headers -Body $body
```

`202` 表示通知已经接受入队。`SENT` 表示 provider 接受请求；真实送达和点击需要回执。使用同一个 `event_id` 重放会得到包含原通知 ID 的重复错误。

库方式使用时，构造 `NotificationService(queue=SQLiteMessageQueue(path))`，再调用 `service.create_worker_pool(providers, NotificationLog(SQLiteNotificationLog(path)))`；启动与关闭分别是 `await pool.start()`、`await pool.stop()`。完整可执行示例见英文 README。仅构造 `NotificationService()` 使用内存队列，退出进程后数据不保留。

## 本次补齐的链路

- 在接受请求时解析模板并快照正文、元数据和所有端点，避免模板用于去重后实际发送空内容，也避免外部修改字典影响已接受任务。
- 每个通道独立排队和配置 worker 数。单个 worker 逐个发送用户的设备，多个 worker 并发处理。通过 `add_device_token(user, token, Channel.IOS_PUSH/ANDROID_PUSH)` 登记平台；旧版无平台 token 仍兼容两个推送通道。
- 接受时及每次调用 provider 前检查退订；重试不绕过偏好。已发生的外部调用无法撤回。
- 按用户、按通道限频；`global_rate_limit` 是该用户跨通道总额，不是全系统限额。入队失败不占用限额，也不污染内容去重。
- 可选 `NotificationRequest.event_id` 在 `(event_id, recipient_id, channel)` 范围去重。SQLite 在接受事务中原子保存事件账本、通知任务和入队日志，跨队列实例、重启及终态完成后均有效；采用首次接受内容优先，变更正文的重放仍返回原通知 ID。不同事件允许相同内容。未提供事件 ID 时，内容 TTL 去重限于单个 service 实例。
- worker 领取任务时预留队列容量，避免投递期间新任务挤掉重试。延迟重试由队列调度，不占着 worker 等待退避；provider 异常和超时进入重试，领取队列的临时故障采用可中断退避。
- 多设备普通重试跳过已成功设备；永久拒绝端点独立保留，暂时失败端点继续重试。存在永久拒绝时最终整体状态为 `FAILED`。
- SQLite 使用可续期、带凭据隔离的租约；取消 worker 会归还任务，进程退出后租约到期恢复。过期领取者无法确认其他 worker 重新领取的任务。计划通知使用带时区的 `scheduled_at`，持久化后仍能按时进入队列，且不阻塞即时任务。
- 审计包含接受、失败尝试及终态记录，可以查询和清理。内存 `tracker` 是实时状态机，不是持久送达账本。

## HTTP 接口

| 接口 | 作用 |
|---|---|
| `POST /notifications` | 接受通知，支持 `event_id`、带时区 ISO `scheduled_at` |
| `GET /notifications/<id>` | 当前进程事件历史与数据库审计记录 |
| `GET /analytics` | 内存状态统计及各通道待处理、投递中数量 |
| `GET/PUT /settings/<user>` | 查询和修改用户通道偏好 |
| `GET/POST /templates` | 列举与登记模板 |
| `POST /events` | 接收已验证调用方的 `delivered/clicked` 状态回调 |

请求体必须是 JSON 对象，最多 1 MiB，并提供 `Content-Length`。共享 bearer key 演示内部接口鉴权；真实部署还需 TLS、分客户授权、provider 回调签名和密钥轮换。重启后的回调历史重建尚未实现。

## 真实边界

本项目使用 SQLite 本地持久队列，不提供复制型消息代理。计划任务和已领取任务计入容量；`depth()` 包含等待、计划及租约到期任务，`in_flight()` 统计有效租约。SQLite 修改和审计写入在线程执行，监控深度接口是同步快照；写事务串行化，不能据此声称已达到书中每日千万级容量。

投递为**至少一次**。外部服务接受后、数据库确认前崩溃，会重发；事件 ID 防止上游请求被再次接受，不保证 provider 外部副作用恰好一次。真实适配器应使用稳定的逐端点幂等键。已成功和永久拒绝端点在重新调度时检查点落盘，检查点前崩溃仍可能重发端点。

联系人、偏好、模板、状态统计、内容 TTL 去重仍在内存；启动恢复 worker 前需重新加载。共享元数据、跨进程限频、缓存失效、真实渠道适配、外部告警、消息代理复制和自动扩容属于尚未实现的部署工作。HTTP 示例串行处理请求，worker 异步并发投递。

事件账本在清理审计时仍保留，需要独立制定保留策略。通知正文及端点会进入本地数据库，应按业务需要设置文件权限和保留期限。

## 验证

```powershell
python -m pytest tests -q
```

具体回归结果见[第 11 章审查记录](docs/chapter11-review.md)。
