# 第27章：支付系统

<!-- LEARNING_GUIDE_START -->
## 从这里开始读代码

[跟着一个案例读懂系统：架构、状态变化与关键代码详解](docs/learning-guide_cn.md)。
<!-- LEARNING_GUIDE_END -->

单币种电商支付参考实现，涵盖收款、向商户出款、部分退款、不可变复式账本、钱包、未知结果恢复和异步对账。API、工作进程、模拟PSP和内部事件消费者分别运行在四个独立进程，通过真实本机HTTP通信。**所有资金操作均为模拟，未接入真实支付渠道。**

## 运行

Python 3.10及以上，仅使用标准库。在本目录的PowerShell中执行：

```powershell
$env:PYTHONPATH = 'src'
New-Item -ItemType Directory -Force .runtime/tmp | Out-Null
$env:TEMP = (Resolve-Path .runtime/tmp).Path
$env:TMP = $env:TEMP
python -m unittest discover -s tests -v
python examples/network_demo.py
python tools/benchmark.py --payments 20 --clients 4
```

48项测试包括5项独立进程HTTP测试。演示自动启动并清理自己的四个进程，完成收款10000美分、出款3000美分、退款2000美分，核验最终可用余额5000美分、5笔账本事务及供应商对账。演示退出后删除临时数据库；压测结果保存在 `benchmarks/latest.json`。

如需保留数据库并在浏览器体验模拟托管收银台：

```powershell
python tools/init_config.py
```

随后在四个终端中分别运行，均以本目录为工作目录并设置 `PYTHONPATH=src`：

```powershell
python -m payment_system.cli api --config .runtime/private-config.json --port 9870
python -m payment_system.cli provider --config .runtime/private-config.json --port 9871
python -m payment_system.cli receiver --config .runtime/private-config.json --port 9872
python -m payment_system.cli worker --config .runtime/private-config.json
```

配置生成器不会覆盖已有文件。凭据、数据库均位于Git忽略的 `.runtime/`，生成器不修改文件权限，请妥善保护此目录。服务仅允许绑定回环地址，配置也只接受本机模拟端点。

## 支付流程

商户API使用配置中 `merchants.seller` 的Bearer凭据。管理、工作进程、PSP及事件消费者使用彼此不同的凭据。商户无法读取其他商户的支付或使用管理API。

1. `POST /v1/payments`，提交 `{"request_key":"pay1","order_id":"order1","amount":10000,"currency":"USD"}`。
2. 查询 `GET /v1/operations/<id>`，等待 `checkout` URL；创建接口返回仅表示持久受理。
3. 打开模拟PSP托管页面，选择批准或拒绝。自动演示通过 `POST <checkout URL>` 的 `{"choice":"test-success"}` 完成操作。
4. PSP签名回调或工作进程查询确认 `SUCCEEDED` 后，钱包入账。待支付状态不会产生可用资金。
5. `POST /v1/payouts`，示例 `{"request_key":"out1","amount":3000,"currency":"USD","destination":"test-bank-ok"}`。
6. `POST /v1/refunds`，示例 `{"request_key":"refund1","amount":2000,"currency":"USD","payment_id":"<原收款ID>"}`。

金额统一使用正整数USD美分，拒绝浮点数、布尔值、其他货币、卡号和额外字段。幂等键在同一商户所有操作中唯一；相同键不同请求返回409。同一订单仅允许一次收款，不能换键重复支付。退款累计包含待处理、未知结果，不能超过原收款，且必须有足够可用余额。`test-bank-decline` 可模拟出款拒绝。收银台有效期十分钟，过期后的查询或支付提交会确认失败。

`GET /v1/wallet` 查看可用/冻结余额；`GET /v1/ledger` 查看已入账借贷明细，超过100笔事务明确返回422，避免截断后被误用为完整账本。

## 账务与恢复

| 业务阶段 | 借方 | 贷方 |
|---|---|---|
| 收款成功 | PSP清算资产 | 商户可用资金负债 |
| 受理出款/退款 | 商户可用资金负债 | 商户冻结资金负债 |
| 出款/退款成功 | 商户冻结资金负债 | PSP清算资产 |
| 出款/退款明确失败 | 商户冻结资金负债 | 商户可用资金负债 |

操作、钱包、平衡分录及outbox在同一SQLite事务提交。入账触发器要求借贷相等，已入账分录禁止修改或删除。供应商交易ID关联最终分录。

工作进程先查询PSP，只在明确404时用原操作ID创建。超时保留 `UNKNOWN`，出款/退款冻结金额不会自动释放。五次连续通信失败进入死信并生成人工复核；正常等待用户支付的轮询不消耗失败次数。租约过期后使用新令牌认领，旧工作进程不能确认新认领；供应商及本地账本的唯一ID约束防止重叠执行重复生效。

回调校验原始请求字节的HMAC-SHA256和五分钟时间窗口，持久化去重并检查供应商版本，拒绝伪造签名，同事件ID不同内容返回409。较旧回调被忽略，金额/身份不符或终态冲突进入复核，不更改资金。内部事件采用至少一次投递，消费者持久化去重，不宣称网络恰好一次。

管理凭据可使用：

