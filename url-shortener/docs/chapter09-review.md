# 第 09 章核对与实现

原书核心链路包括缩短、去重、数据库持久化、缓存回源和 HTTP 301/302 重定向。本轮补齐 SQLiteStorage、有限容量 LRU 读缓存和 WSGI HTTP 适配器。

| 原书要点 | 实现 |
| --- | --- |
| Hash + 冲突处理、Base62 | 保留两种策略；Base62 编码严格校验非负整数，本地自增器增加线程锁。默认仍使用 CRC32 哈希策略。 |
| 数据库与重复 URL | SQLite 唯一索引约束短码和长 URL；BEGIN IMMEDIATE 将查询、生成和保存放在同一事务，多实例共享文件时不会创建重复映射。连接按操作关闭。 |
| 缓存回源 | 默认最多 1024 条映射的 LRU，cache_capacity=0 可关闭；数据库查询不持缓存锁，命中仍记录点击。 |
| POST/GET | create_app 接收 URLShortener，提供 POST /api/v1/data/shorten，JSON 字段 longUrl，返回 shortURL；GET /{code} 或 /api/v1/{code} 返回 Location 和 301/302。302 使用 no-store。无效输入 400，未知链接 404；请求体最多 16 KiB。 |
| 已发链接不可变 | 内存和 SQLite 保存均拒绝冲突；之前允许覆盖的 save 行为改变为 ValueError，保护旧链接及缓存一致性。 |

## 启动示例

```python
from wsgiref.simple_server import make_server
from url_shortener.shortener import URLShortener
from url_shortener.sqlite_storage import SQLiteStorage
from url_shortener.http import create_app

service = URLShortener(storage=SQLiteStorage("urls.db"), domain="http://localhost:8080")
with make_server("127.0.0.1", 8080, create_app(service)) as server:
    server.serve_forever()
```

示例服务器供本机演示；部署使用支持并发的 WSGI 服务器。SQLite 会串行化写事务，读缓存属于各实例；未实现负载均衡器、分库分片、数据库复制或跨机器部署。不能据此声称达到原书 3650 亿条数据的规模。

Base62Strategy 的本地自增器不是分布式 ID 分配器；多实例或重启需要外部唯一 ID 生成器，否则新映射冲突将被拒绝，不会覆盖旧链接。CRC32 输入空间只有 32 位，补齐到 7 字符不能扩展哈希容量。Bloom filter 是原书可选优化，尚未引入；数据库唯一约束才是正确性的依据。

映射没有修改/删除接口，因而缓存不需要失效广播；手工修改数据库不属于该契约。点击明细仍在实例内存中，无持久化和保留期限，301 浏览器缓存命中不会被服务统计。限流、全局分析和高可用部署属于待扩展项。

回归覆盖共享数据库的并发去重与重新打开、两个后端的冲突保护、真实 WSGI 请求/响应及缓存命中；完整项目测试结果见 DEEP_REVIEW_PROGRESS.md。
