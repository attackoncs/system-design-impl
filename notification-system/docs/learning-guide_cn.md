# 第11章：从“受理成功”走到“投递成功”

通知系统把业务请求转换为**可重试的投递任务**。`send` 成功说明任务已接受，Worker稍后才调用Provider；重试和进程重启都发生在这条边界之后。本页选择SQLite持久队列与模拟Provider，避免把默认内存队列的行为误认为已经持久化。

## 案例：同一张发票只受理一次

用户user已配置邮箱 `user@example.com`、开启EMAIL通道。提交 `event_id="invoice-123"`、正文 `"invoice ready"`。得到通知ID后启动Worker；模拟Provider第一次暂时失败、第二次成功。预期始终沿用同一通知身份，最后状态为SENT。

此时即使任务已确认删除、进程重启，再提交同一event_id、recipient和channel，仍应抛重复通知错误并带原通知身份。这里的业务事件账本独立于“队列里还有没有这条任务”。

```mermaid
flowchart TB
  B[业务调用者] -->|await send 仅等受理| S[NotificationService]
  C[联系人 偏好 模板] --> S
  S -->|事务入队及事件身份| Q[(SQLite队列与事件账本)]
  Q -.->|后台claim租约| W[通道Worker]
  W -->|带超时deliver| P[模拟Provider]
  W -->|ack或reschedule| Q
  W --> L[(投递审计)]
```

持久队列保护任务与事件身份；联系人、偏好、限流等默认仍属进程内状态。Provider是模拟实现，不表示已经接入真实邮件、短信或推送服务。

## 用三个身份看懂数据

| 状态 | 关键字段 | 生命周期 |
|---|---|---|
| `NotificationRequest` | recipient_id、channel、event_id、title/body | 业务输入 |
| `NotificationTask` | notification_id、endpoints、attempt、completed_endpoints | 同一通知投递与重试的载荷 |
| `notification_events` | event_id、recipient_id、channel → notification_id | 接受成功后保留，终态后也不删除 |
| `notification_jobs` | id、payload、available、receipt、lease_until | 可领取、执行、延迟重试直至确认 |
| `receipt` | 每次claim新生成的随机凭据 | 防止过期Worker确认新租约 |
| `notification_log` | status、attempt、error_details | 持久审计，不是外部收件人的已读证明 |

## 七站阅读路线

