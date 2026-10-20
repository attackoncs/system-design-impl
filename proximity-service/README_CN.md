# 附近商家服务（第17章）

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
python -m proximity.cli primary --db .runtime/primary.sqlite --port 8080 --tokens examples/tokens.demo.json
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
- `POST /internal/publish`：admin Bearer，提前强制发布。
- `GET /internal/snapshot`：replication Bearer，导出公开快照。
- `GET /health`：角色、版本、快照日期、就绪状态及复制错误。

商家 JSON 必填 name/latitude/longitude；可选 address/category/hours。
hours 使用 UTC 周时间窗口，例如 `[{"day":1,"start":600,"end":720}]` 表示
周二10:00—12:00。跨夜拆为两天；空窗口表示营业时间未知，open_at 查询排除。
open_at 必须为带时区的 ISO 时间。

增删改先写草稿，次日 UTC 发布时统一更新搜索与详情；当天首次写入先冻结昨日数据，
防止今日商家提前上线。主服务读请求及副本轮询也触发每日发布，无流量时没有午夜守护
进程，可外部调度 `python -m proximity.cli publish --db .runtime/primary.sqlite`。
`--force` 或管理接口用于主动提前发布。副本默认每秒轮询，主服务失联继续读旧版本。

幂等记录7天后可过期复用，在后续写入时清理。分页绑定查询和快照，变化返回409，
需重新请求第一页。网关要求 `X-Region: local`，只在同区域副本间重试网络错误或5xx。
精确查询坐标不入数据库、不生成访问日志，缓存按格子／商家和版本建立。

## 验证与边界

17项测试通过，包括两极、日期变更线、半径边界、快照并发、事务回滚、缓存、
幂等、权限、进程重启、真实网络复制和故障切换。
10,000合成商家下，4客户端120个真实 HTTP 请求零错误，吞吐29.5 QPS。
延迟和复现命令见[验证报告](benchmarks/results.md)。

逻辑分片仍在单个 SQLite 中；副本使用真实 HTTP 完整 JSON 快照，尚未实现
数据库 WAL 复制、主节点选举或物理分片。区域为本机路由配置，没有部署云地域。
TLS、生产身份认证、备份、数据库HA、增量复制和书中5000 QPS均待扩展。
设计见[design.md](docs/design.md)。
