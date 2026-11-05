# 第24章开源源码对照与审查记录

日期：2026-11-05。范围为仓库《System Design Interview.md》第24章（11227–11924行）。本页记录实际阅读过的上游源码及其与本项目的对应关系；本次最终测试和运行数据见[验证报告](../benchmarks/results.md)，章节整体范围见[章节核对](chapter24-review.md)。

## GitHub搜索与选材

分别搜索了 `distributed email server SMTP queue`、`stalwart mail server`、`maddy mail server`。通用查询返回了邮件中继项目；下面选择同时包含实际SMTP与投递队列代码的两个服务器，便于对照本章的持久受理、重试和收件人部分失败。

| 项目 | 本次固定版本 | 实际阅读内容 |
| --- | --- | --- |
| [Stalwart](https://github.com/stalwartlabs/stalwart) | `8b32686532d34623e9ce8e1237cf2bed9554af38` | `crates/smtp/src/queue/spool.rs`：队列身份、Blob引用、持久队列提交及调度通知 |
| [maddy](https://github.com/foxcpp/maddy) | `4fac6a28d710ed6b6bd76f2e7f215cf3b566e6f6` | `internal/target/queue/queue.go`：队列启动/停止、逐收件人结果、剩余收件人持久化和指数退避 |

只阅读和分析了列出的源码，未安装、构建或运行上游系统，也未复制其实现到本项目。固定提交链接用于重现本次观察，不代表后续版本的行为。

## 参考一：Stalwart的对象与持久队列分离

在 [`new_message`及队列锁](https://github.com/stalwartlabs/stalwart/blob/8b32686532d34623e9ce8e1237cf2bed9554af38/crates/smtp/src/queue/spool.rs#L109) 中，新消息生成独立的队列ID，队列项通过有期限的锁协调处理。这里的队列ID是内部投递身份，不能据此推断互联网SMTP具有端到端幂等性。

[`queue`的对象保存路径](https://github.com/stalwartlabs/stalwart/blob/8b32686532d34623e9ce8e1237cf2bed9554af38/crates/smtp/src/queue/spool.rs#L466) 先计算Blob哈希、登记临时引用，再写入Blob。后续[队列提交](https://github.com/stalwartlabs/stalwart/blob/8b32686532d34623e9ce8e1237cf2bed9554af38/crates/smtp/src/queue/spool.rs#L674)在一个存储批次中更新永久引用、Blob提交标记和消息记录；成功之后才[通知调度器](https://github.com/stalwartlabs/stalwart/blob/8b32686532d34623e9ce8e1237cf2bed9554af38/crates/smtp/src/queue/spool.rs#L751)。

可借鉴的是持久状态与唤醒信号的职责：调度通知不应替代可恢复的队列记录。此路径并非一个覆盖Blob服务与元数据库的通用分布式事务；本次也没有审计各存储后端、集群锁或全套恢复路径，不能把这段代码当作跨节点零丢失证明。

本项目对应 [`Spool.accept`](../src/distributed_email/spool.py)：先保存不可变原文对象，再事务写入受理记录；SMTP成功应答位于持久受理之后。对象保存后、受理提交前故障可能留下未被引用的对象，当前没有对象GC。稳定的本地 `X-Delivery-ID` 与内容/信封指纹用于处理丢失应答后的重试，而不是依赖调度线程记忆。

## 参考二：maddy的收件人结果与重试

[`tryDelivery`](https://github.com/foxcpp/maddy/blob/4fac6a28d710ed6b6bd76f2e7f215cf3b566e6f6/internal/target/queue/queue.go#L392)逐收件人处理结果：已成功的收件人退出后续尝试；暂时失败的收件人增加计数并保留；永久失败或达到次数上限的收件人进入DSN处理。仍有待重试收件人时，更新剩余名单和最近尝试时间，持久化元数据，再按[指数退避](https://github.com/foxcpp/maddy/blob/4fac6a28d710ed6b6bd76f2e7f215cf3b566e6f6/internal/target/queue/queue.go#L386)重新调度。

[`start`与`EarlyStop`](https://github.com/foxcpp/maddy/blob/4fac6a28d710ed6b6bd76f2e7f215cf3b566e6f6/internal/target/queue/queue.go#L257)分别恢复磁盘队列、关闭调度并等待已有投递；并发投递另有信号量。注意所读实现中元数据更新失败会记录错误，而远端可能已经接受部分邮件，因此不能由“成功收件人从列表移除”推断任意故障下端到端恰好一次。

本项目采用更简单的本地策略：[`Spool.process_once`](../src/distributed_email/spool.py)部分失败后可重新遍历全部收件人，以相同delivery身份调用邮箱主库；邮箱端按用户与delivery去重。它允许重复网络调用，保证的是本地业务入箱去重。`claim/settle`另用token与有效租约隔离处理尝试，避免旧尝试覆盖新任务。本项目没有公网DSN或按收件人维护独立重试队列，本次不为减少少量本地重复调用而额外引入状态机。

## 章节范围与工程选择

| 原章关注点 | 本地对应与边界 |
| --- | --- |
| HTTP邮箱、发收邮件、附件 | HTTP API、真实本机SMTP、不可变对象服务；只接受配置的 `local.test` 用户，不执行公网MX查找 |
| 用户分区与单主一致性 | 按用户映射到邮箱主库；CAS更新、元数据与索引同事务；只读副本不能直接提升为完整发信主库 |
| 只读/未读、文件夹、搜索 | 固定文件夹、读状态筛选、字段倒排索引及有界元数据缓存；不是Elasticsearch或LSM搜索服务 |
| 新邮件通知 | 持久变更日志和有界长轮询；不需要为本地示例另加WebSocket服务 |
| 投递失败与恢复 | 持久队列、稳定身份、退避与租约隔离；本地SMTP接受不等于每个收件人已入箱 |
| 反垃圾、反病毒 | 默认未扫描邮件隔离；测试字面标记规则只用于验证流程，不能作为真实防病毒结果 |
| 公网投递、跨机房可用性 | SPF/DKIM/DMARC、信誉预热、投诉/退信处理、复制对象与spool、自动选主和跨机房灾备仍未实现 |

原章的十亿用户估算、PB级存储与多机房可用性属于系统目标，不能由本地九进程实验验证。会话线程是原章的加分项，Cc/Bcc、完整文件夹管理与隐私生命周期也未在当前实现中补齐；这些边界应与已验证功能分开阅读。

## 本次本地改动与验收

CodeGraph核对后，本次仅调整HTTP通知准入与请求生命周期两个生产模块。
通知正等待配额为每实例 `floor(3 * concurrency / 4)`，默认24/32，
超额立即503，为收信及普通CAS写入保留入口容量；并发1时可用零等待查询。
这没有引入新队列框架或改变邮件幂等、元数据/索引事务及复制语义。

新增3项回归，修复前主库、API及单工作线程三个子场景失败；
修复后 **99项完整测试通过，31.374秒**，启用 `-W error`、无跳过。
两个真实九进程示例及40封本机邮件/40次搜索验证通过，
见[完整实测及限制](../benchmarks/results.md)。更新中英文文档及导读3张图，
纠正旧“无读屏障”和“两秒网络发送租约”表述。SMTP仍只有5秒空闲超时，
活跃会话没有总期限；本次未把此边界扩大为新的SMTP功能。
