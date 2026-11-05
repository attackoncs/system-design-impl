# 第19章：开源源码对照与实现核对

日期：2026-11-04。对照根目录《System Design Interview.md》第19章“Google Maps”。

结论：本地参考实现覆盖位置批上报、导航/ETA、地图渲染及动态导航的核心闭环。
本轮未发现需要修复的源码缺陷，保留现有实现；更新开源差异、验证记录和学习指南。
真实道路、生产分层寻路、预测ETA及云端规模仍是明确的后续工作，未标为已完成。

## GitHub开源参考

本轮分别搜索Valhalla、OSRM，并阅读以下路由瓦片、缓存、分层限制与候选路线的
实际源码。链接固定到检索所得提交，便于复核；参考职责与算法边界，未复制代码、
安装上游项目或增加其运行依赖。

| 项目与固定版本 | 实际阅读的源码 | 与本章相关的机制 |
| --- | --- | --- |
| [Valhalla](https://github.com/valhalla/valhalla/tree/2085e78a517a0680a39d3b89efb7eff1f5cf9201) | [三层瓦片及父层](https://github.com/valhalla/valhalla/blob/2085e78a517a0680a39d3b89efb7eff1f5cf9201/src/baldr/tilehierarchy.cc#L12)、[GraphReader按需加载](https://github.com/valhalla/valhalla/blob/2085e78a517a0680a39d3b89efb7eff1f5cf9201/src/baldr/graphreader.cc#L597)、[缓存配置与同步](https://github.com/valhalla/valhalla/blob/2085e78a517a0680a39d3b89efb7eff1f5cf9201/src/baldr/graphreader.cc#L440)、[分层扩展限制](https://github.com/valhalla/valhalla/blob/2085e78a517a0680a39d3b89efb7eff1f5cf9201/valhalla/sif/hierarchylimits.h#L35) | 按道路级别组织highway/arterial/local层；GraphId包含层与瓦片身份。先查缓存，再读取mmap或本地瓦片，按配置可回源URL，缺失明确返回空。搜索扩展限制结合向上转换次数与到终点距离，并提供放宽限制重试机制。 |
| [OSRM Backend](https://github.com/Project-OSRM/osrm-backend/tree/746c97c1c8ef7ebeb7acb9b986cf6c4e04f82caf) | [MLD候选路线参数](https://github.com/Project-OSRM/osrm-backend/blob/746c97c1c8ef7ebeb7acb9b986cf6c4e04f82caf/src/engine/routing_algorithms/alternative_path_mld.cpp#L27)、[候选筛选与解包](https://github.com/Project-OSRM/osrm-backend/blob/746c97c1c8ef7ebeb7acb9b986cf6c4e04f82caf/src/engine/routing_algorithms/alternative_path_mld.cpp#L875) | 从双向搜索相交的via候选构造路径；先按局部最优、单元共享率筛选压缩路径，再限制解包数量，按详细路径共享率及实际时长相对最短路的增幅筛选结果。候选质量与候选计算量分别控制。 |

### 用于本地审查的依据

1. **有三层数据不等于完成分层寻路。** Valhalla将道路层、图ID、读取器和扩展限制
   联系起来。本地z16/z12/z8分类视图与跨层入口可用于学习数据布局；若仍在细图上
   执行Dijkstra，就不能把导出三层视图写成已取得分层搜索加速。
2. **按需读取与有限缓存要一起验证。** Valhalla的GraphReader先查缓存，只在缺失
   时读取瓦片，且存在缓存大小与同步配置。本地应核对图身份/版本是否进入缓存键、
   冷加载是否占全局锁、返回数据能否污染共享值，以及密集瓦片的工作量边界。
3. **候选路线既要正确，也要控制重复和成本。** OSRM的实现会筛选相似度、绕行
   幅度和局部合理性。本地单边排除候选可以保持简单，但应准确标为有界候选方法，
   不等同完整K最短路或OSRM的MLD候选算法；所有尝试仍需共享访问预算。
4. **存储层次与搜索层次需要区分。** 按需瓦片能降低一次性图加载，但不自动减少
   算法遍历的节点数；道路类别视图也不能替代允许性、方向和费用过滤。正确性应由
   独立最短路参考、跨瓦片路线与并发图/交通快照测试共同确认。

### 适用范围与差异

- Valhalla的分层GraphId和瓦片布局不是本地XYZ的z16/z12/z8协议，不能直接交换
  文件或把层号当Web Mercator缩放级别。上游缓存的size配置也不证明本地条目预算
  就是进程RSS上限。
- OSRM阅读范围是MLD候选路由实现；本轮未构建其真实道路数据、运行引擎或比较
  性能。不把“存在候选结果”解释为完整K最短路径，也不照搬其中经验阈值。
- 本地还有图版本、私有导航会话、交通聚合、SSE和PNG/GeoJSON渲染等职责。选读
  的上游路由模块不能证明这些本地能力已完成，也不是本章全部服务的实现证据。
- 真实道路许可、转弯规则、公交班次、预测ETA、物理分片、多地域、CDN与写主HA
  仍需独立工程和验证，不能由两套开源引擎的存在推定本地生产能力。

## 章节覆盖与源码路径

完整逐项对照见[章节映射](chapter19-review.md)。本轮重点沿以下调用链检查边界，
未为选读上游算法引入额外依赖或替换已通过验证的本地实现。

| 章节链路 | 本地入口及关键约束 | 验证依据 |
| --- | --- | --- |
| 路线、候选与ETA | [route](../src/maps_service/navigation.py)在同一图/交通快照内寻路；[outgoing](../src/maps_service/store.py)保持方向、模式与费用约束；所有候选共用访问预算 | 225组Floyd–Warshall独立对照、跨瓦片/候选预算、ETA与指引一致 |
| 路由瓦片及缓存 | [routing_tiles](../src/maps_service/routing_tiles.py)暴露三类视图与入口；[cache](../src/maps_service/cache.py)合并冷加载、隔离返回值；细图邻接按节点缓存 | 三类道路/父子关系、跨图身份、并发加载；按需读取的实测只说明加载范围 |
| 定位与交通 | [telemetry](../src/maps_service/telemetry.py)先冻结批次，获得写锁后校验时间/TTL；新鲜驾驶样本按用户聚合并保护管理员封路 | 原子回滚、幂等/乱序、锁等待后过期、离线数据与封路保护 |
| 动态导航 | [tracking](../src/maps_service/tracking.py)在写锁外计算，按revision及expires条件接受结果；按区域失效并周期全量刷新 | 旧刷新不覆盖新位置、不恢复删除/过期会话；实际HTTP/SSE封路重规划 |
| 瓦片与归档 | [render](../src/maps_service/render.py)绑定active图；[artifacts](../src/maps_service/artifacts.py)独立归档、校验长度/checksum、有界原子发布 | 真实PNG/GeoJSON、重启命中、损坏/不可变键、换图与工作预算 |
| 公开副本及网络 | [http](../src/maps_service/http.py)有界准入、本人鉴权和有限SSE；[transport](../src/maps_service/transport.py)限制总期限、响应大小及重定向 | 慢I/O与协议故障测试；三个真实进程、独立目录数据库、断主后公开读取 |

### CodeGraph与人工核实

使用CodeGraph沿route、outgoing、NavigationTracker、HTTP/直接传输、归档与渲染
关系定位并阅读当前源码；分工复核路由、定位/交通及开源差异。重点检查跨模块的数据
快照、写锁范围、缓存身份、私有状态与资源所有权，而非仅统计函数数量。

本轮code-review-graph增量刷新成功，返回无待更新文件、无错误；这是增量操作结果，
不能解释为全库没有节点或没有影响。Python动态调用和测试覆盖仍需人工阅读与实际
执行交叉确认。前次核对中的图统计和审批失败保留原日期，不充作本轮分析结果。

## 本轮验证

Python 3.13.9 / Windows 11；真实网络检查使用独立本机进程。命令在项目根目录执行，
测试使用`PYTHONPATH=src`，临时目录隔离在仓库`.test-tmp`。

```powershell
python -W error -m unittest discover -s tests -v
python -W error examples/adaptive_demo.py --output benchmarks/features-2026-11-04.json
python -W error examples/network_demo.py
python -W error tools/benchmark.py --output benchmarks/review-2026-11-04.json
```

- **113项测试全部通过，19.507秒，无跳过**；所有警告作为错误处理。
- [动态导航原始结果](../benchmarks/features-2026-11-04.json)：CLI预生成2个瓦片、
  4个对象共4517字节；9个邻居URL；封路后ab/bc自动变为ad/de/ec，经SSE返回
  revision 2；3步指引、polyline、PNG及删除会话均成功。
- 独立主服务/副本/网关共3个进程、2份目录数据库：封路后正确重规划；停主后仍读到
  公开revision 2。自建进程已停止，诊断文件保留在忽略的`.runtime/lab-<uuid>/`。
- [HTTP原始测量](../benchmarks/review-2026-11-04.json)：900节点/3480有向边，
  4客户端60请求，46.66混合QPS，0错误；各类型仅20次，未区分冷/热缓存。

完整分项、命令与测量边界见[验证报告](../benchmarks/results.md)。本轮未改源码及测试，
没有性能提升的对照实验；该小型合成数据结果不能外推到真实路网或章节中的全球流量。

静态及文档验证：src的Ruff E9/F通过，31个Python文件通过3.10语法解析；16份Markdown
中的534个本地链接有效，基准JSON可解析。27份学习指南生成校验通过，本章3张Mermaid
图已实际渲染，限定本章范围的`git diff --check`通过；源码和测试目录无本轮差异。

## 阅读顺序与后续边界

从[学习指南](learning-guide_cn.md)的A→B→C案例进入：先读架构图与一次寻路，
再读交通快照、候选预算和导航CAS时序图，最后看缓存、归档和HTTP生命周期。
[设计](design_cn.md)、[需求](requirements_cn.md)、[任务](tasks_cn.md)同步记录验收范围。

当前采用单机SQLite单写者、条目数缓存与线程HTTP；区域筛选是刷新范围优化，
三层分类视图是数据布局参考。真实分层加速、字节/RSS预算、授权道路/转弯/公交、
HMM及预测模型、流式分片、写主HA与云CDN均需要独立实现和规模验证。
