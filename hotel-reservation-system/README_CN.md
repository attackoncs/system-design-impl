# 第 23 章：酒店预订系统

<!-- LEARNING_GUIDE_START -->
## 从这里开始读代码

[跟着一个案例读懂系统：架构、状态变化与关键代码详解](docs/learning-guide_cn.md)。
<!-- LEARNING_GUIDE_END -->

标准库 Python 参考实现：网关、两个酒店分片、两个持久化任务工作进程和独立模拟支付服务，共六个真实 HTTP 进程。提供酒店/房型管理、每日价格与库存、多夜原子预订、最多 10% 超售、幂等取消、过期释放和支付/退款补偿。

要求 Python 3.10+，不依赖额外包或外部服务。所有支付均为模拟，不会操作真实资金；不含酒店搜索或具体房号分配。

2026-10-26核心审查补充酒店内跨房型可用查询、滚动日历默认策略，并改善库存/Saga/
传输职责、任务身份校验、缓存与后台资源边界。本轮最终测试/实测见
[验证报告](benchmarks/results.md)，原书映射和未完成项见
[章节记录](docs/chapter23-review.md)。2026-10-20数据仅保留历史日期。

## 运行

在本目录执行：

```powershell
$env:PYTHONPATH = 'src'
python examples/network_demo.py
python -m unittest discover -s tests -v
python tools/benchmark.py --output benchmarks/results.json
```

实验自动选用本机端口、生成私有凭据、启动并清理自有进程；数据与日志保留在忽略提交的 `.runtime/lab-*`。若系统临时目录访问受限，创建 `.runtime/tmp`，将 `TEMP` 和 `TMP` 设为其绝对路径。详见 [验证报告](benchmarks/results.md)。

## 手动启动

```powershell
$env:PYTHONPATH = 'src'
python tools/init_config.py
# 每条分别在独立终端运行，设置相同 PYTHONPATH：
python -m hotel_reservation.cli gateway --config .runtime/secrets.json --port 9830
python -m hotel_reservation.cli payment --config .runtime/secrets.json --port 9831
python -m hotel_reservation.cli shard --config .runtime/secrets.json --shard 0 --port 9832
python -m hotel_reservation.cli shard --config .runtime/secrets.json --shard 1 --port 9833
python -m hotel_reservation.cli worker --config .runtime/secrets.json --shard 0
python -m hotel_reservation.cli worker --config .runtime/secrets.json --shard 1
```

配置包含不同的管理/内部/支付令牌、alice/bob 两个用户令牌、分片 URL、模拟支付 URL、120 秒占用期和 2 秒任务租约。生成器拒绝覆盖已有文件。服务默认仅监听 `127.0.0.1`，不要公开配置或数据库。

## 接口与流程

所有业务接口使用 Bearer 令牌。用户可查看详情和本人订单，管理员管理酒店、房型、日历并可取消订单。

| 接口 | 用途 |
| --- | --- |
| `POST /v1/hotels` | 管理员创建，正文 `{id,name,address}` |
| `GET/PUT/DELETE /v1/hotels/{hotel}` | 详情、管理员增改 `{name,address}` 或软删除 |
| `POST /v1/hotels/{hotel}/rooms` | 管理员创建房型，正文 `{id,name,description}` |
| `GET/PUT/DELETE /v1/hotels/{hotel}/rooms/{type}` | 房型详情、管理员增改/软删除 |
| `PUT /v1/hotels/{hotel}/rooms/{type}/calendar` | 管理员设置 `{days:[{day,capacity,overbook,price},...]}` |
| `GET /v1/hotels/{hotel}/rooms/{type}/availability?start=YYYY-MM-DD&end=YYYY-MM-DD` | 可用量与每日价格，结果仅供报价参考 |
| `GET /v1/hotels/{hotel}/availability?start=YYYY-MM-DD&end=YYYY-MM-DD&quantity=1` | 同一快照查询所有活跃房型，返回每晚日历完整且足量的房型；最多100源房型 |
| `GET/PUT/DELETE /v1/hotels/{hotel}/rooms/{type}/calendar-policy` | 管理员查看/设置滚动默认策略，或停止后续自动补空日 |
| `POST /v1/reservations` | 用户提交预订，占用全部入住日期并持久化支付任务 |
| `GET /v1/reservations?hotel={hotel}&after={cursor}` | 本人按酒店分页历史，最多 100 条，cursor 可省略 |
| `GET/DELETE /v1/reservations/{id}` | 本人或管理员查看/幂等取消 |

酒店/房型 ID 为 1–80 个 ASCII 字母、数字、下划线或连字符。PUT 可创建或更新，DELETE 软删除并阻止新预订，历史退款仍可完成。`capacity` 为每日物理可售数量，`overbook` 为 0–10 的整数百分比，`price` 为单间每日 USD 美分。必须预填每晚日历。

预订正文示例（执行时换成当前允许日期）：

