# 第29章：证券交易系统

限价下单/撤单参考实现，包含价格—时间优先撮合、部分成交、现金/持仓冻结、主备独立重放、行情增量/快照/K线和私有成交报告。**账户、持仓、时钟及交易全部为模拟，不向真实交易所发送订单。**

四个独立进程分别运行网关/定序器、主撮合机、备撮合机、行情/报告服务，通过真实本机HTTP通信并使用独立SQLite文件。主备撮合可人工切换，定序器仍是单点，未实现自动选主或完整生产HA。

## 运行

Python 3.10及以上，仅使用标准库。在本目录PowerShell执行：

```powershell
$env:PYTHONPATH = 'src'
New-Item -ItemType Directory -Force .runtime/tmp | Out-Null
$env:TEMP = (Resolve-Path .runtime/tmp).Path
$env:TMP = $env:TEMP
python -m unittest discover -s tests -v
python examples/network_demo.py
python tools/benchmark.py --pairs 20 --clients 4
```

13项测试含4项独立进程HTTP测试。演示验证同价FIFO、部分成交与价差返还，停主后提升已追平的备机，再撤销剩余卖单并核验现金/股份守恒、行情与报告。实验只清理自己启动的进程及临时数据库。

如需持久运行，先执行 `python tools/init_config.py`，在四个终端设置 `PYTHONPATH=src` 后分别执行：

```powershell
python -m stock_exchange.cli gateway --config .runtime/private-config.json
python -m stock_exchange.cli primary --config .runtime/private-config.json
python -m stock_exchange.cli backup --config .runtime/private-config.json
python -m stock_exchange.cli market --config .runtime/private-config.json
```

端口依次为9890–9893。配置生成器不覆盖已有文件，不修改文件权限；请保护凭据。`.runtime/` 已加入Git忽略，配置与监听均限制回环HTTP。

默认Alice、Bob、Carol各有1000000美分现金及AAPL/MSFT各1000股。时钟固定从 **2026-10-21 14:00 UTC** 开始，模拟工作日 **UTC 09:30–16:00** 交易，仅由管理接口推进。这不是NYSE真实时区/交易日历，不处理节假日或夏令时。

## 下单与撤单

使用配置中 `owners.alice` 的Bearer凭据；其他用户、管理和内部进程凭据彼此独立。

`POST /v1/orders` 请求：

```json
{"request_id":"request_bid1","order_id":"bid1","symbol":"AAPL","side":"BUY","price":110,"quantity":8}
```

价格为正整数USD美分，最小价位1美分；数量为正整数股。拒绝浮点、布尔值及额外字段，不支持市价单或卖空。默认每用户/股票/UTC日最多受理100万股新单；已撤数量仍计入这一保守的报单量限额。

返回序号仅表示指令持久受理，须查询 `GET /v1/requests/<request_id>` 获取实际撮合结果：`accepted=true`及订单状态，或 `accepted=false`及拒绝原因。超时/503后使用原请求ID重试；同一用户、ID和内容返回原序号，变更内容返回409。订单ID在本实验中全局唯一。

| 接口 | 用途 |
|---|---|
| `POST /v1/cancels` | `{"request_id":"cancel1","order_id":"bid1"}` |
| `GET /v1/orders/<order_id>` | 本人的订单、已成交量和剩余量 |
| `GET /v1/account` | 活跃撮合机上的可用/冻结现金、持仓及已应用序号 |
| `GET /v1/requests/<request_id>` | 本人的指令结果或PENDING |

撮合选择最优对手价，再按定序器序号先后，成交价为挂单方价格。买单按限价冻结资金，成交后即时返还价差；卖单冻结股份。部分成交保留剩余冻结，撤单释放。遇到本人挂单时取消主动单剩余部分以阻止自成交，此前与他人的合法部分成交仍保留。撤单/成交竞态完全按定序结果处理。

