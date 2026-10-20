# 第 22 章：广告点击事件聚合

标准库 Python 参考实现：真实 HTTP 采集/持久化分区日志 → 4 个独立聚合进程 → 全局查询归并进程，共 6 个进程。支持事件时间分钟窗口、重复去重、迟到修正、广告热点分散、过滤计数与 Top N、检查点恢复、原始数据重放和独立批对账。

要求 Python 3.10+，不依赖额外 Python 包或外部服务。本章使用单节点 SQLite 原始日志传输，不代表复制队列高可用，也不代表原书十亿事件规模已验证。

## 运行

在本目录执行：

```powershell
$env:PYTHONPATH = 'src'
python examples/network_demo.py
python -m unittest discover -s tests -v
python tools/benchmark.py --output benchmarks/results.json
```

实验自动选择本机端口、生成私有凭据、启动并清理自有的 6 个进程，日志保留在忽略提交的 `.runtime/lab-*`。若系统临时目录受限，先创建 `.runtime/tmp`，把 `TEMP` 和 `TMP` 设置到该目录的绝对路径。

详细证据见 [测试与压测报告](benchmarks/results.md)。网络测试包括事务提交前进程直接退出、聚合进程恢复、日志服务重启，以及局部 Top N 无法求出全局答案的反例。

## 手动启动

```powershell
$env:PYTHONPATH = 'src'
python tools/init_config.py
# 以下每条命令分别在独立终端运行，并设置同样的 PYTHONPATH：
python -m ad_click.cli ingress --config .runtime/secrets.json --port 9820
python -m ad_click.cli worker --config .runtime/secrets.json --partition 0 --port 9822
python -m ad_click.cli worker --config .runtime/secrets.json --partition 1 --port 9823
python -m ad_click.cli worker --config .runtime/secrets.json --partition 2 --port 9824
python -m ad_click.cli worker --config .runtime/secrets.json --partition 3 --port 9825
python -m ad_click.cli api --config .runtime/secrets.json --port 9821
```

配置生成器拒绝覆盖现有文件；配置包含独立的写入、读取、内部和管理令牌，以及固定分区数、日志 URL、有序工作进程 URL、迟到容忍秒数（默认 120）与原始事件容量（默认 100 万）。不要公开配置或数据文件。服务默认仅监听 `127.0.0.1`。

## 事件与查询

事件必须有稳定 `event_id`；一个用户的不同点击应使用不同 ID：

```json
{"event_id":"click-001","ad_id":"ad-1","click_timestamp":1700000001,"user_id":"u-1","ip":"127.0.0.1","country":"US"}
```

采集服务 `POST /events` 使用写入 Bearer 令牌，正文 `{"events":[...]}`，每批 1–200 条。同 ID 同规范化内容返回原位置及 `duplicate=true`；同 ID 不同内容返回 409，整批回滚。超过接收时间 30 秒的未来事件拒绝；容量不足返回 429。持久接收不等于已经可查。

支持追加 JSONL 文件，每行一条事件，结尾换行：

```powershell
python -m ad_click.cli ingest-file --config .runtime/secrets.json --source clicks.jsonl --checkpoint .runtime/clicks.cursor.json
```

重复执行只处理新增完整行。远程确认后保存字节偏移与前缀校验和；丢失确认可安全重放，不完整末行等待换行。文件前缀修改或截断会明确失败，轮换需新检查点。检查点文件由一个采集进程独占。

查询服务使用读取 Bearer 令牌：

| 接口 | 含义 |
| --- | --- |
| `POST /query` | JSON 查询，广告计数或精确全局 Top N |
| `GET /v1/ads/popular_ads?window=5&count=100&country=US` | 最近 5 个完整分钟的 Top 100 |
| `GET /v1/ads/ad-1/aggregated_count?window=5&user_id=u-1` | 指定广告计数及精确过滤 |
| `POST /admin/reconcile` | 管理令牌，原始批计算与在线结果对账 |
| `GET /health` | 最小公开角色/就绪信息 |

```json
{"from":1699999980,"to":1700000280,"count":100,"filters":{"country":"US","ip":"127.0.0.1"}}
```

时间为分钟对齐的 epoch 秒，范围 `[from,to)`，最多 1440 分钟。也可只给 `window`，默认最近 1 个完整分钟；`count` 默认 100，最多 1000。`filters` 支持 `user_id`、`ip`、`country` 的 AND 精确过滤。加 `ad_id` 返回指定广告的 `count`，不存在时为 0；相同点击数按广告 ID 排序。全局候选广告超过 10000 时返回 422，需要缩窄查询。

## 正确性与运维

- 事件 ID 去重和原始数据不自动过期。IP 统一规范化，国家码统一大写。不会把一个用户的正常多次点击合并掉。
- 热门广告按事件 ID 哈希分散到多个分区；每个分区返回全部候选计数，再全局按广告求和、排序，不能先截断局部 Top N。
- 每个工作进程在同一事务提交计数、去重和检查点；事务前崩溃全部回滚，事务后恢复从持久位置继续。队列身份或分区不匹配时停止处理。
- Watermark 单调推进，考虑事件时间、记录的接收时间以及空闲工作进程的墙上时钟；需要合理的时钟同步。迟到事件始终修正旧窗口，`closed=true` 不表示结果永不变化。
- 响应带 `lineage`、检查点向量、积压估计、watermark、`closed` 和可迟到修正标记；整体是分区快照的组合，不是跨分区原子快照。必需工作进程缺失、异常或源身份不一致时返回 503，不返回部分排名。
- 日志与工作进程 `/internal/status` 分别提供容量/分区头，以及检查点、桶行数、watermark、迟到修正次数、最近轮询和错误；内部接口需内部或管理令牌。

```powershell
python -m ad_click.cli reconcile --config .runtime/secrets.json
python -m ad_click.cli rebuild --config .runtime/secrets.json --partition 0 --output .runtime/rebuilt-0.sqlite
```

对账用独立批计数计算到每个工作进程固定检查点，比较完整维度哈希；不一致时 CLI 退出码为 1。重建把有限原始日志重放到新数据库，拒绝覆盖文件；不会自动替换在线数据。批对账当前在内存中保存维度，适合本参考实现的有限数据量。

生产边界：单日志节点和工作进程 SQLite 尚无复制/自动故障转移；测试保留本地磁盘，不覆盖机器或磁盘丢失。分区数固定，无在线扩分区；达到容量需显式迁移。维度行可接近事件数量；原始数据和去重键没有自动清理。尚无欺诈识别、财务结算、TLS、多租户权限、自动隐私生命周期或生产分布式批处理。更多语义见 [设计文档](docs/design.md) 和 [英文说明](README.md)。