```json
{"hotel":"demo","room":"king","start":"2030-01-02","end":"2030-01-04","quantity":1,"request_key":"booking-001","expected_total":20000,"payment_method":"test-ok"}
```

可直接执行的建酒店/房型/日历/预订 Python 示例见 [英文说明](README.md)。日期为入住日含、离店日不含，最多 30 晚、10 间，允许未来 730 个 UTC 日。价格只支持 USD 整数美分，不包含税费。预订事务重新检查 `expected_total`，价格总额改变返回 409，必须重新接受报价。已接受订单保存每天的价格快照。

`request_key` 按酒店+用户分组。同键同内容返回原订单当前状态；不同内容拒绝；已取消或过期订单不会因旧键重试重新占房。`payment_method` 仅接受 `test-ok` 或 `test-decline`，请勿提交银行卡资料。

滚动日历是显式启用功能：PUT策略必填`capacity/overbook/price`，选填`horizon_days`
（1..730，默认730）。分片CLI的自有维护线程默认每5秒执行一轮（配置`calendar_interval`），
每轮最多32策略/4096日期行，只补空日，不改任何既有日价、占用或历史库存。策略更新不是
批量重定价；删除策略保留已有日历。进度持久化并检查version，重启可以继续；停用酒店/
房型不再填充。内部/管理令牌可直接向分片POST `/internal/calendar-roll`，正文`{}`，
执行同样有限的一轮，网关不转发此接口。

## 正确性与恢复语义

- 可售上限 = 物理容量 + floor(物理容量 × 超售百分比 / 100)。9 间在 10% 时仍最多卖 9 间，10 间可卖 11 间；待付款与已确认订单都占库存。
- 跨夜库存、订单、价格快照和支付任务在同一分片事务提交。任一夜不足或缺失整笔回滚。SQLite 写事务、条件递增与 CHECK 共同保护上限，禁止管理员调低到低于当前占用的容量。
- PENDING 只是临时占用，全额模拟付款成功且未过期才变成 CONFIRMED/PAID。明确拒付会释放库存；超时或 HTTP 错误属于结果未知，不能当作拒付。
- 取消只返还一次库存；已付款订单进入全额退款。占用过期或取消后才收到的支付成功会进入补偿退款，不会确认住宿。任务认领有持久化租约和随机令牌，旧进程不能覆盖新认领。
- 工作进程每轮清理最多 100 个过期占用；进程停机期间可能保守占用库存直到恢复，不保证截止秒即时释放。已确认订单不按待付款期限过期。
- 本参考实现支持无罚金的全额取消；真实取消政策、酒店当地时区、币种和税费尚未实现。
- 酒店哈希路由到固定分片，库存/订单在同一数据库；不支持运行中直接改变分片数。单热点酒店仍串行争用同一个 SQLite 写者。
- 详情与库存读取采用 128 项、1 秒 TTL 缓存，键含数据库持久化版本，独立任务进程改库存也会使旧缓存失效。缓存可用量仅为参考，最终预订始终依据数据库事务。

缓存冷加载固定条带合并并隔离返回对象；catalog/availability在一致读快照中读取版本与
内容。预订/回调输入在等待writer前冻结，事务时间在获取writer后采样；维护/任务/历史索引
减少无关历史扫描。同分片SQLite仍只有一个写者，不把版本字段称为已实现乐观重试算法。

HTTP默认32个活跃handler、128 backlog、10秒绝对socket I/O预算，正文1MiB/响应4MiB；
CLI可设`--concurrency`（1..256）和`--request-timeout`（大于0且不超过3600秒）。
显式路由/方法/query契约、正文前鉴权、严格framing/JSON；网关只转发允许的公共接口，
保持原身份且不盲目重试写入。RPC直连，不走环境代理/重定向，HTTPS正常验证证书。
socket期限不能强杀DNS/SQL/CPU，名额保留到实际退出。HTTP绑定成功后才启动维护，
关闭排空handler和自有线程；失败用脱敏状态/日志报告并重试。

分片 `/internal/status` 提供未完成任务、重试错误、待清理占用及维护错误；
`/internal/events?after=0`提供分页持久化事件；内部POST接口为`/internal/sweep`、
`claim`、`finish`、`retry`和`calendar-roll`。需内部/管理令牌，网关不转发。

模拟支付服务提供受支付令牌保护的 `/operations`，以及管理员 `/admin/status`、`/admin/control`。控制正文 `{"fail_after_commit":true,"fail_refunds":false}` 可模拟支付已持久化但响应丢失；退款故障开关可验证补偿重试。这些都是本地模拟接口，不是银行或真实支付接口。

数据库分片和模拟账本暂无复制/自动故障转移；验证仅覆盖保留本地磁盘的进程重启。
在线扩分片、跨分片用户索引、租户员工权限、TLS、分布式限流、自动隐私清理、真实支付、
酒店当地时区/税费及原书5000酒店/100万房间规模尚未实现或验收。更多见
[设计文档](docs/design_cn.md)。