仅支持DAY单：下一UTC日期的首条定序指令会使旧日未成交订单过期并释放冻结；单独推进时钟但没有指令时不主动清扫。非交易时段允许撤单。身份、时段和风控抽象见 `http.py` 与 `matching.py`，没有生产KYC认证。

## 行情和报告

以下接口位于9893端口。公开行情不含用户身份或订单ID：

| 接口 | 内容 |
|---|---|
| `GET /market/snapshot` | 全部L2订单簿、全局序号与链头 |
| `GET /market/deltas?after=<seq>` | 连续价位变更与公开成交，保留8个增量批次 |
| `GET /market/book?symbol=AAPL&depth=10` | L2深度，depth=1为最优买卖报价 |
| `GET /market/candles?symbol=AAPL&resolution=60` | 成交生成的OHLCV，支持1–86400秒窗口 |
| `GET /v1/report`，携带本人凭据 | 本人的已受理订单及买/卖方成交报告 |

增量中的数量是价位**绝对数量**，0表示删除价位。客户端先取快照，再要求序号连续、previous与链头一致。收到410表示错过保留窗口，重新取快照。行情进程自身也使用这一协议，投影与消费游标原子提交；重复批次不会重复生成成交。

行情与报告异步更新，响应含已应用序号，采用HTTP轮询，无FIX、多播或WebSocket推送。K线只返回存在成交的时间桶。被业务拒绝的命令保留在定序日志及请求结果中，私有报告只列出已受理订单/成交。

## 主备、重启及审计

两台撮合进程独立读取相同不可变指令前缀，确定性生成相同订单状态、成交ID和报告ID。每只股票有独立逻辑订单簿，但共享同一串行撮合进程和账户账务；未实现按股票独立进程并行分区。备机落后不阻塞受理或主机返回结果。

管理凭据可调用：

| 服务 / 接口 | 用途 |
|---|---|
| 网关 `GET /admin/status` | 持久epoch、活跃节点、模拟时钟与定序日志尾部 |
| 网关 `POST /admin/promote`，`{"node":"backup"}` | 仅提升已追平完整日志的节点，并提高epoch |
| 网关 `POST /admin/clock`，`{"timestamp":1792591260}` | 向前推进模拟时钟，不可回退 |
| 撮合机 `POST /admin/snapshot`，`{}` | 校验和快照 |
| 撮合机 `GET /admin/audit` | 现金/股份守恒、冻结明细及独立重放对账 |
| 行情 `GET /admin/status` | 应用序号/链头和重同步次数 |

停主后保留定序器，待备机追平再人工提升。若校验时有新指令进入，提升可能409，追平后重试。旧主在epoch改变后不能从活跃发布接口或增量接口提供行情；仍可作为备机计算同一日志，这不代表再次对外成交。定序器重启保留活跃节点与epoch，定序器不可用时撮合机发布接口关闭，避免自行宣称活跃。

快照先fsync再原子替换。启动校验完整指令哈希链，验证快照等于其重放前缀，再应用尾部；不会重新提交订单或发送外部交易。当前优先校验完整性，没有快照加速历史扫描或日志裁剪。内部只读副本检查与活跃发布接口分开。

## 实测与边界

详见[设计](docs/design.md)、[测试/压测报告](benchmarks/REPORT.md)和[原始样本](benchmarks/latest.json)。预置20笔卖单后，4个并发客户端提交20笔买单，本机实测约 **14.43笔成交主动单/秒**，提交至成交 **p99 473.58毫秒**。这是含HTTP和客户端轮询的小样本端到端指标；20个观测的最近秩p99就是最大值，不能代表稳定尾延迟。

未实现定序器复制、自动选主、跨机耐久性、真实资金、真实交易日历、生产KYC/合规、清算结算、卖空、手续费、市价单或FIX。最多100个账户/100只股票、1000条指令，使用完整JSON状态复制与线性订单簿扫描，保留全部历史。原书每日十亿订单、低毫秒尾延迟和99.99%可用性均未证明。
