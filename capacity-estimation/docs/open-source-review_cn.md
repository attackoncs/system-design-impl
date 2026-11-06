# 第03章：开源测量代码与本地估算模型对照

日期：2026-11-06。依据根目录《System Design Interview.md》524–697行。原书讨论2的幂、历史延迟、可用性和假想社交媒体QPS/存储估算，本地实现提供显式假设和单位的计算器；没有新增网络服务需求。

## GitHub检索与固定源码

通过GitHub连接器查询`repo:sirupsen/napkin-math`，核实主仓库及默认分支master，读取固定提交[`13ae05a78f60b5ef7d70b220a2545a0cac7c380c`](https://github.com/sirupsen/napkin-math/tree/13ae05a78f60b5ef7d70b220a2545a0cac7c380c)的README和以下实际Rust文件。仅阅读，没有安装、复制、编译或运行上游，也没有将云主机、磁盘或网络参数写成本地默认值。

| 实读源码 | 机制与可借鉴之处 | 不能直接等同的能力 |
|---|---|---|
| [README 22–140行](https://github.com/sirupsen/napkin-math/blob/13ae05a78f60b5ef7d70b220a2545a0cac7c380c/README.md#L22) | 性能表标注操作大小、并行条件、测试环境及延迟/吞吐口径；区分便于估记的数与测量 | 这是上游公布的条件，不是本机重测结果，也不是通用常数 |
| [network.rs 59–121行](https://github.com/sirupsen/napkin-math/blob/13ae05a78f60b5ef7d70b220a2545a0cac7c380c/benches/network.rs#L59) | 往返先预热，逐请求计时；吞吐用服务端接收字节合计，分别除`1e9`和`2^30`输出Gbit/s与GiB/s | 链路往返、平均延迟和多连接吞吐是不同量，不能把任一个直接当单节点混合业务QPS |
| [storage_durable_write.rs 52–68行](https://github.com/sirupsen/napkin-math/blob/13ae05a78f60b5ef7d70b220a2545a0cac7c380c/benches/storage_durable_write.rs#L52) | 工具参数k/m使用二进制倍数，输出KiB/MiB；单位必须按实现解释 | 本地显式区分MB/MiB，不能抄单位名字再沿用不同底数 |
| [同步writer 114–141行](https://github.com/sirupsen/napkin-math/blob/13ae05a78f60b5ef7d70b220a2545a0cac7c380c/benches/storage_durable_write.rs#L114) | 每次先写指定偏移，再`sync_data`，分别累计写与同步耗时 | 持久写实测不同于只写缓存或空间预算；本地算术没有执行磁盘基准 |
| [并发持久写146–200行](https://github.com/sirupsen/napkin-math/blob/13ae05a78f60b5ef7d70b220a2545a0cac7c380c/benches/storage_durable_write.rs#L146) | io_uring将write和fdatasync关联，检查完成结果；在途数量是显式实验条件 | 并发深度的吞吐变化不证明任意业务线性扩展；本地备用节点数不证明故障域可用性 |

napkin-math是估算依据与测量工具集合，不是与本地同接口的预算计算器。上游用于核对单位、平均值/分位数、并发条件和持久化口径；本轮节点取整修复来自本地反例，没有移植上游代码。

## 本地检查点

- [quantities.py](../src/capacity_estimation/quantities.py)统一显式单位与有限Decimal输入；不将大小写不同的比特/字节单位混算。
- [scenario.py](../src/capacity_estimation/scenario.py)回显完整假设，校验比例、整数计数与正能力；每次[estimate](../src/capacity_estimation/model.py)独立计算，线程不会共享可变数值上下文。
- 展示值允许80位有效数字舍入，服务节点数从原始校验值构造精确有理数并上取整，总节点直接整数相加；验证见[取整回归](../tests/test_precision.py)。
- 原书的约3500/7000写QPS与约55PB媒体，是特定假设下的量级；本地六次实际CLI输出见[原始证据](../benchmarks/review-examples-2026-11-06.json)，不称作性能测量。

章节覆盖、图工具与测试结果见[本轮审查](chapter03-review.md)，完整命令见[验证报告](../benchmarks/REPORT.md)。没有实测硬件容量、供应商SLA、故障域、价格或生产增长分布，也没有为纯计算器添加后台并发系统。
