# 地图系统（第19章）

有向道路寻路/ETA、位置批量上报、交通聚合、分类路由瓦片、版本化GeoJSON/PNG，
补齐持久预生成瓦片与私有导航会话的后台路线更新。示例为CC0合成道路，不是真实导航数据。
[English](README.md)。

## 运行

Python3.10+，只使用标准库（含SQLite RTree）。在本目录执行：

```powershell
python -m unittest discover -s tests -v
python examples/network_demo.py
python examples/adaptive_demo.py
python tools/benchmark.py --output .runtime/benchmark.json
```

示例使用主服务/只读副本/区域网关三个独立进程和独立目录数据库，真实HTTP验证路线、
交通和瓦片故障恢复。退出停止自有进程，日志留在忽略目录`.runtime/lab-<uuid>/`。

另一个adaptive_demo示例通过CLI预生成和真实HTTP/SSE验证私有导航自动重规划。

每个终端先设置`$env:PYTHONPATH='src'`，持久服务可依次运行：

```powershell
python -m maps_service.cli import --graph examples/roads.json
python -m maps_service.cli provision --user admin --admin --token-file .runtime/admin.token
python -m maps_service.cli provision --user alice --token-file .runtime/alice.token
python -m maps_service.cli precompute --tiles examples/tiles.json --tile-archive .runtime/tiles.sqlite
python -m maps_service.cli serve --port 8095 --tile-archive .runtime/tiles.sqlite
```

另两个终端分别运行：

```powershell
python -m maps_service.cli replica --db .runtime/replica.sqlite --port 8096 --upstream http://127.0.0.1:8095 --token-file .runtime/admin.token
python -m maps_service.cli gateway --port 8097 --regions examples/regions.json
```

归档文件必须与目录数据库分开；副本可另传--tile-archive，但公开目录复制不会分发归档文件。
凭据独占写入并fsync后才创建SQL用户，普通失败仅清理身份仍匹配的自有新文件。
文件和备份需保护，默认只监听localhost，TLS/生产身份仍需部署。

## 主要接口

| 方法/路径 | 行为 |
| --- | --- |
| POST /v1/route | origin/destination；可选mode/avoid_tolls/alternatives/max_nodes |
| GET /v1/geocode?address=Central | 固定数据中精确规范化地址或节点ID |
| GET /v1/reverse?latitude=0&longitude=0 | 1000米内最近固定节点 |
| GET /v1/tile-urls?latitude=0&longitude=0&zoom=14 | 当前图版本的本格/邻居URL，可选format=png/geojson |
| GET /v1/tiles/{graph}/{z}/{x}/{y}.png或.geojson | 真实256像素PNG或裁切矢量 |
| GET /v1/routing-tiles/{graph}/{z}/{x}/{y} | z16全道路、z12主干/高速、z8高速；层级和跨瓦片入口 |
| GET /v1/routing-tiles/{graph}/{x}/{y} | 兼容z16旧接口 |
| POST/GET /v1/locations | Bearer；批上报1..100条 / 自己保留期内最近100条 |
| POST /v1/navigation | Bearer，主服务；路线选项加可选ttl，建立私有会话 |
| GET/DELETE /v1/navigation/{id} | 本人读取/删除导航会话 |
| POST /v1/navigation/{id}/position | 本人更新{origin: 节点/地址/坐标}并重算 |
| GET /v1/navigation/{id}/events?after=N | 本人有限SSE页；Last-Event-ID重连最新路线 |
| GET /health | 图/目录/交通版本、角色、就绪和已挂载后台组件状态 |

管理员Bearer接口：POST /internal/graph、/internal/traffic、/internal/aggregate，
GET /internal/catalog分别导入图、设交通、聚合和导出公开目录。

起终点支持节点/地址字符串或经纬度对象。mode为driving/walking/cycling，不含班次公交。
结果含geometry、精度5编码polyline、逐段指令和时距；指令是几何提示，不含转弯限制、
信号灯或车道规则。Dijkstra保持同一SQL图/交通快照，搜索仍使用细层准确图；已提供三层
分类视图但未宣称分层寻路加速。候选为有界单边避让，非完整K最短路；max_nodes默认50000，
计所有候选总访问。不可达404、预算耗尽413，ETA为长度/速度而非训练模型。

## 位置和实时导航

报告含event_id/sequence/Unix timestamp/latitude/longitude，可选mode/heading。
同用户事件ID幂等，冲突整批回滚；乱序不回退当前位置。取得SQL写锁后检查过去24小时/
未来30秒的时间窗口。原始报告按接收时间保留24小时，只能本人查询；上报只裁剪该用户，
主服务每30秒后台全局清理，停机或排队会使物理删除延后。公开复制不含用户、凭据、轨迹
或导航会话，不记录坐标/凭据访问日志。

地图匹配为100米内短路段切平面近似。明确航向、同一有向边前进、间隔1..120秒、
速度0.5..50m/s且足够新的驾驶报告才生成样本；五分钟窗口每用户取最后接收样本，至少
两名用户才聚合，未过期的管理员封路不被重开。这是显式SQL处理，没有部署Kafka/ML。

导航会话默认TTL900秒、主服务容量128。后台默认每秒检查，以变化道路包围盒和包含路线
的父瓦片筛选需重算会话，周期全量刷新也发现新近更快路线；计算在写锁外，安装以CAS复核。
SSE返回最新路线状态或heartbeat后关闭，客户端带Last-Event-ID重连并替换展示；中间状态
会合并，不是持久事件回放。事件等待最多5秒，最多使用HTTP容量的四分之三。没有完整手机
后台导航客户端或多主机会话HA。

## 瓦片、并发和验证

XYZ支持z0..21、经度环绕和极区钳制。冷渲染默认限制5000候选边/10000裁切段，密集请求
可能413；路由瓦片导出默认限制5000节点/边。持久TileArchive为独立SQLite本地对象存储参考，
校验图身份/checksum，预生成默认每批256瓦片/64MiB，单对象16MiB。UUID URL、ETag/304
适合接CDN，但未部署云对象存储或CDN；旧代可保留归档，源站仍只服务active图，旧URL返回410。

缓存固定条带合并冷加载，加载器不占全局LRU锁，返回数据隔离；邻接缓存每节点不可变JSON文本，再解析独立边对象，减少嵌套深拷贝。
缓存条目预算不等于RSS。HTTP默认32处理器/10秒套接字期限，CLI用--concurrency/
--request-timeout配置，--navigation-interval/--max-navigation-sessions配置后台导航。
绑定成功才启动后台组件，关闭排空处理器、复制、导航和维护；严格JSON/framing、私有正文前
鉴权，直接传输拒绝重定向，仅在同region切换副本。系统DNS/SQLite/CPU不受套接字期限强制终止。

最终数量和实测见[验证报告](benchmarks/results.md)、[本轮原始数据](benchmarks/review-2026-10-25.json)。
2026-10-20的14项测试/72.17混合QPS为历史结果，不能冒充本轮或证明生产容量。
[章节记录](docs/chapter19-review.md)与[设计](docs/design_cn.md)列明原书映射和范围：
真实道路授权、生产转弯/公交、ML ETA、物理分片、HA、云地域及书中规模仍待扩展。
