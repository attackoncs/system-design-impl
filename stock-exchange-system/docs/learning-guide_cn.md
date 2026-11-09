# 第29章：从一笔部分成交读懂有序日志与撮合

先把系统分成三个角色：定序器决定命令先后，撮合机按顺序确定性地改变订单簿，行情服务发布这些变化。`ACCEPTED`只说明命令已进入日志；订单是否被接受、成交多少，要看撮合结果。所有账户、行情时钟和订单均为模拟，不连接真实交易所。

## 贯穿案例：两张同价卖单，买入8股

按测试默认账户与交易时段，先提交bob的`ask1: SELL 100×5`，再提交carol的`ask2: SELL 100×7`，最后alice提交`BUY 110×8`。预期以maker价格100成交：先吃ask1的5股，再吃ask2的3股；ask2剩4股。alice实际花800美分，不是按限价110花880，未使用的预留金额应归还。

```mermaid
flowchart TB
  U["模拟交易用户"] -->|同步命令受理| S["Sequencer"]
  S --> L[("有序不可变命令日志")]
  L -.->|按seq轮询| E["主／备Engine独立重放"]
  E -->|本地事务| B[("订单簿、账户、成交投影")]
  E -->|同事务输出| D[("行情增量batches")]
  D -.->|后台订阅| M["Market行情与报告"]
  M --> Q["深度／K线／私有成交查询"]
```

主备撮合可人工切换，但定序器仍是单点；不要把两套独立重放理解为自动共识选主。

| 想先弄明白什么 | 阅读入口 | 手算或验证的结果 |
|---|---|---|
| 一笔买单为什么分两次成交 | 下方案例 → `reduce_command` → `OrderBook.best` | 5＋3股，按maker价格100结算 |
| 崩溃后为什么不重复成交 | `Sequencer.submit` → `Engine.apply` → `Engine.replay` | 相同请求不生成新命令，相同日志行不再次应用 |
| 什么时候可以提升备机 | 提升检查图 → `Sequencer.promote` | 完整前缀与类型敏感状态均相同才切换epoch |
| 行情怎样防止订单被改写 | `Market.apply` → 行情回归测试 | 原身份保持，成交进度不倒退，已撤订单不能复活 |

章节覆盖和剩余架构见[chapter29-review.md](../docs/chapter29-review.md)；上游Exchange Core、CoinTossX的实际源码差异见[open-source-review_cn.md](../docs/open-source-review_cn.md)。

## 先认清数据层次

| 数据 | 关键字段 | 在案例中的作用 |
|---|---|---|
| 接收身份 | owner＋request_id及内容hash | 网络重试不能再分配一条新命令 |
| 命令日志 | `seq/previous/event/hash` | 先ask1、再ask2、最后买单的确定顺序 |
| 订单 | `side/price/quantity/remaining`等 | ask2从7变4，未成交部分继续留盘 |
| 账户 | cash与持仓的available／held | 下单预留与成交／撤单释放分开 |
| 成交 | 买卖order、quantity、price | 本例两笔数量5、3，价格都100 |
| 行情增量 | seq、head、books、orders、trades | 从同一撮合事务导出，缺口需补快照 |

## 六站源码路线

