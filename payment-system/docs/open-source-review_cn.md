# 第27章开源源码对照与审查记录

日期：2026-11-05。对应仓库《System Design Interview.md》第27章（13482–14170行）。
本页记录实际搜索、阅读的开源实现及本地对应关系；本次验收见
[章节记录](chapter27-review.md)和[验证报告](../benchmarks/REPORT.md)。

## GitHub搜索与固定版本

分别搜索 `hyperswitch payment orchestration`、`tigerbeetle financial transactions`，
并在仓库内检索请求身份、Webhook验签与转账创建代码。前者是支付编排项目，
后者是金融记账数据库，二者承担不同职责，不能互相替代。

| 项目 | 固定提交 | 实际阅读的主要源码 |
| --- | --- | --- |
| [juspay/hyperswitch](https://github.com/juspay/hyperswitch) | `0ddf2657234ad8c4c73bd9d92af84685fe9b3ef4` | `crates/router/src/core/utils.rs` 的请求引用复用；Webhook接口及Stripe连接器的验签实现 |
| [tigerbeetle/tigerbeetle](https://github.com/tigerbeetle/tigerbeetle) | `0450c32b2c3c3ab6114f3402cc601341cc8192aa` | `src/state_machine.zig` 的转账身份校验、金额/余额约束和借贷账户更新 |

只阅读了列出的源码，未安装、构建或执行上游系统，也未复制实现。没有连接真实PSP，
没有真实资金流动。本次选定的函数不是对整个上游的安全、容灾或合规认证。

## 参考一：Hyperswitch的连接器身份与Webhook边界

在v1路径的
[`get_connector_request_reference_id`](https://github.com/juspay/hyperswitch/blob/0ddf2657234ad8c4c73bd9d92af84685fe9b3ef4/crates/router/src/core/utils.rs#L2239)
中，如果payment attempt已经保存`connector_request_reference_id`，就复用它；
没有时才委托连接器生成。payout辅助方法也优先复用已保存引用。
可以借鉴的是：一次业务操作的外部身份需要跨多次连接器调用保持稳定，
不能把每次网络尝试都当成一笔新付款。

这不是“任何连接器都自动幂等”的证明。引用如何持久化、PSP保留期限、查询能力以及
创建请求的实际幂等契约仍需分别审计。所读的
[v2同名函数](https://github.com/juspay/hyperswitch/blob/0ddf2657234ad8c4c73bd9d92af84685fe9b3ef4/crates/router/src/core/utils.rs#L2343)
尚为`todo!()`，所以本页的结论明确限于所读v1辅助路径，不泛化到全部版本或功能。

[`Stripe`的Webhook实现](https://github.com/juspay/hyperswitch/blob/0ddf2657234ad8c4c73bd9d92af84685fe9b3ef4/crates/hyperswitch_connectors/src/connectors/stripe.rs#L2770)
选择HMAC-SHA256，提取`v1`签名，并以时间戳与消息体构造验签材料。
通用[`verify_webhook_source`](https://github.com/juspay/hyperswitch/blob/0ddf2657234ad8c4c73bd9d92af84685fe9b3ef4/crates/hyperswitch_interfaces/src/webhooks.rs#L277)
取得连接器算法、商户配置的secret、签名和消息后调用验证；缺少secret配置会失败。
该接口的默认算法并不是所有连接器的安全实现，实际行为要看具体覆盖方法。

验签只回答消息是否满足配置的验证方法，不自动证明对应业务已经正确记账。
本次没有据此推断上游完整时间窗、事件去重、状态转换或对账保证。
本地[HTTP入口](../src/payment_system/http.py)、[操作状态](../src/payment_system/operations.py)
另行实现时间窗、事件身份、冻结金额/商户校验及修订号规则；
“验签通过”不能绕过这些业务条件。

## 参考二：TigerBeetle的转账身份与借贷成对更新

[`create_transfer`](https://github.com/tigerbeetle/tigerbeetle/blob/0450c32b2c3c3ab6114f3402cc601341cc8192aa/src/state_machine.zig#L3693)
先验证transfer ID，已存在的ID进入专门的重放检查，而不是再增加账户金额。
新转账检查借贷账户存在、账户不同及ledger一致，并在账户约束验证后才进入修改阶段。

[`create_transfer_exists`](https://github.com/tigerbeetle/tigerbeetle/blob/0450c32b2c3c3ab6114f3402cc601341cc8192aa/src/state_machine.zig#L3971)
比较标志、pending身份、超时、借贷账户、金额、用户数据、ledger和code。
普通转账同ID换金额会返回冲突；特殊balancing转账的amount是上限，重放规则有所不同，
不能把该例外直接套成本地固定整数分金额的规则。

在[状态修改段](https://github.com/tigerbeetle/tigerbeetle/blob/0450c32b2c3c3ab6114f3402cc601341cc8192aa/src/state_machine.zig#L3886)，
转账记录和借贷两侧使用同一实际金额：pending增加双方pending计数，
普通已入账转账增加双方posted计数。可借鉴的是先验证不可变身份和约束，再成对更新。
本次没有审计完整共识、存储提交、所有pending完成路径，也未执行上游故障测试，
因此不能从这段函数推导任意宕机、网络分区或外部PSP下的原子性。

本地[`LedgerMixin.post`](../src/payment_system/ledger.py)采用SQLite事务、不可变借贷行和
稳定账务身份；它校验operation、kind、账户、金额及provider身份后才接受重放。
钱包是同事务内的余额投影。TigerBeetle是可对照的记账引擎，不是本地已经集成的依赖，
也不能代替支付执行、PSP状态查询、回调或对账。

## 原章与本地范围

| 原章关注点 | 本地对应与边界 |
| --- | --- |
| Pay-in、pay-out与多订单checkout | 可信内部checkout编排、每订单稳定操作身份；本地接收可以同事务，外部商户订单仍独立执行，不是原子多商户扣款 |
| Hosted payment page | 独立模拟PSP及合成令牌/选择；不接收真实卡号、银行账号或资金 |
| 双重记账、钱包与内部一致性 | SQLite中的不可变平衡账务、可用/冻结余额及事务outbox；不是多数据库分布式事务 |
| 重试与幂等 | 稳定operation身份、先查询再创建、持久任务/租约、原身份人工重试；不声称网络只发送一次 |
| 超时与异步通知 | UNKNOWN保留不确定性，签名回调和查询按身份/修订合流；超时不等于失败或立即释放冻结款 |
| 对账 | 有界provider statement与本地操作、账务、钱包核对；差异留待审查，不自动补账或模拟真实银行结算 |
| 金额与币种 | 单币种USD整数分，拒绝浮点/布尔；未实现汇率、费用、税、拒付和多币种结算 |
| 可靠性与规模 | 单活SQLite及四进程本机实验；权威复制HA、真实风险/AML、PCI认证、持续吞吐与生产监控仍是缺口 |

原章强调正确处理失败与跨系统不一致。本地的幂等效果、借贷平衡和恢复行为，
必须由本地真实执行的测试验证；开源项目名称或单个函数不能替代这些证据。

## 本次本地验收与文档修正

CodeGraph及账本/状态、任务/供应商、HTTP/传输专项核对后，未发现新的确认代码缺陷；
生产代码与测试保持不变。单次完整严格验收 **48项通过，22.021秒**，
启用 `-W error`、无跳过，包含5项实际四进程HTTP测试。两个四进程示例及20笔/4客户端
模拟收款基准通过，账本、钱包与独立对账一致；具体数据与限制见本页开头的报告。

同步纠正文档：失败预算是累计计数，PENDING不增加也不清零；attempts不是failures。
网络工作任务租约30秒、I/O预算20秒，直接辅助默认2秒。可信checkout支持1..20订单
原子本地受理，外部逐笔独立，并非跨商户原子扣款。导读补充UNKNOWN/死信保留冻结、
确认终态才记账的流程；所有资金和PSP仍为模拟，不推断真实支付、HA或合规保证。
