# 搜索自动补全系统

对应根目录书稿第 14 章。实现搜索日志、批量聚合、原子快照、前缀树 Top 5 缓存、
即时过滤、均衡范围分片与实际 HTTP 副本协调。运行时仅依赖 Python 标准库。

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
```

显式时间为 UTC 秒。相同事件 ID 重试不会重复计数，冲突内容会拒绝；没有传入时间
的重试保留第一次事件时间。查询建议不会增加统计。默认构建过去七天的数据，
部署方按周调用构建命令；构建失败不会替换旧快照。历史日志与快照需另设保留策略。

HTTP 分片节点及协调配置见 [英文说明](README.md#http-shards)。协调服务为每次请求
锁定快照/过滤版本，短前缀可跨分片合并；副本失败会尝试备用副本，任意必要分片
不可用则返回 503，不返回错误的部分排行。即时屏蔽会使旧请求缓存失效；已在处理
的请求按其捕获版本完成。过滤后的新快照不再含该词，解除屏蔽后需再次构建才能
恢复已经物理排除的词。HTTP 禁用浏览器缓存，以保持屏蔽及时生效。

**30 项测试通过**，覆盖排名、短语、并发事件去重、时间窗口、失败构建回滚、重启、
过滤及实际多进程副本故障。延迟工具与 [实测报告](benchmarks/results.md) 已提供。

这是单主机 SQLite 持久化与多进程 HTTP 参考实现，不是跨主机存储高可用。副本
须使用相同数据库的快照谱系；过滤补足结果可能扫描子树，后续构建恢复常规快速
路径。本机顺序测试不能证明千万日活或 48,000 峰值 QPS。实时热点、多语言、地域
排序、生产认证/TLS 和分布式日志/对象存储为扩展。详细架构见 [design.md](docs/design.md)。
