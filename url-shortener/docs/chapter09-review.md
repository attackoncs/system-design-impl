# 第 09 章核对与实现

## 2026-11-09 点击明细有界保留

- 问题：`URLShortener` 每次解析向 `_clicks[code]` 列表追加 ClickRecord，计数用 `len(list)`，热门链接的明细无限增长。
- 策略：新增构造参数 `click_retention_max_records`（默认每链接1000条，0不保留明细，None不限）与 `click_retention_max_age`（默认30天，None不限）。总数改为每链接独立的精确计数器，不受淘汰影响；明细放入 `deque(maxlen=N)`，满时自动丢弃最旧记录。
- 复杂度：时间戳在点击锁内生成，deque 按时间有序；写入后从左端弹出过期记录，每条记录最多弹出一次，单次点击均摊O(1)，无全量扫描；读取前同样修剪。内存 O(链接数 + 链接数×N)。项目只有内存点击明细，SQLite 不存点击，因此无需新增索引。
- 接口：`get_click_count` 仍返回精确总数；`get_click_records` 返回保留窗口副本（由旧到新），响应形状不变。非法参数抛 ValueError。
- 验证：/tmp 临时脚本覆盖条数上限、时长过期、0不保留、非法参数和8线程4000次并发计数；原有145项测试通过。持久化分析事件与集群聚合仍属部署边界。

原书核心链路包括缩短、去重、数据库持久化、缓存回源和 HTTP 301/302 重定向。此前补齐 SQLiteStorage、有限容量 LRU 读缓存和 WSGI HTTP 适配器。2026-10-31进一步补充事务内持久ID、已有URL只读快路径和中文网址传输，最新[开源对照与验证记录](open-source-review_cn.md)说明完整边界。

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

Base62Strategy 的默认本地自增器不是分布式 ID 分配器。持久场景可将同一个 SQLiteStorage 同时传给 URLShortener 的 storage 和 Base62Strategy 的 id_generator；多个进程共享文件时可协调发号、重启后继续递增。跨数据库仍需外部唯一ID方案。CRC32 输出空间只有32位，补齐到7字符不能扩展哈希容量。Bloom filter 是原书可选优化，尚未引入；数据库唯一约束才是正确性的依据。

映射没有修改/删除接口，因而缓存不需要失效广播；手工修改数据库不属于该契约。点击明细仍在实例内存中、无持久化，保留策略见文首 2026-11-09 小节；301 浏览器缓存命中不会被服务统计。限流、全局分析和高可用部署属于待扩展项。

当前145项测试通过；4进程1000次创建和真实HTTP验证见[原始结果](../benchmarks/review-2026-11-01.json)。演示统一使用 `python -m examples.demo_server --database urls.db`，创建路径为 `/api/v1/data/shorten`。