1. [NotificationService.send](../src/notification_system/service.py#L145)：检查请求和计划时间，再按收件人条带锁串行受理；返回通知ID。
2. [NotificationService._accept](../src/notification_system/service.py#L186)：先检查显式event_id，再解析端点、偏好和模板；找到任务快照在哪里构造。
3. [SQLiteMessageQueue._enqueue](../src/notification_system/durable.py#L141)：在写事务里重新检查事件身份并接受任务；这才守住跨实例重复受理。
4. [SQLiteMessageQueue._claim](../src/notification_system/durable.py#L191)：输入通道，输出带receipt的任务；数据库不删除任务，只标记租约。
5. [Worker._process_claim](../src/notification_system/worker.py#L137)：并行监视投递与续租，处理取消时返回任务或依赖租约恢复。
6. [Worker._process_task](../src/notification_system/worker.py#L178)：再查偏好、跳过已完成端点、调用Provider；下一站处理失败。
7. [RetryHandler.handle_failure](../src/notification_system/retry.py#L70)：决定重试或终态；最后看 [SQLiteMessageQueue._ack](../src/notification_system/durable.py#L234) 如何核验当前租约。

## 微观一：先查去重还不够，接受事务必须再查

[durable.py:141](../src/notification_system/durable.py#L141)

```python
def _enqueue(self, task):
    with self._db.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        request = task.request
        if request.event_id is not None:
            original = db.execute(
                "SELECT notification_id FROM notification_events WHERE event_id=? AND recipient_id=? AND channel=?",
                (request.event_id, request.recipient_id, task.channel.value),
            ).fetchone()
            if original is not None:
                raise DuplicateNotificationError(original[0])
        count = db.execute(
            "SELECT count(*) FROM notification_jobs WHERE channel=?",
            (task.channel.value,),
        ).fetchone()[0]
        if count >= self._max_depth:
            raise QueueFullError(task.channel.value, self._max_depth)
```

`BEGIN IMMEDIATE` 取得写事务；用event_id、收件人和通道查询持久账本。若已经存在直接报重复，未存在才检查通道容量并准备接受。继续向下读：任务插入、QUEUED审计和事件账本都在同一事务。

两个进程可以同时通过服务层的初次查询，但不能都通过这个写事务。进程内条带锁只减少本进程竞争，不能替代数据库唯一键。

## 微观二：旧Worker不能确认新Worker领到的任务

[durable.py:234](../src/notification_system/durable.py#L234)

```python
def _ack(self, task, status, details):
    with self._db.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        changed = db.execute(
            "DELETE FROM notification_jobs WHERE id=? AND receipt=? AND lease_until>?",
            (task.notification_id, task.receipt, time.time()),
        ).rowcount
        if not changed:
            raise RuntimeError("Notification lease was lost before acknowledgement")
        if status is not None:
            self._db.log(db, task, status, details)
```

删除语句同时匹配通知id、receipt和未过期租约。如果Worker停顿到租约失效，另一个Worker已经领取，旧receipt即使携带正确notification_id也不能删除新任务；rowcount为0时必须报错。

```mermaid
sequenceDiagram
  participant B as 业务方
  participant S as 服务
  participant Q as SQLite队列
  participant W as Worker
  participant P as 模拟Provider
  B->>S: send(event_id=invoice-123)
  S->>Q: 事务接受任务和事件身份
  Q-->>S: 事务接受完成
  S-->>B: notification_id
  Q-->>W: claim给出receipt
  W->>P: 第一次deliver
  P-->>W: 暂时失败
  W->>Q: reschedule同一任务并延迟
  Q-->>W: 到期后新receipt领取
  W->>P: 再次deliver
  P-->>W: 成功
  W->>Q: 核对receipt后ack并记SENT
```

这条时序不承诺外部恰好一次：Provider已发送但Worker来不及确认时，恢复可能重复调用。供应商幂等需要额外集成。

## 两个很容易误解的失败结果

队列满不是“接受后等待”：入队拒绝应保持业务可重试，不能顺手耗尽本地限流额度或污染内容去重。已经受理的任务暂时投递失败，则使用延迟重试归还Worker，避免一个失败通知占住唯一Worker睡眠。

- [test_event_id_dedup_survives_ack_restart_and_concurrent_acceptance](../tests/test_runtime_reliability.py#L185)：两实例并发只接受一个，任务确认和重启后相同业务事件仍被识别。
- [test_durable_lease_recovers_process_exit_and_fences_stale_ack](../tests/test_runtime_reliability.py#L221)：子进程领取后退出，过期任务可恢复；旧receipt不能确认它。
- [test_retry_yields_worker_to_other_jobs_and_provider_exception_isolated](../tests/test_runtime_reliability.py#L105)：单Worker的实际投递次序为first、second、first，而非被first的重试等待堵住。

## 自查与练习

- **删除完成队列项会让同一event_id重新受理吗？** 不会，事件账本独立保留。
- **SENT是否说明用户已经阅读？** 不是，它表示Provider调用成功这一层的状态。
- **多设备推送部分成功后是否重试全部端点？** 任务记录已完成端点，重试只处理剩余端点；最终应结合永久拒绝分支检查结果。

按运行示例把同一业务事件提交两次，再重启服务提交第三次；预期只产生一个被接受的通知身份。换成新event_id、相同正文应允许成为新的业务通知。随后在模拟Provider制造一次暂时失败，观察attempt、receipt和审计如何分别变化。运行方式见 [README_CN.md](../README_CN.md)。


---

[返回全仓库学习导航](../../docs/learning-guide_cn.md) · [项目运行说明](../README.md)

[导读原稿](../../tools/learning_guides/notification-system.md)。正文与图按当前实现编写；源码链接和摘录由生成器绑定到当前定义。修改行为后应同时复核案例、时序和解释，再重新生成；定义与链接校验不等同于业务回归测试。
