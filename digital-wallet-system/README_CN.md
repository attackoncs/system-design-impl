# 第28章：数字钱包

钱包间余额转账参考实现，包含账户分片、同分片原子转账、跨分片TCC、幂等、可重演事件日志、历史余额、快照及内部对账。**初始余额全部是合成数据，不连接银行或真实资金渠道。**

实验使用七个独立进程：固定协调者、worker、两个账户分片、三个协调日志副本。日志通过真实HTTP复制，至少两个副本确认后提交；账户分片各自使用SQLite，尚未复制。此处实现固定写入者与人工epoch隔离，未实现Raft、自动选主或生产级HA。

## 运行

Python 3.10及以上，仅使用标准库。在本目录PowerShell中执行：

```powershell
$env:PYTHONPATH = 'src'
New-Item -ItemType Directory -Force .runtime/tmp | Out-Null
$env:TEMP = (Resolve-Path .runtime/tmp).Path
$env:TMP = $env:TEMP
python -m unittest discover -s tests -v
python examples/network_demo.py
python tools/benchmark.py --transfers 20 --clients 4
```

14项测试包含6项真实HTTP独立进程测试。演示在付款方已确认、收款方尚未到账时核验在途资金，重启协调者后继续完成，并执行同分片转账。实验只清理自己启动的进程，退出后删除临时数据库。压测会断言最终余额和对账一致。

如需持久化运行，先执行 `python tools/init_config.py` 生成 `.runtime/private-config.json`，随后在七个独立终端分别设置 `PYTHONPATH=src` 并执行：

```powershell
python -m digital_wallet.cli replica --index 0 --config .runtime/private-config.json
python -m digital_wallet.cli replica --index 1 --config .runtime/private-config.json
python -m digital_wallet.cli replica --index 2 --config .runtime/private-config.json
python -m digital_wallet.cli shard --index 0 --config .runtime/private-config.json
python -m digital_wallet.cli shard --index 1 --config .runtime/private-config.json
python -m digital_wallet.cli coordinator --config .runtime/private-config.json
python -m digital_wallet.cli worker --config .runtime/private-config.json
```

协调者默认9880端口，分片9881–9882，日志副本9883–9885。账户名称、所有者和初始余额在生成配置的 `accounts` 中。生成器不覆盖已有配置，也不修改文件权限；请保护凭据和数据库。`.runtime/` 已加入Git忽略，配置和监听仅允许回环HTTP。

## 使用接口

商户/用户身份使用配置 `owners.alice` 或 `owners.bob` 的Bearer凭据。管理和内部调用另用独立凭据。只允许账户所有者作为付款方；双方所有者都可查看转账，账户余额/历史仅本人可读。

`POST /v1/wallet/balance_transfer` 请求严格包含以下字段：

```json
{
  "transaction_id": "transfer_1",
  "from_account": "<配置中的本人账户>",
  "to_account": "<配置中的收款账户>",
  "amount": 500,
  "currency": "USD"
}
```

金额为正整数USD美分，拒绝浮点数、布尔值和十进制字符串；这与原书示例字符串金额的接口表示不同，但同样避免浮点精度丢失。标识符为1–80个ASCII字母、数字、下划线或连字符。

同一转账ID和内容返回原状态，换内容返回409。不同ID代表不同付款指令；超时/503后务必重用原ID。503可能留下已落盘但尚未获多数派确认的指令，恢复后仍可能完成。

查询 `GET /v1/transfers/<transaction_id>`，受理为 `PENDING`，终态为 `SUCCEEDED`、`CANCELED` 或 `REJECTED`。同分片余额不足直接持久拒绝，跨分片余额不足选择取消。通信结果不明时保持待恢复状态，不对外误报资金失败。

`GET /v1/accounts/<account>` 返回可用/冻结金额及分片事件序号；`?at=<sequence>` 按事件前缀重建历史余额。序号1是初始事件，各分片序号不代表共同时间点。读模型会显示TCC中间状态，不提供跨账户全局原子读。

## 转账与资金守恒

- 同分片：一笔数据库事务提交事件和双方余额投影，并发扣款不能透支。
- 跨分片Try：付款方可用转冻结；收款方仅记录准备，不提前生成可花余额。
- 双方准备后：先把COMMIT决定复制到至少两个日志副本，再确认付款方扣减冻结，随后确认收款方增加可用余额，最后记录完成。
- Cancel：只在未决定提交时选择，释放付款方冻结并在双方写取消墓碑。迟到Try无法复活取消记录，Confirm也不能穿过取消。
- 未知结果：查询参与者持久状态后用原ID继续。冻结没有自动过期；连续五次参与者通信失败进入人工恢复队列，不自动退款或判失败。
- 对账公式：**所有钱包可用＋冻结＋付款方已确认但收款方未确认的在途资金＝初始合成总额**。

协调者串行处理API和推进步骤。没有日志多数派时不再推进资金动作；分片在资金事务内检查持久epoch，拒绝旧写入者。参与者提交后响应丢失通过原ID去重恢复；少数日志副本接收后失败则依赖协调者原盘保留的意图继续复制。**不能用空盘或陈旧副本启动另一权威协调者**，本实现没有原盘丢失后的提升恢复协议。

## 运维与故障实验

管理凭据可调用：

| 接口 | 用途 |
|---|---|
| `GET /admin/status` | 待处理数量、失败次数/死信标记、日志计数及epoch |
| `GET /admin/audit` | 多数派检查、事件/投影核验、参与者记录与总额守恒对账 |
| `POST /admin/sync`，请求体 `{}` | 从原盘日志补齐副本 |
| `POST /admin/retry/<id>`，请求体 `{}` | 清除恢复退避/死信，保留原转账身份 |
| `POST /admin/cancel/<id>`，请求体 `{}` | 为尚未提交的跨分片转账选择取消 |

分片支持 `POST /admin/snapshot`，请求体 `{}`，原子写入带校验和JSON快照。启动时校验完整事件链及快照前缀，再应用尾部事件重建读模型；不会重新执行转账命令，也不会再次扣款。当前优先完整性校验，没有通过快照跳过历史扫描或裁剪日志。

人工epoch恢复须停止worker/协调者，对**全部三个日志副本和两个分片**调用 `POST /admin/fence`，请求体包含更高 `epoch` 和指定 `leader`；更新私有配置后，用协调者原数据库重启，再启动worker。所有参与者完成隔离后方可恢复流量。旧epoch和同epoch不同leader被持久拒绝；维护途中可能阻塞，不等同自动故障切换。

故障注入：分片 `POST /admin/control` 接受 `{"lose_reply":false,"reject_prepare":false}`，可模拟提交后丢响应或明确拒绝Try。日志副本同路径接受 `{"reject_append":true}`，保持状态查询可用但拒绝追加，以验证少数接受时的恢复。重试前请复位标志。

## 验证边界

详见[设计文档](docs/design.md)、[测试/压测报告](benchmarks/REPORT.md)与[原始样本](benchmarks/latest.json)。百万TPS、99.99%可用性均未证明。账户分片不复制，固定协调者原盘仍是恢复前提；未验证跨主机耐久性、真实资金、存取款、FX、手续费、拒付、自动选主及生产身份/密钥管理。

参考实现限制100个预置账户、1000笔受理转账，保留完整日志，协调者读状态重放历史，对账读取完整分片状态。更大规模需要分页、索引、流式复制/重放、分片迁移、更完善的存储复制与备份恢复。
