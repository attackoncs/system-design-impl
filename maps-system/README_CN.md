# 地图系统（第19章）

实现有向道路图、三种模式路线／ETA、位置批量上报、交通聚合、按需路由瓦片、
版本化 GeoJSON和PNG瓦片、独立HTTP副本及区域读故障恢复。
示例道路为CC0合成数据，不是真实出行导航。

## 运行

Python 3.10+，只使用标准库（含SQLite RTree），无需外部账户。在本目录执行：

```powershell
python -m unittest discover -s tests -v
python examples/network_demo.py
python tools/benchmark.py --output .runtime/benchmark.json
```

示例启动主服务、只读副本、区域网关三个独立进程及两个独立数据库；通过真实HTTP
验证路线、封路重规划、PNG/GeoJSON输出和主服务停止后的副本读取。
退出停止自有进程，日志和实验数据库留在忽略目录 `.runtime/lab-<uuid>/`。

持久服务先设置 `$env:PYTHONPATH = 'src'`：

```powershell
python -m maps_service.cli import --graph examples/roads.json
python -m maps_service.cli provision --user admin --admin --token-file .runtime/admin.token
python -m maps_service.cli provision --user alice --token-file .runtime/alice.token
python -m maps_service.cli serve --port 8095
```

另两个终端各设置相同PYTHONPATH并分别运行：

```powershell
python -m maps_service.cli replica --db .runtime/replica.sqlite --port 8096 --upstream http://127.0.0.1:8095 --token-file .runtime/admin.token
python -m maps_service.cli gateway --port 8097 --regions examples/regions.json
```

默认只监听localhost。凭据文件私有，不能提交；生产部署需要TLS和身份服务。

## 语义与接口

- `POST /v1/route`：origin/destination为节点ID、精确地址或经纬度对象；
  mode支持driving/walking/cycling。可选avoid_tolls、alternatives（1..3）、
  max_nodes。Dijkstra按ETA寻路；候选采用有界单边避让，不是完整K最短路。
  不可达返回404，搜索预算耗尽返回413，不能把耗尽当成不可达。
- `GET /v1/geocode?address=Central` 和 `/v1/reverse?latitude=0&longitude=0`：
  仅覆盖导入数据，反向查询匹配1000米内最近节点，不是全球地址服务。
- `GET /v1/tiles/{graph}/{z}/{x}/{y}.geojson` 或 `.png`：真实矢量／256像素PNG。
  支持z0—21、经度环绕、极区投影限制和跨日期变更线裁切。
- `GET /v1/routing-tiles/{graph}/{x}/{y}`：z16路由瓦片及跨瓦片目标引用。
- `/v1/locations`：POST携带Bearer和reports数组（1..100条），GET只读自己最近
  24小时内的最多100条。报告含event_id/sequence/Unix timestamp/latitude/
  longitude，可选mode和heading。相同ID重试不重复；内容冲突整个批次回滚。
  乱序报告保留但不回退当前位置，时间不能超过过去24小时或未来30秒。
- `/internal/traffic`、`/internal/aggregate`、`/internal/graph`：管理员Bearer，
  分别更新交通、聚合样本、原子替换道路图；`/internal/catalog`导出公开目录。
- `/health`：图UUID/hash、目录／交通版本、就绪状态及副本复制错误。

地图匹配采用100米内短路段切平面近似；缺少明确heading或方向有歧义的报告不产生
速度样本。相同有向道路上前进、相隔1—120秒、速度0.5—50m/s的驾驶报告才参与聚合。
五分钟窗口中至少两个不同用户，各取最近接收的样本。聚合需显式调用，不是Kafka／ML
服务。手动交通参数为edge/speed_mps/closed，可设置过期；只影响驾驶，其他模式用基线。
再次请求路线即重新规划，没有手机端自动后台导航。

## 一致性、隐私与验证

每次路线在同一SQL读事务中绑定图和交通版本；源节点邻接按需加载，LRU容量64个
路由瓦片。渲染缓存128项，按图UUID分开；URL带ETag和immutable缓存头，支持304。
替换道路图后旧URL在源站返回410。内容校验防止副本在相同UUID下接受变更道路。
缓存限制为条目数，密集瓦片仍可能占较大内存。

副本是真实HTTP完整目录复制，只包含道路／地址／交通，不包含位置、用户、凭据或
幂等记录；失败保留完整旧版本。网关需 `X-Region: local`，只在同区域读节点间恢复。
这不是数据库写主选举、云地域部署或真实CDN。

原始位置查询保留期24小时，主服务上报时和每30秒清理；停机期间下次启动恢复物理
清理。不生成精确坐标访问日志；磁盘和备份仍需保护。

14项测试通过；包括225组最短路对照、图切换并发、边界、交通、批次和真实网络故障。
900节点／3480有向边，4客户端60次混合HTTP请求零错误，实测72.17 QPS。
详见[验证报告](benchmarks/results.md)和[设计](docs/design.md)。
生产道路授权、转弯限制、分层加速、交通预测、分布式数据层、TLS、云地域和书中规模
仍待扩展，不能把合成数据实验视为真实导航安全或生产规模达标。
