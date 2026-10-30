# 搜索自动补全系统

<!-- LEARNING_GUIDE_START -->
## 从这里开始读代码

[跟着一个案例读懂系统：架构、状态变化与关键代码详解](docs/learning-guide_cn.md)。
<!-- LEARNING_GUIDE_END -->

对应根目录书稿第 14 章。实现搜索日志、批量聚合、原子快照、前缀树 Top 5 缓存、
即时过滤、均衡范围分片与实际 HTTP 副本协调。运行时仅依赖 Python 标准库。
本轮补齐稳定事件采样、持久聚合中间结果及可停止的周期构建 Worker。

```bash
pip install -e ".[dev]"
pytest -q
python examples/basic.py
python examples/network_demo.py
```

网络示例启动三个独立分片进程和一个协调服务，终止一个副本后确认排行仍正确，
再验证即时过滤。搜索只匹配开头，按频率降序、文本字典序打破平局，最多返回五条。
支持英文与空格，统一大小写和重复空白，前缀末尾空格保留；最长 50 个字符。

```bash
python -m search_autocomplete.cli --db demo.sqlite3 record event-1 twitch --at 100
python -m search_autocomplete.cli --db demo.sqlite3 build --start 0 --end 200 --shards 2
python -m search_autocomplete.cli --db demo.sqlite3 query tw
python -m search_autocomplete.cli --db demo.sqlite3 block twitch
python -m search_autocomplete.cli --db demo.sqlite3 aggregate --start 0 --end 200
python -m search_autocomplete.cli --db demo.sqlite3 aggregation 1
python -m search_autocomplete.cli --db demo.sqlite3 worker --once --shards 2
# 立即构建，之后每七天构建一次；Ctrl-C 停止。
python -m search_autocomplete.cli --db demo.sqlite3 worker --shards 2
```

显式时间为 UTC 秒。相同事件 ID 重试不会重复计数，冲突内容会拒绝；没有传入时间
的重试保留第一次事件时间。查询建议不会增加统计。默认构建过去七天的数据，
Worker 默认立即构建、每七天重复一次，SQLite 暂时失败或发布冲突五秒后重试；
构建失败不会替换旧快照。周期状态在当前进程中，聚合结果和快照持久化；如需按
自然周聚合，请为 aggregate/build 传入明确的 UTC 周起止时间。

`record ... --sample-every 10` 或 `Collector(store, sample_every=10)` 按事件 ID
稳定采样约十分之一，跨进程和重试选择相同；同一日志流应保持采样比例一致。
频次保存采样计数，不自动推算总量；`Store.record` 仍是无采样的底层接受接口。

屏蔽后可单独运行 `purge-blocked`，或让 `worker --purge-blocked` 在每次构建前清理。
该维护操作删除被屏蔽词的源事件与过期聚合，保留查询摘要去重凭据，防止重试重新
写入；解除屏蔽无法恢复已删除源数据。历史服务快照保持不可变，需另外制定保留
策略；解除屏蔽后，显式查询仍保留该词的历史快照可能再次看到该词。

HTTP 分片节点及协调配置见 [英文说明](README.md#http-shards)。协调服务为每次请求
锁定快照/过滤版本，短前缀可跨分片合并；副本失败会尝试备用副本，任意必要分片
不可用则返回 503。即时屏蔽会使旧请求缓存失效；缓存命中也在返回前复核规则修订，
处理中变更可能返回 503，供客户端重试。过滤后的新快照不再含该词；未清理源数据
时，解除屏蔽后可在下一次构建恢复。HTTP 禁用浏览器缓存，以保持屏蔽及时生效，
因此没有采用原书一小时浏览器缓存的可选优化。

**170 项测试通过**，覆盖跨实例去重、构建不阻塞在线读写、发布竞争、采样、周期重试、不可变 Trie、
过滤排序及实际多进程副本故障。结果及原书覆盖表见 [章节核对](docs/chapter14-review.md)。
延迟工具与 [实测报告](benchmarks/results.md) 已提供，本轮顺序测量本地 P99
0.0596ms、HTTP P99 26.17ms；不代表并发吞吐或生产延迟承诺。

这是单主机 SQLite 持久化与多进程 HTTP 参考实现，不是跨主机存储高可用。副本
须使用相同数据库的快照谱系。过滤补位按排名上界剪枝，大量屏蔽时仍可能遍历整个
匹配子树。冷缓存加载和 Trie 构建不持有全局 LRU 锁；协调器的未完成分片任务不
超过 concurrency，总 request_timeout 包含排队和副本切换。SQLite/Store 元数据
等待和系统 DNS 解析可能超过期限；网络任务持续占用有限槽位，直到退出。历史日志、快照、聚合及去重凭据需配置
保留策略。本机顺序测试不能证明千万日活或 48,000 峰值 QPS。实时热点、多语言、地域
排序、生产认证/TLS 和分布式日志/对象存储为扩展。详细架构见 [design.md](docs/design.md)。
