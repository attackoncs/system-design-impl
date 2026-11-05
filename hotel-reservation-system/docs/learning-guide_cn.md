# 第23章：从两晚预订读懂库存事务与支付补偿

酒店预订先做两件事：在数据库里占住房间，再在数据库外收钱。前者可由本地事务原子完成，后者需要异步任务、幂等支付和补偿。把预订`status`与支付`payment`分开看，就不会把“房间已锁定”误读成“已经付款确认”。这里的支付接口只支持模拟方法。

## 贯穿案例：alice预订两晚

沿测试配置：酒店h、房型king，入住`2030-01-02`、退房`2030-01-04`，每天容量10、允许10%超订，价格分别10002和10003美分。alice订1间，提交`expected_total=20005`、`request_key=req`、`payment_method=test-ok`。

受理后两个日期的reserved都加1，预订为`PENDING`，产生charge任务；收到可信paid回执后才确认。相同req和相同内容再次提交返回原预订，不能再占两晚库存。

```mermaid
flowchart TB
  U["用户"] -->|同步预订| API["预订API／Bookings"]
  API -->|同一SQLite事务| I[("逐日inventory")]
  API -->|同一SQLite事务| B[("bookings与nights")]
  API -->|同一SQLite事务| J[("支付jobs")]
  J -.->|后台领取| W["Worker"]
  W -->|事务外调用| P["模拟支付适配器"]
  W -->|租约校验后提交| B
  W -->|取消／过期时补偿| I
```

## 读懂这几个状态就够开始

| 状态／数据 | 本案例的变化 | 谁能决定 |
|---|---|---|
| `inventory.reserved/version` | 两晚各增加1 | 预订写事务，不能由缓存查询结果决定 |
| `bookings.status` | PENDING→CONFIRMED，或CANCELLED等终态 | 受理和Saga状态推进 |
| `bookings.payment` | NONE→PAID，取消后可能REFUND_PENDING | 可信支付结果与补偿任务 |
| `held` | 是否仍占库存 | release只应释放仍持有的库存 |
| `nights.day/price` | 保存两晚价格快照 | 预订受理事务 |
| jobs租约 | 同一次领取的身份与期限 | claim／finish，旧worker不能越权完成 |

## 六站读到核心代码

