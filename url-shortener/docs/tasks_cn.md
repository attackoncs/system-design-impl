# 实施任务：第9章 URL 短链服务

状态依据2026-10-31的源码核对和2026-11-01的收尾验证；[需求](requirements_cn.md)、[设计](design_cn.md)、[开源对照](open-source-review_cn.md)。原清单中的已完成条目改为以源码/测试为依据的验收清单，移除过期重复实现说明。

## 已实现并验收

- [x] Python >=3.9 src布局，零运行时依赖；公开核心模型、策略、存储及异常，SQLiteStorage可从包导入。
- [x] 不可变 URLMapping/ClickRecord，301/302枚举及清晰异常。
- [x] 内存双向索引，RLock覆盖查询、生成和写入；冲突拒绝覆盖。
- [x] CRC32哈希加后缀冲突重试，失败抛 CollisionLimitExceededError。
- [x] Base62字母表、编码/解码、协议式 ID 注入；左补零至少7位，大 ID 不截断。
- [x] 短链化、精确去重、解析与点击统计；无效请求不写库。
- [x] SQLite持久化、code/url唯一索引、事务内重查与原子创建。
- [x] 已有URL只读返回，在其他连接持有写事务时仍可完成。
- [x] SQLite持久计数行；同对象的ID分配和映射保存共用事务，失败回滚，重复URL不消耗ID。
- [x] 多实例/跨进程唯一性、重启恢复、旧版顺序数据初始化、序列耗尽不回绕。
- [x] 有界实例LRU，0禁用；缓存锁外回源，命中也记录点击。
- [x] URL校验统一；HTTP中文域名/路径转换为合法Location，不重复转义，支持IPv6。
- [x] 统一WSGI创建与重定向API；16KiB输入限制、过深JSON400、未知码404、302不缓存。
- [x] 演示服务器复用WSGI，支持可选SQLite文件，移除重复协议与处理逻辑。
- [x] 原有109项测试加本轮36项回归，合计145项通过。
- [x] 4进程1000次创建，得到501条唯一映射；重开后全部可解析且继续发新ID。
- [x] 真实HTTP验证200/302/400/404；[原始结果](../benchmarks/review-2026-11-01.json)与[复跑工具](../tools/verify_concurrency.py)。
- [x] 中英README、设计、需求、任务、学习路线和仓库索引同步。

## 明确留在部署层的工作

- [ ] 生产WSGI、入口限流、负载均衡及容量规划。
- [ ] 数据库复制/分片、跨数据库发号、集群统计与持久分析事件。
- [x] 点击明细有界保留（2026-11-09）：每条链接精确计数；明细按条数（默认1000）和时长（默认30天）限制，写入时均摊O(1)淘汰。

这些不属于本轮小规模运行实现；不以单机测试宣称达到原书每天一亿条新增链接的规模。Bloom filter是可选优化，当前不添加。

## 复验

```bash
pip install -e ".[dev]"
python -W error -m pytest tests -q
python tools/verify_concurrency.py --output benchmarks/local.json
python -m examples.demo_server --port 8000 --database urls.db
```