| 接口 | 用途 |
|---|---|
| `GET /admin/status` | 账本审计、待处理/死信数量及前100条待复核记录 |
| `POST /admin/reconcile`，请求体 `{}` | 异步对账，返回任务ID |
| `GET /admin/reconciliations/<run>` | 查询pending/done/failed及差异 |
| `POST /admin/retry/<operation>`，请求体 `{}` | 用原PSP身份重试未决操作 |
| `POST /admin/retry-job`，请求体 `{"job_id":"notify:<operation>"}` | 用原事件身份重投死信任务 |

先检查供应商状态和复核记录再人工重试。对账比较本地支付、分录、钱包和PSP完整快照；差异只进入复核，绝不自动补扣/改余额。供应商状态领先也可能来自在途操作，需要稍后复核。参考实现没有复核关闭界面。

故障注入：PSP的 `POST /admin/control` 必须包含 `lose_response`、`webhooks_down`、`query_down` 三个布尔值和整数 `statement_delta`。例如 `{"lose_response":true,"webhooks_down":true,"query_down":false,"statement_delta":0}` 可模拟PSP提交后响应丢失。`statement_delta=1` 仅篡改账单样本用于验证差异检测。事件消费者同路径接受 `{"lose_ack":true}`，模拟提交成功但确认丢失。恢复时将标志复位。

## 实测与边界

详见[设计文档](docs/design.md)及[测试/压测报告](benchmarks/REPORT.md)。本机Windows、20笔支付、4个并发客户端的样本为 **6.67笔/秒**，从创建到模拟批准并确认成功的p95为 **697.34毫秒**；尚未达到或证明原书约10 TPS的目标。

权威库是单SQLite WAL，写事务串行；四进程验证了网络和持久化重启，未实现权威库复制、自动故障切换或跨地域可用性。无真实PSP、PCI认证、真实银行结算、多币种、手续费、税费、拒付、跨商户原子外部扣款、商户开户或生产级凭据生命周期。模拟PSP上限10000个操作，供应商对账采用有界完整快照，大历史需要分页/流式对账及更完整的运维设计。

## 第27章工程复核（2026-10-27）

新增可信订单编排 `POST /internal/checkouts`，正文 `{checkout_id,orders}`，每次1..20笔；
订单字段为merchant/order_id/amount/currency，金额仍为USD整数美分。只允许内部令牌，
商户令牌不能为其他商户建单。所有操作ID和任务同一SQL事务持久化，后续订单无效则全部受理回滚；
同checkout ID改变内容返回409。`GET /internal/checkouts/{id}` 查询各笔独立状态。
外部授权/结算逐笔进行，不是跨商户一次原子扣款。新增 `GET /v1/payments/{id}` 为本人pay-in查询别名。

Store门面拆schema/操作/任务/账本/对账/checkout/契约；输入锁前冻结，writer后单次校验采时。
相同账本ID须核对原分录的账户、金额和供应商身份，不静默忽略冲突。供应商revision指纹独立持久化，
即使本地转UNKNOWN，已观察版本也不能改内容释放冻结资金；冲突进入复核。旧版没有记录的历史指纹
无法追溯校验，仍依靠当前状态检查和独立对账。

默认4个执行线程，配置 `worker_concurrency=1..16`。HTTP认领租约30秒，每任务共享20秒I/O预算，
只有明确PSP404才用原操作ID创建，回执必须属于当前任务才可应用。finish同时校验token与到期，
不要求先有新worker重领。未知结果保留冻结和原身份。回调分发独立15秒租约/5秒预算，旧ACK不能
覆盖新尝试；通知核对消费者事件ID。对账先验证完整有界账单、摘要、唯一身份和严格回执，
内部提交须有有效job租约，已完成结果不被重写；差异仅入复核，不自动改资金。
权威与模拟PSP各最多10000操作，生产大历史仍需流式、保留与完整对账运维。

HTTP默认32准入/15秒预算，普通JSON正文1MiB、内部对账8MiB、RPC响应8MiB。
禁代理/重定向，严格JSON/framing/路由/凭据/额外字段；绑定后启动后台，退出取消socket并排空线程。
无法强杀任意SQL/CPU/DNS或自定义provider。最终48项通过21.561秒，无跳过、-Werror；
基线13项16.541秒，其中5项用真实四进程HTTP。新增演示验证两商户独立成功/失败、同ID重试、
出款/退款后可用500/冻结0、另一商户0、5笔分录/4个唯一PSP操作及对账一致；原演示5000美分/5笔分录仍通过。
20笔/4客户端测得6.67笔完成/秒，p50/p95537.585/697.339ms，仍低于原书10TPS示例，
不是持续容量或受控提速证明。见[章节记录](docs/chapter27-review.md)和[实测报告](benchmarks/REPORT.md)。

真实PSP/卡/银行、AML/反欺诈、PCI认证、权威HA、TLS、银行结算、FX/手续费/税/拒付与复核结案UI
仍未实现或验收；所有资金均模拟。未提交或推送。

供应商交易ID在本地操作间全局唯一，复用先入复核、不入账，数据库唯一索引再次保护；
旧库如有重复绑定须先审计再升级。
