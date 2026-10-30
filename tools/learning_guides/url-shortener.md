# 第9章：跟着同一个URL走完创建、重定向与缓存

短链接服务的核心是一张**不可变双向唯一映射**：长URL对应短码，短码再找回长URL。生成策略决定“候选短码是什么”，存储决定“并发请求最终保存哪条映射”；HTTP层只是把这两个操作暴露成创建和重定向。

## 一个贯穿全篇的请求

采用 `SQLiteStorage` 保存映射；连续两次缩短 `https://example.com/guide`。把首次得到的短码记为 `c`，不预先猜随机／哈希策略产生的具体字符。两次返回应相同，数据库只保留一条该URL的映射。随后调用 `resolve(c)` 两次：两次返回原URL和重定向类型，首次回源，后续可以命中本实例缓存；本实例点击数应增加2。

```mermaid
flowchart LR
  A[HTTP客户端] -->|同步创建或重定向| B[HTTP适配]
  B --> C[URLShortener]
  C -->|生成候选码| D[可插拔策略]
  C -->|唯一映射事务| E[(SQLite urls)]
  C -->|读命中或回填| F[本实例LRU缓存]
  C -->|成功解析后记录| G[本实例点击记录]
```

本页选择持久映射方案，默认不传存储时则使用内存实现。点击记录与LRU仍在进程内；重启后映射能恢复，并不表示点击记录也持久化了。

## 先分清三种状态

| 数据 | 关键字段 | 事实还是派生 |
|---|---|---|
| SQLite `urls` | `code/url/redirect/created`；code与url分别唯一 | 链接映射事实，重启后保留 |
| `URLMapping` | `short_code/long_url/redirect_type/created_at` | 不可变业务对象 |
| `_cache` | short_code → mapping，维护最近使用顺序 | 可丢弃的读取加速 |
| `_clicks` | short_code → ClickRecord列表 | 进程内观测，和映射事务不同 |
| 候选短码 | 策略函数的计算结果 | 未保存前不等于已接受链接 |

## 六站阅读路线

1. {{symbol:src/url_shortener/shortener.py:URLShortener.shorten}}：输入长URL，输出完整短URL。先看最终的 `get_or_create`，理解原子性不在HTTP Handler里。
2. {{symbol:src/url_shortener/shortener.py:URLShortener._validate_url}}：阻止空值、控制字符和非HTTP(S)地址进入策略层；下一站才开始创建候选码。
3. {{symbol:src/url_shortener/strategies.py:Base62Strategy.generate}}：理解策略只负责码的构造；切回服务代码看它如何被factory调用。
4. {{symbol:src/url_shortener/sqlite_storage.py:SQLiteStorage.get_or_create}}：把查询、factory和保存放在一次事务中；这是案例两次创建得到同一映射的关键。
5. {{symbol:src/url_shortener/shortener.py:URLShortener.resolve}}：输入短码，先查LRU，再查SQLite；成功后记录点击，输出 `(long_url, redirect_type)`。
6. {{symbol:src/url_shortener/http.py:create_app}}：最后再看如何把业务结果转换成HTTP状态和Location，避免把业务方法返回值误认为已经发出了重定向响应。

## 微观一：为什么不是先查再单独保存

{{excerpt:src/url_shortener/sqlite_storage.py:SQLiteStorage.get_or_create:0:16}}

`BEGIN IMMEDIATE` 在读取前取得写事务。查询、factory内的策略操作和 `save` 复用线程局部连接，因此第二个共享同一文件的实例会在前一个事务结束后重新看到已有URL。成功后提交，任意异常则回滚。

这段代码保证的是本地SQLite的映射接受语义。不能据此推导跨数据库集群的全局唯一性，也不能假设默认进程内ID生成器适合多实例统一发号。

```mermaid
sequenceDiagram
  participant A as 调用方
  participant S as URLShortener
  participant D as SQLiteStorage
  participant H as 生成策略
  A->>S: shorten(相同长URL)
  S->>D: get_or_create(URL, factory)
  D->>D: BEGIN IMMEDIATE并查询URL
  alt 已有映射
    D-->>S: 返回原映射
  else 尚未创建
    D->>H: factory生成候选短码
    H-->>D: 候选映射
    D->>D: INSERT并COMMIT
    D-->>S: 返回新映射
  end
  S-->>A: domain加短码
```

## 微观二：锁只保护缓存结构，回源在锁外

{{excerpt:src/url_shortener/shortener.py:URLShortener.resolve:17:12}}

首先在 `_cache_lock` 内取值并更新LRU顺序；未命中后，数据库读取发生在锁外。这样一个慢回源不会堵住另一个已经命中的键。拿到映射后再短暂持锁回填、淘汰，缓存容量为0时不回填。

失败短码在点击记录之前抛 `ShortCodeNotFoundError`；缓存命中也仍会走后面的点击记录，因此不能把缓存命中等同于“无需记录解析”。

## 失败路径：冲突不能覆盖旧链接

{{symbol:src/url_shortener/sqlite_storage.py:SQLiteStorage.save}} 使用INSERT与唯一约束；若同一个code已经属于另一条映射，不会执行覆盖。候选策略可在自身契约内尝试新码，最终仍不能悄悄改写旧链接的目标。

- {{symbol:tests/test_chapter_review.py:test_shared_database_atomic_creation_and_restart}}：并发共享数据库创建相同URL，检查相同返回以及重启后仍能读取。
- {{symbol:tests/test_chapter_review.py:test_collision_never_overwrites_old_link}}：强制短码冲突，观察旧映射保持不变。
- {{symbol:tests/test_shortener.py:TestResolve.test_resolve_records_click_analytics}}：成功解析后才增加点击记录。

## 读完应能解释

- **重复缩短相同URL会改成新的302或301吗？** `get_or_create` 返回已有不可变映射，不把新参数覆盖到旧记录。
- **SQLite重启恢复能保证点击数恢复吗？** 不能，点击保存在实例内存中。
- **为什么保存接口还要唯一约束，策略已经检查过存在性？** 检查和保存之间可能有并发，最终冲突必须由接受数据的一层守住。

## 动手练习

把缓存容量设为1，依次解析 `c1`、`c2`、`c1`。预期最后一次 `c1` 再次回源，两个URL映射从未消失；本实例点击计数分别为2和1。然后用同一SQLite文件重建实例，预期映射仍在、点击计数重新从0开始。运行方式见 {{file:README_CN.md}}。
