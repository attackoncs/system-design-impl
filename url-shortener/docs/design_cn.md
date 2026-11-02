# 设计文档：URL 短链服务

本章核心是不可变的双向唯一映射。生成策略给出候选码，存储以原子去重和唯一约束决定是否接受；HTTP 只适配请求和响应。完整[学习路线与时序图](learning-guide_cn.md)及[原书/开源对照](open-source-review_cn.md)提供宏观入口，以下链接直接对应维护中的源码，避免在设计文档复制第二套实现。

## 组件与依赖方向

| 组件 | 职责 | 源码 |
|---|---|---|
| URLShortener | 校验、短链化、解析、LRU、点击记录 | [shortener.py](../src/url_shortener/shortener.py) |
| HashCollisionStrategy / Base62Strategy | CRC32冲突重试，或完整编码唯一 ID | [strategies.py](../src/url_shortener/strategies.py) |
| StorageBackend / InMemoryStorage | 存储契约；内存双字典和 RLock | [storage.py](../src/url_shortener/storage.py) |
| SQLiteStorage | 持久双向唯一映射、共享事务、持久 ID | [sqlite_storage.py](../src/url_shortener/sqlite_storage.py) |
| URLMapping / RedirectType / ClickRecord | 不可变对象与301/302枚举 | [models.py](../src/url_shortener/models.py) |
| redirect_uri | 校验 HTTP(S)网址并转换为 ASCII URI | [urls.py](../src/url_shortener/urls.py) |
| create_app | 16KiB请求限制、JSON、重定向与错误状态 | [http.py](../src/url_shortener/http.py) |
| 演示入口 | 复用 WSGI，不维护第二套协议 | [demo_server.py](../examples/demo_server.py) |

策略依赖存储抽象，不依赖 HTTP。SQLite 通过 `IDGenerator.next_id()` 协议接入 Base62，无需在编排器内按存储类型分支。核心零运行时依赖，Python >=3.9。

## 写入：读取快路径与事务内重查

1. `shorten` 校验 URL 和重定向配置。只接受 HTTP(S)、有效主机/端口及301/302；出错时不保存映射。
2. `get_or_create` 先只读查长 URL。映射不可变，所以查到后可直接返回，不必等待写锁。
3. 未命中时，SQLite 执行 `BEGIN IMMEDIATE`，取得写事务后重查。同一文件中另一个实例可能已创建该 URL，因此不能省略重查。
4. 仍无映射才执行 factory。策略生成短码，`save` INSERT，code/url 唯一约束兜底；成功 COMMIT，异常 ROLLBACK。
5. factory 和 ID 分配复用线程局部连接；操作结束关闭连接，不跨线程共享 sqlite3 连接。

内存实现把查询、生成和写入置于同一个 RLock；自定义后端必须自己提供并发原子的 `get_or_create`，基类回退实现只保证顺序使用。

## 持久 ID 与策略

```python
storage = SQLiteStorage("urls.db")
service = URLShortener(storage=storage, strategy=Base62Strategy(id_generator=storage))
```

同一实例必须使用**同一个 storage 对象**，防止发号器另开连接等待自己的写事务。不同进程创建各自对象并共享文件。`id_sequence` 只有一条计数行，事务内递增；重复 URL 不发号，保存失败则一起回滚。独立 `next_id()` 调用会提交自己的事务，因此允许有跳号，契约不是连续无缺口序列。

首次初始化以已有 `urls.id` 最大值起步，兼容旧版从1逐次创建的本地 Base62 数据。任意外部 ID 或混合生成策略需先迁移并协调序列，不能仅靠行号推导短码 ID。SQLite 最大 ID 为 `2**63-1`，耗尽报 OverflowError，不回绕。

Base62 字母表为 `0-9a-zA-Z`。编码左补零至至少7位，不截断；`62**7` 编码为8位。唯一性取决于 ID 来源，默认 `AutoIncrementIDGenerator` 只在单进程内线程安全，不能跨重启发号。

默认哈希策略使用32位 CRC32，候选被占用时追加后缀重试，默认最多10次重试。补成7位不增加32位哈希空间，不宣称支持3.5万亿条哈希映射；不保证所有不同 URL 都能在有限重试内成功。

## 读取、缓存与点击

`resolve` 在缓存锁内查找并维护 LRU，未命中后在锁外读存储，再短暂持锁回填/淘汰。容量默认1024，0禁用。映射不支持修改或删除，因而实例缓存没有更新失效协议。

不存在的短码抛 `ShortCodeNotFoundError`；成功后记录一次 ClickRecord，命中缓存也记录。点击列表由独立锁保护，查询返回副本。统计仅属于本实例，重启丢失、没有明细保留上限；高流量部署应接入持久事件与聚合服务。

## URL 与 HTTP 边界

存储和库 `resolve` 保留输入原串，按字符串精确去重。HTTP Location 才做 IDNA 主机转换和 UTF-8 百分号编码，保留 `%xx` 转义；不合并不同 URL，也不下载目标地址。

转换还保留IPvFuture方括号及空 `?` / `#`。标准库IDNA2003会折叠某些Unicode域名，因此以NFC往返比较拒绝有损转换，要求显式ASCII ACE域名；不自行实现完整IDNA2008。详见[标准与边界](open-source-review_cn.md)。

| 请求/情况 | 行为 |
|---|---|
| POST `/api/v1/data/shorten`，JSON `longUrl` | 200 + JSON `shortURL` |
| GET `/<code>` 或 `/api/v1/<code>` | 301/302 + ASCII Location |
| 302重定向 | `Cache-Control: no-store` |
| 未知短码/路径 | 404 |
| 非法 URL/JSON/请求长度/过深 JSON | 400；请求体限制1..16384字节 |
| 错误重定向配置 | 库抛 ValueError，保存前失败 |
| 唯一约束冲突 | 拒绝覆盖，库抛 ValueError |

301可能被浏览器缓存，服务端点击数只覆盖实际到达并调用 `resolve` 的请求。HTTP适配器没有集群统计接口；旧演示 `/shorten`、`/stats` 已被统一入口取代。

## 一致性与扩展边界

当前 SQLite 是本地单写者数据库，未实现分片、主从、跨机房 ID 或集群缓存。Web 适配层可以托管于生产 WSGI 服务器；标准库演示服务器为单线程。限流可在 HTTP 入口前集成第5章模块，负载均衡、数据库复制与分析系统属于部署扩展，不应把小规模验证当作书中百亿规模证明。可选 Bloom filter 未添加，唯一索引仍是判断依据。

## 可运行的验证

- [原有章节测试](../tests/test_chapter_review.py)：共享数据库相同 URL 去重、重启、拒绝覆盖与缓存。
- [本轮回归](../tests/test_open_source_review.py)：持久 ID 多实例、回滚、序列上限、旧库初始化、写锁下只读返回、中文/IPv6/已转义网址、非法配置。
- [属性测试](../tests/test_properties.py)：格式、往返、幂等、点击准确性等；大 ID 不截断另有回归测试。
- [跨进程与真实HTTP工具](../tools/verify_concurrency.py)及[结果](../benchmarks/REPORT.md)。
