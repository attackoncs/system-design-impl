# 附近商家服务（第17章）

<!-- LEARNING_GUIDE_START -->
## 从这里开始读代码

[跟着一个案例读懂系统：架构、状态变化与关键代码详解](docs/learning-guide_cn.md)。
<!-- LEARNING_GUIDE_END -->

已实现 geohash 搜索、商家增删改、持久化幂等、每日原子发布、有界 TTL 缓存、
独立进程 HTTP 副本和按区域配置的读路由。

## 运行

Python 3.10+，运行与测试无第三方依赖。在本目录执行：

```powershell
python -m unittest discover -s tests -v
python examples/cluster_demo.py
python tools/benchmark.py --output .runtime/benchmark.json
python tools/http_benchmark.py --output .runtime/http-benchmark.json
```

示例实际启动主服务、两个副本和网关共4个进程，使用3个独立数据库。
验证日期变更线两侧搜索、副本停止后切换、主服务停止后读取旧快照，退出自动清理。

持久运行时，在4个终端分别设置 `$env:PYTHONPATH = 'src'`，每个终端执行一条：

```powershell
python -m proximity.cli primary --db .runtime/primary.sqlite --port 8080 --tokens examples/tokens.demo.json --publication-interval 1
python -m proximity.cli replica --db .runtime/r1.sqlite --port 8081 --tokens examples/tokens.demo.json --upstream http://127.0.0.1:8080
python -m proximity.cli replica --db .runtime/r2.sqlite --port 8082 --tokens examples/tokens.demo.json --upstream http://127.0.0.1:8080
python -m proximity.cli gateway --port 8083 --regions examples/regions.demo.json
```

默认只监听本机；示例凭据是公开演示值，外部使用需换私有随机凭据并启用 TLS。
各角色不能共用 SQLite 文件。虚拟环境内也可 `python -m pip install -e .`。
完整 PowerShell 创建、发布和查询示例见[英文 README](README.md)。

## 接口语义

- `GET /v1/search/nearby`：必填 latitude/longitude；radius 单位米，允许
  500/1000/2000/5000（默认）/20000，不自动扩大。支持 category、open_at、
  limit（1..1000，默认100）和 cursor，按球面距离、ID排序。
- `GET /v1/businesses/{id}`：返回已发布详情及版本。
- `POST /v1/businesses`、`PUT /v1/businesses/{id}`、`DELETE /v1/businesses/{id}`：
  要求 owner Bearer 和 Idempotency-Key，只能修改自己的商家。
- `POST /internal/publish`：admin Bearer，提前强制发布，请求无正文。
- `GET /internal/snapshot`：replication Bearer，导出公开快照。
- `GET /health`：角色、版本、快照日期、就绪状态，以及已挂载后台任务的复制／发布错误。

商家 JSON 必填 name/latitude/longitude；坐标必须是有限 JSON 数值；
可选 address/category/hours。hours 使用 UTC 周时间窗口，例如
`[{"day":1,"start":600,"end":720}]` 表示周二10:00—12:00。
跨夜拆为两天；空窗口表示营业时间未知，open_at 查询排除。
open_at 必须为带时区的 ISO 时间。

增删改先写草稿，次日 UTC 发布时统一更新搜索与详情。主服务 CLI 启动自己的
`DailyPublisher` 后台任务，即使没有读请求也会检查日期并发布；
`--publication-interval` 控制检查间隔（默认1秒，允许1..86400秒）。
构建、安装和复制仍需要时间，因此不保证恰好在午夜可见。当日首次写入先冻结昨日数据，
防止今日商家提前上线。仅使用 `Store` 库时保留按需发布；没有流量也需要发布时，
应显式启动 `DailyPublisher`。也可外部调度：

```powershell
python -m proximity.cli publish --db .runtime/primary.sqlite
```

`--force` 或管理接口用于主动提前发布。副本默认每秒轮询；主服务失联继续读旧版本。
关闭服务会停止后台循环、取消进行中的网络读取并回收自有线程；已开始的数据库发布允许完成。

幂等记录7天后可过期复用，在后续写入时清理。分页绑定完整查询和快照版本，变化返回409，
需重新请求第一页。通过 next_cursor 遍历全部结果；游标有严格格式校验，但不承担鉴权。
网关要求 `X-Region: local`，只在同区域配置的副本间重试网络错误、重定向、5xx或非法响应；
4xx（包括游标冲突）直接返回。不会跟随重定向目标。准入和所有副本尝试共享总时间预算，
同时限制在途请求数。

HTTP 处理线程、请求／响应正文均有上限；拒绝歧义消息边界、重复字段和非有限 JSON 数值。
精确查询坐标不入数据库、不生成访问日志，缓存按格子／商家和版本建立。
公开商家记录本身仍包含商家坐标。

## 工程结构与验证

地理计算、业务／查询校验、缓存、发布任务和 HTTP 传输已有独立模块。
缓存加载移出全局 LRU 锁，以固定64个锁条带合并同键冷加载，TTL 从加载成功后起算。
通用 Cache 深拷贝隔离可变值；Store 缓存不可变的规范 JSON 文本，在详情／搜索时解析为
独立对象，减少重复字典拷贝并保持嵌套数据隔离。候选正文按每批256条 SQL 批量读取。
发布先在写事务外构建索引，再通过草稿修订号和版本检查原子安装；构建期间的公开读取
可继续使用旧的完整版本。

带日期的完整回归、独立进程演示和实测数据见[验证报告](benchmarks/results.md)；
章节要求与实现的逐项对应见[第17章审查记录](docs/chapter17-review.md)。

## 边界

逻辑分片仍在单个 SQLite 中；副本使用真实 HTTP 完整 JSON 快照，各自持有独立数据库。
缓存为进程内缓存，没有部署 Redis 集群。区域为本机路由配置，没有部署云地域。
构建阶段移出了写事务，但批量安装仍占用 SQLite 写事务，快照／索引占用随数据规模增长的内存；
密集查询为了返回 total 仍需过滤并排序全部候选。网络期限不能强行中断系统 DNS 解析或数据库／CPU 工作。

数据库 WAL 复制、主节点选举、物理分片、TLS 终止、生产身份认证、备份、增量复制和
书中2亿商家／5000 QPS均待扩展。设计见[design_cn.md](docs/design_cn.md)。
