# 第10章实施与验收清单

状态按2026-11-05代码与运行结果更新。见[需求](requirements_cn.md)、[设计](design_cn.md)、[原书/开源对照](open-source-review_cn.md)。保留2026-11-01历史审查记录。

## 已完成

- [x] Python>=3.9、src布局、零运行时依赖、模型/配置/公共API与自定义异常。
- [x] HTML抓取、超时、大小上限、截断检测、有界下载槽与DNS TTL/LRU缓存。
- [x] 高/中/低优先级、同优先级FIFO、同主机保留和完成后礼貌间隔。
- [x] 每主机堆、可用队首与延迟唤醒；活跃主机积压不在每轮重扫。
- [x] 随机操作与全扫描oracle对照；相同负载基线/当前各3次调度测量。
- [x] 在创建协程前限制任务数量；空闲槽每50毫秒复查到期主机，不被慢请求拖住。
- [x] origin级robots缓存、并发miss合并、Crawl-delay、通配符/末尾锚点及最长匹配。
- [x] robots百分号转义与字面操作符区分；空路径查询按根路径匹配。
- [x] 首个base href、坏href隔离、主机/端口过滤；规范化保留userinfo大小写。
- [x] 地址去重与内容指纹分离；认领失败可重试，重复内容仍发现相对链接。
- [x] 单owner持久队列、恢复inflight、原子归档/确认、取消归还与持久重试。
- [x] 外部批次触发自适应重爬；主机分片及持久路由移交接口。
- [x] 同步/异步内容Hook与可替换Fetcher；外部副作用幂等边界说明。
- [x] 修复测试SQLite连接关闭；显式配置async fixture循环作用域，以-W error验收。
- [x] 2026-11-01原有213项加43项回归为256项；2026-11-05新增3项连续取消场景为259项，示例及真实HTTP/恢复检查通过。
- [x] 连续取消仍排空已接受SQLite操作和自有子任务；open/finish/close取消后释放owner并复位running。
- [x] 中英README、需求、设计、任务、学习路线与[原始测量](../benchmarks/REPORT.md)同步。

## 独立部署工作

- [ ] 集群成员发现、消息代理、租约与带版本的安全移交控制面。
- [ ] 数据库复制/分片、五年保留清理、生产容量规划与地域调度。
- [ ] 动态渲染、反垃圾模型、PageRank/概率优先级策略。
- [ ] 如需完整RFC robots语义，补充代理分组合并及HTTP失败状态策略；当前明确保留兼容行为。

这些是当前参考实现的边界，不能据此声称已经达到原书每月十亿页的规模。

## 复验

```bash
pip install -e ".[dev]"
python -W error -m pytest tests -q
python examples/basic_crawl.py
python tools/benchmark.py --output benchmarks/local.json
python -W error tools/verify_http.py --output benchmarks/http-local.json
```