1. [Sequencer.submit](../src/stock_exchange/storage.py#L112)：输入owner、kind和body，输出seq及ACCEPTED。看去重索引和日志怎样同事务提交。
2. [Engine.apply](../src/stock_exchange/storage.py#L312)：核对前缀、序号和hash，确定哪些命令可以应用。
3. [reduce_command](../src/stock_exchange/matching.py#L46)：核心业务。先跟new订单的校验、资金／持仓预留，再跟撮合循环。
4. [OrderBook.best](../src/stock_exchange/order_book.py#L88)与[OrderBook.consume](../src/stock_exchange/order_book.py#L77)：看最佳价格如何定位，以及同价队列怎样保留时间顺序。
5. [market_batch](../src/stock_exchange/storage.py#L210)：比较新旧状态，把该命令的增量输出读清楚，别误认为行情会重新撮合。
6. [Market.apply](../src/stock_exchange/market.py#L86)及[Engine.replay](../src/stock_exchange/storage.py#L254)：前者检查行情增量，后者从命令重建权威撮合状态。

## 微观一：已收到的seq也不能无条件忽略

[storage.py:318](../src/stock_exchange/storage.py#L318)

```python
with self.transaction() as db:
    tail = self.tail(db)
    seq = row["seq"]
    if seq <= tail["seq"]:
        old = db.execute(
            "SELECT body FROM events WHERE seq=?", (seq,)
        ).fetchone()
        if old is None or json.loads(old[0]) != row:
            raise Error(409, "matching replica prefix conflict")
        return
    if row != entry(tail["seq"] + 1, tail["hash"], row["event"]):
        raise Error(409, "command sequence/checksum gap")
```

重放旧seq时要比较完整日志行；只有内容完全相同才返回。下一条新命令必须由当前tail接上正确的previous和hash，否则报gap。这防止“序号一样但内容不同”的请求污染副本。

## 微观二：订单簿承担价格与时间优先

[order_book.py:88](../src/stock_exchange/order_book.py#L88)

```python
def best(self, symbol, taker_side):
    maker_side = "SELL" if taker_side == "BUY" else "BUY"
    side = self.value[symbol][maker_side]
    sign = -1 if maker_side == "BUY" else 1
    while side["heap"]:
        key = str(side["heap"][0] * sign)
        level = side["levels"].get(key)
        if level is not None:
            return level["head"]
        heapq.heappop(side["heap"])
```

`best`按taker方向选择对手盘，返回最佳价格层的队首订单；它并不判断这个价格是否满足taker限价。真正的可成交判断在[reduce_command](../src/stock_exchange/matching.py#L46)的撮合循环：买单遇到maker价格高于买限价就停止，卖单遇到maker价格低于卖限价也停止。找到最佳候选与判断能否成交是两步。

同价排序不是按订单ID字典序，而是由命令进入订单簿的顺序形成。取消和部分成交不能让后来的订单突然插队。

```mermaid
sequenceDiagram
  participant S as 定序器
  participant E as Engine
  participant B as 订单簿
  participant A as 账户投影
  S->>E: ask1，SELL100×5
  E->>B: 价格100队列尾加入ask1
  S->>E: ask2，SELL100×7
  E->>B: 同价尾部加入ask2
  S->>E: BUY110×8
  E->>A: 按买入限价预留
  E->>B: 先成交ask1的5，再ask2的3
  B-->>E: ask2剩4，maker价100
  E->>A: 结算800并释放多余预留
  E->>E: 同事务保存状态、成交和行情增量
```

## 崩溃与重放要检查什么

[MatchingTests.test_price_time_partial_fill_and_price_improvement](../tests/test_matching.py#L54)直接给出本案例断言：成交卖单顺序ask1、ask2，数量5、3，ask2剩4，alice现金available为999200、held为0。

[SafetyTests.test_command_rollback_keeps_projection_events_and_batches_together](../tests/test_safety.py#L188)检查事务中途失败不会留下“有成交但没行情”或相反的部分结果。[MatchingTests.test_storage_replay_snapshot_tail_immutability_and_gap](../tests/test_matching.py#L147)检查日志重放、快照与缺口。

真实的恢复不是拿旧snapshot就信任它：快照必须与日志前缀及状态校验一致，再应用尾部。行情增量保留窗口以外的消费者要重新拉快照，而不是静默跳过丢失的book变化。

## 微观三：同序号、同链头还不够

```mermaid
flowchart TD
  R["候选备机：seq、head、state、state_hash"] --> P{"完整定序日志前缀一致？"}
  P -->|否| E["拒绝提升，保持当前epoch"]
  P -->|是| V["候选状态自校验＋定序日志独立重放"]
  V --> H{"规范化状态hash相同？"}
  H -->|否| E
  H -->|是| A["事务内切换active并提升epoch"]
  A --> F["发布前后及消费端核验epoch和前缀"]
```

[storage.py:197](../src/stock_exchange/storage.py#L197)

```python
    expected = {}
    for row in rows:
        expected = apply_command(expected, row["event"])
    if replica["state_hash"] != checksum(expected):
        raise Error(
            409, "candidate state differs from independent sequencer replay"
        )
    current = db.execute("SELECT active FROM control").fetchone()[0]
    if node != current:
        db.execute("UPDATE control SET epoch=epoch+1,active=?", (node,))
return self.control()
```

Python认为`False == 0 == 0.0`，但它们是三种不同的JSON数据。提升、快照和投影核验比较规范化校验和，使错误类型无法靠值相等通过；字典插入顺序不影响规范化结果。[ReplayIntegrityTests.test_promotion_rejects_equal_python_values_with_different_json_types](../tests/test_replay_integrity.py#L13)直接保护这个边界。

发现坏投影也不能继续撮合。[audit](../src/stock_exchange/audit.py#L4)检查余额、数量和序号的严格整数类型；[ReplayIntegrityTests.test_non_integer_balance_blocks_matching_until_rebuild](../tests/test_replay_integrity.py#L45)把现金改成整值浮点数，验证新命令被拒绝、日志应用序号不推进，原日志重建后可以继续。

行情投影只接收合法的订单演进：已有订单的属主、意图和创建序号不变，filled只能增加、remaining只能减少，FILLED/CANCELED终态不能改写，新订单的seq必须等于本批次。[MarketTransitionTests.test_unfilled_order_cannot_change_owner_in_later_batch](../tests/test_replay_integrity.py#L71)和[MarketTransitionTests.test_canceled_order_cannot_reopen_even_with_matching_book_levels](../tests/test_replay_integrity.py#L83)验证即使L2总量能对上，也不能改写身份或复活订单；失败会回滚游标、公开增量和私有报告。

## 自查与练习

- **同价买单谁先成交？** 在同一价格层按进入订单簿的时间顺序，受确定命令顺序约束。
- **买限价110为什么成交100？** 对手方maker价格100更优，未用预留需释放。
- **Engine已经apply但应答丢失会重复成交吗？** 相同seq和完整内容重放返回，不再次归约。

练习：把两张卖单改为ask1价格105、ask2价格100，再买入3股。预期先吃价格100的订单，即使它更晚到达；这说明先比较价格，再比较同价时间。配置与模拟交易时段见[README_CN.md](../README_CN.md)。


---

[返回全仓库学习导航](../../docs/learning-guide_cn.md) · [项目运行说明](../README.md)

[导读原稿](../../tools/learning_guides/stock-exchange-system.md)。正文与图按当前实现编写；源码链接和摘录由生成器绑定到当前定义。修改行为后应同时复核案例、时序和解释，再重新生成；定义与链接校验不等同于业务回归测试。
