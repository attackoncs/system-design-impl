# 第11章：批量受理并发验证

日期：2026-11-01；Windows，Python3.13.9。计时范围为构造好联系人/请求之后的 `send_batch`，每组240条、重复3次，取受理量中位数。每条请求使用不同收件人和event_id，检查全部返回不同通知ID且队列深度正确。

## 同一实现的受控对照

全套测试结束后，单独顺序执行以下两组；测量期间没有同时运行pytest或其他性能测试。两组仅并发参数不同，固定 `PYTHONHASHSEED=0`。

```powershell
$env:PYTHONHASHSEED = '0'
python tools/benchmark.py --concurrency 1 --output benchmarks/batch-sequential-2026-11-01.json
python tools/benchmark.py --concurrency 16 --output benchmarks/batch-review-2026-11-01.json
```

| 后端场景 | 串行受理/秒 | 并发16受理/秒 | 并发enqueue峰值 | 解读 |
|---|---:|---:|---:|---|
| 内存队列，主动让出事件循环 | 14952.81 | 16707.51 | 15 | 短任务，差异容易受运行时调度影响 |
| 内存队列附加异步等待 | 64.32 | 863.11 | 16 | 可重叠等待，本次约13.42倍 |
| 真实SQLite持久队列 | 100.99 | 99.17 | 未采集 | 写入仍串行，没有观察到吞吐提升 |

第二行在入队前使用 `asyncio.sleep(0.002)` 模拟独立后端等待；Windows实际唤醒延迟受定时器和调度影响，不宣称真实服务固定耗时2ms。它验证可重叠的异步等待受益于并发，并不代表SMTP、APNs、FCM或短信厂商性能。

没有启动投递worker，以上指标是**受理并入队**的速率。数据库初始化、联系人构造与真正投递不在计时中。SQLite实例还会串行数据库操作；同一用户的条带锁也会限制有效并发。不能将这些数据外推为书中每日千万级通知容量。

## 原始数据

- [串行控制组](batch-sequential-2026-11-01.json)
- [并发16控制组](batch-review-2026-11-01.json)
- [修改前的默认串行实现](batch-baseline-2026-11-01.json)：历史记录；上表结论以同一实现的控制组为准。

## 正确性证据

[批量回归](../tests/test_batch_admission_review.py)覆盖513条请求最多16个消费者、结果位置、单项失败、串行模式、取消后无遗留子任务、内存/SQLite同事件首次接受优先。资源上限由固定消费者数保证，而非为每条输入先创建一个受Semaphore限制的任务。

[队列回归](../tests/test_queue_reliability_review.py)以实际SQLite写锁和可控时钟验证租约，不使用性能阈值判断正确性。[Worker回归](../tests/test_worker_lifecycle_review.py)验证复用后的启动/停止及取消。