1. [Catalog.availability](../src/hotel_reservation/catalog.py#L149)：先看查询怎样给出当前报价和库存；记住它只是查询。
2. [Bookings.reserve](../src/hotel_reservation/bookings.py#L30)：从user和body开始，顺着幂等检查、报价检查、逐日占库、建预订和任务读下去。
3. [Saga.claim](../src/hotel_reservation/saga.py#L40)：找到hold过期清理及任务租约，理解后台为什么能够重启。
4. [Worker._work](../src/hotel_reservation/worker.py#L85)：标出支付调用与数据库回调的分界，以及未知异常如何变成重试。
5. [Saga.finish](../src/hotel_reservation/saga.py#L127)：检查paid结果到来时预订是否仍能确认。付款成功不保证预订还未取消。
6. [Bookings.release](../src/hotel_reservation/bookings.py#L123)与[Bookings.cancel](../src/hotel_reservation/bookings.py#L148)：跟踪库存释放和必要的退款任务，不要只读状态字符串。

## 微观一：幂等检查必须在占库之前

[bookings.py:59](../src/hotel_reservation/bookings.py#L59)

```python
with self.transaction(True) as db:
    old = db.execute(
        "SELECT id,hash FROM bookings WHERE hotel=? AND user=? AND request_key=?",
        (hotel, user, key),
    ).fetchone()
    if old:
        if old["hash"] != fingerprint:
            raise Error(409, "idempotency key payload conflict")
        return self.booking(old["id"], user, db)
```

身份范围是hotel、user和request_key，内容hash防止同一身份偷偷更换入住日期或数量。命中后直接返回原结果，绕过第二次占库。如果只在HTTP层做临时去重，重启或并发请求仍可能重复扣库存。

## 微观二：预检查不足以防超卖

[bookings.py:85](../src/hotel_reservation/bookings.py#L85)

```python
for day in days:
    cursor = db.execute(
        """UPDATE inventory SET reserved=reserved+?,version=version+1
        WHERE hotel=? AND room=? AND day=? AND reserved+?<=capacity+(capacity*overbook)/100""",
        (quantity, hotel, room, day, quantity),
    )
    if not cursor.rowcount:
        raise Error(409, "insufficient inventory for at least one night")
```

决定是否成功的是UPDATE中的容量条件与`rowcount`。两晚中任意一晚更新失败就抛错，由外层事务回滚已经更新的其他日期。配置允许超订时上限也不是简单capacity；阅读表达式时应把整数房间数与百分比一起算。

```mermaid
sequenceDiagram
  participant U as alice
  participant B as Bookings事务
  participant J as 持久支付任务
  participant W as Worker
  participant P as 模拟支付
  U->>B: 订两晚，expected_total=20005
  B->>B: 检查req、价格、每晚库存
  B->>J: 同事务写charge任务
  B-->>U: PENDING
  W->>J: 领取带租约任务
  W->>P: charge
  P-->>W: paid
  alt 预订仍有效
    W->>B: CONFIRMED且PAID
  else 已取消或过期
    W->>B: 保持终态并安排退款
  end
```

## 用失败来检验理解

两个日期中第二晚库存不足时，第一晚不能留下占用；对应[StoreTests.test_second_night_failure_and_precommit_exception_rollback](../tests/test_store.py#L75)。模拟支付已经扣款但回执丢失时，不能把未知结果当明确拒绝；重试必须沿原任务身份恢复。

更容易漏掉的是支付调用过程中用户取消。[WorkerSagaTests.test_cancel_during_payment_requires_compensation_without_confirming](../tests/test_worker_reliability.py#L267)断言最终不能重新确认取消的预订，而应推进补偿。[StoreTests.test_multinight_atomicity_price_snapshot_idempotency](../tests/test_store.py#L60)则验证两晚报价、原子性和重复请求。

## 微观三：坏响应为什么不能等同“预订失败”

分片可能已经提交预订，但返回的JSON、长度或framing损坏。网关此时不知道最终结果，
不能用400暗示用户只是填错请求，也不能自动重发写入。沿
[request](../src/hotel_reservation/transport.py#L121)的响应校验异常，到
[_Handler.handle_request](../src/hotel_reservation/http.py#L389)的503映射读一次。

```mermaid
flowchart TD
  U["客户端提交预订与request_key"] --> G["网关校验身份、路由和JSON"]
  G --> F["向分片转发一次"]
  F --> R{"收到什么响应？"}
  R -->|"有效成功／明确业务4xx"| B["返回业务结果或保留4xx"]
  R -->|"损坏JSON／长度／framing"| E["503：结果未知，不自动重发"]
  E --> K["客户端用原键和原内容重试"]
  K --> D["分片幂等检查返回原订单当前状态"]
```

RPC只将已经收到的上游响应校验失败转为OSError；直接JSON解析、本地配置和请求编码
错误仍保留ValueError。分片明确返回的409也不能被包装成“未知付款”。已有订单ID时
可读详情；未拿到ID时，用原request_key重试才能恢复同一预订，不能先换新键。

[HTTPTests.test_gateway_invalid_upstream_response_is_unknown_without_write_retry](../tests/test_http_reliability.py#L247)
用真实网关PUT和受控HTTP分片验证5类坏响应、脱敏503及每次只转发一次；它不模拟实际
预订提交。持久幂等、库存和退款另由前述Store测试及六进程示例验证。

[open-source-review_cn.md](../docs/open-source-review_cn.md)给出DeathStarBench酒店预订与Eventuate订单Saga的
固定源码链接。读前者时区分“先检查后逐晚写”和本地多夜事务；读后者时区分额度预留
与真实扣款退款，不把同名Saga直接当作相同协议。

## 自查与练习

- **为什么availability说有房，reserve仍可能409？** 查询后库存或价格会变化，写事务必须重新检查。
- **支付成功能直接把所有预订改CONFIRMED吗？** 不能，取消、过期与租约条件仍需复核。
- **重复取消会把reserved减成负数吗？** release依据held保护，测试还需确认只释放一次。

练习：在两晚中只给第一晚足够库存，尝试预订后重新查询两晚。预期失败，第一晚reserved也保持原值。再按req重试已成功订单，预期返回同一预订且库存不变。运行步骤见[README_CN.md](../README_CN.md)。


---

[返回全仓库学习导航](../../docs/learning-guide_cn.md) · [项目运行说明](../README.md)

[导读原稿](../../tools/learning_guides/hotel-reservation-system.md)。正文与图按当前实现编写；源码链接和摘录由生成器绑定到当前定义。修改行为后应同时复核案例、时序和解释，再重新生成；定义与链接校验不等同于业务回归测试。
