# 第3章：容量估算

使用Decimal实现QPS/峰值、逻辑与物理存储、带宽、缓存、节点数、并发量及可用性停机预算。输出保留完整假设、公式对应字段和单位。**估算不是实测容量，也不保证SLA。**

## 运行

Python 3.10及以上，仅标准库。在本目录执行：

```powershell
$env:PYTHONPATH = 'src'
python -m unittest discover -s tests -v
python -m capacity_estimation.cli estimate --scenario examples/social-media.json
python -m capacity_estimation.cli estimate --scenario examples/read-heavy.json --sweep peak_factor=2,5,10
python -m capacity_estimation.cli units "1 MiB"
python -m capacity_estimation.cli availability 99.99
```

9项测试覆盖公式、边界、单位、节点向上取整、敏感性分析及真实CLI调用。也可通过 `pip install -e .` 安装 `capacity-estimate` 命令。

## 单位与输入

- MB等十进制单位使用1000的幂，MiB等二进制单位使用1024的幂。`1 MB=1000000 B`，`1 MiB=1048576 B`。
- 大小必须写明单位，支持B及KB至PB、KiB至PiB；大小写严格，字节解析拒绝小写b。带宽结果统一是bits/s，字节乘8。
- `0.5 KiB` 可解析为512字节，`0.1 B` 因不是整字节被拒绝。
- 场景JSON建议使用十进制字符串；CLI也将JSON小数直接解析为字符串，不先经过二进制浮点。Python API拒绝float和bool。
- Decimal按80位有效数字计算，循环小数会在此精度舍入。结果以十进制字符串输出，节点数为整数。
- 拒绝未知字段、非有限数、负需求、越界比例、零利用率/节点QPS、非整数副本数及极端指数。

| 参数组 | 输入字段 |
|---|---|
| 需求 | monthly_active_users、daily_active_ratio、writes_per_dau、reads_per_dau、peak_factor |
| 数据 | metadata_size、media_fraction、media_size、read_response_size |
| 存储 | retention_days、replicas、compression_ratio、index_overhead_ratio |
| 缓存 | cache_fraction、cache_item_size |
| 计算 | server_qps、utilization、safety_factor、spare_nodes、mean_latency_ms |
| 可用性 | availability_percent |

输出 `assumptions` 包含所有默认值和覆盖值，便于检查隐含假设。比例限制0–1，压缩率/利用率必须大于0，峰值/安全系数至少1；副本数为正整数，MAU、保留天数及备用节点为非负整数。

## 示例结果

原书假设示例 `social-media.json`：3亿MAU、日活比例50%、每人每天2次写、10%媒体、每份1MB、保留1825天。可得每日3亿次写，平均写QPS约3472.22、峰值6944.44，每日媒体30TB、五年逻辑媒体54.75PB。

该例故意将元数据设为0、副本为1，复现媒体部分估算；不是完整生产存储，也不是任何社交平台的真实现状。另一示例 `read-heavy.json` 加入读流量、三副本、压缩、索引、缓存与计算冗余，可做峰值倍数等敏感性分析。

## 模型边界

存储先计算逻辑保留量，再压缩、乘副本数及索引倍率。缓存按“保留记录数×热点比例×缓存项大小”，不隐含缓存复制。网络使用未压缩应用载荷，不含TLS、协议、复制、备份、缓存流量或重传。

节点数按混合峰值QPS、安全系数、实测单节点QPS与利用率向上取整，再加显式备用节点。调用者必须给出有依据的单节点指标；模型不证明线性扩容或故障域可用性。并发量使用Little定律的平均延迟，不用p99替代均值。

停机预算采用30天月、365天年，不计SLA排除条款。99.99%对应每天8.64秒、每年3153.6秒；这些是算术预算，不是已达到的可用性。原书旧延迟表仅作历史说明，工具不将其作为当前硬件常量，`mean_latency_ms` 应由调用者测量后提供。

详见[设计](docs/design.md)与[验证报告](benchmarks/REPORT.md)。

## Numerical isolation and module responsibilities

`quantities.py` owns explicit units and decimal validation; `scenario.py` validates scenario fields; `model.py` calculates estimates. Existing public imports remain supported. Each calculation uses an independent 80-digit decimal context with half-even rounding, unaffected by caller/thread rounding, precision or traps. Sensitivity sweeps require a scenario dict and a list/tuple of values. The deep-review suite passes 14 tests, including concurrent callers with different numeric settings. See [review progress](../DEEP_REVIEW_PROGRESS.md).
