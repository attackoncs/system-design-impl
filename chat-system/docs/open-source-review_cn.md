# 第13章：开源源码对照与实现复核

日期：2026-11-05。依据根目录《System Design Interview.md》3855–4288行，核对单聊、最多100人的群聊、文本消息、在线状态、多设备同步、推送及节点故障恢复。原书五千万日活是架构目标，本地测试不能证明该规模。

## GitHub检索与固定源码

分别检索`tinode chat`、`zulip server`。Tinode搜索首先返回客户端仓库，因此另行核实服务端`tinode/chat`；Zulip搜索确认主仓库`zulip/zulip`。读取各仓库实际提交及以下源码，固定版本以便复查：

| 项目 | 本次固定提交 | 实际选读文件 |
|---|---|---|
| [Tinode Chat](https://github.com/tinode/chat/tree/eb90ef83acae6542f52719377ee5a76d267a5618) | `eb90ef83acae6542f52719377ee5a76d267a5618` | `server/topic.go`、`server/store/store.go` |
| [Zulip](https://github.com/zulip/zulip/tree/7380849ee6db900ba5e2ce84aed13156a471e33d) | `7380849ee6db900ba5e2ce84aed13156a471e33d` | `zerver/actions/message_send.py`、`zerver/tornado/django_api.py` |

这里只阅读和比较固定源码，没有安装、复制或运行上游，也没有运行上游测试。两组项目的协议、存储和部署架构不同，不将其全部能力移植到本项目。

## Tinode：先保存，再确认和广播

`saveAndBroadcastMessage`先检查发送权限、处理可信发送者身份，再调用`store.Messages.Save`。保存失败时返回错误；成功后才递增Topic的`lastID`，以该序号确认客户端请求、构造消息、广播会话并安排推送。[保存、确认与广播](https://github.com/tinode/chat/blob/eb90ef83acae6542f52719377ee5a76d267a5618/server/topic.go#L1106-L1196)

这与本地“持久消息是事实，实时队列是提示”的分层一致，但序号作用域不同：Tinode所读路径用Topic内序号，本地SQLite和Redis在各自存储域分配递增全局消息ID，不能把二者的游标直接互换。

`messagesMapper.Save`依次调用`TopicUpdateOnMessage`和`MessageSave`，发送者已读/已接收位置则通过后续订阅更新维护。该接口层没有展示所有存储适配器的事务保证，因此不能仅凭函数名字宣称这些写入和通知是一个跨资源原子事务。[存储接口的实际调用次序](https://github.com/tinode/chat/blob/eb90ef83acae6542f52719377ee5a76d267a5618/server/store/store.go#L681-L710)

所读请求ID用于关联成功回复，不足以证明等同于本地持久`(sender, client_message_id)`去重。稳定业务幂等键、协议请求ID和已读位置应分别理解。

## Zulip：消息、用户收件记录与提交后通知

`do_send_messages`由`transaction.atomic(savepoint=False)`包围，先批量写Message，再按可见用户构造收件记录并调用`bulk_insert_ums`，随后安排客户端和其他组件事件。这提供了核对“消息和可见收件关系一起持久化”的实际参考。[数据库事务和收件记录](https://github.com/zulip/zulip/blob/7380849ee6db900ba5e2ce84aed13156a471e33d/zerver/actions/message_send.py#L1107-L1215)

发送事件的调用使用`send_event_on_commit`；其实现注册Django的`transaction.on_commit`回调，在事务成功提交后才实际发送事件。[调用位置](https://github.com/zulip/zulip/blob/7380849ee6db900ba5e2ce84aed13156a471e33d/zerver/actions/message_send.py#L1451-L1464)、[提交后回调](https://github.com/zulip/zulip/blob/7380849ee6db900ba5e2ce84aed13156a471e33d/zerver/tornado/django_api.py#L275-L292)

提交后回调避免在数据库回滚时提前向客户端公布消息；它本身不是数据库与外部消息系统的两阶段提交，也不能仅凭此函数证明通知恰好一次。Zulip完整收件、流/话题权限与通知策略远超本章范围，本地不复用其ORM或部署栈。

## 本地核对与取舍

| 检查点 | 本地实现 | 边界 |
|---|---|---|
| 消息和收件关系 | [SQLiteMessageStore.append](../src/chat_system/store.py)同事务写消息、发送者去重键、成员快照inbox；[Redis Lua](../src/chat_system/redis_scripts.py)同时写消息/收件箱/事件/推送候选 | SQLite文件模式才跨进程重启持久化；默认内存库不持久 |
| 确认与实时发送 | [ChatService.send](../src/chat_system/service.py)提交及本地入队共用顺序锁；IO移出事件循环，推送在锁外 | 实时提示可能丢失/重放，不推进设备持久游标 |
| 重试与冲突 | 同发送者/同键/同频道/同正文返回原消息，改内容拒绝；Redis原子脚本在写入时核验会话/节点实例 | 不是提供方外部副作用的恰好一次承诺 |
| 多设备恢复 | [ReconnectingChatClient](../src/chat_system/client.py)只确认最近完整有序同步页，fsync后原子替换设备专属游标文件 | 应用处理须按消息ID幂等；游标文件不和应用业务结果原子提交 |
| 群成员隐私 | 按发送时成员写inbox；历史及单条消息读取要求原始收件人资格 | 新成员不回填加入前历史；未实现用户资料或联系人服务 |
| 跨节点与在线状态 | [DistributedChatService](../src/chat_system/distributed.py)消费持久事件，Redis会话/节点租约隔离旧实例；设备聚合心跳 | 同一Redis哈希槽；状态按轮询传播，不是多地域分片 |
| 推送恢复 | [PushWorker](../src/chat_system/push.py)持久候选、锁续租、凭据隔离ACK/重试/死信 | 外部至少一次，供应商须用稳定幂等键去重；不发送真实移动推送 |

本轮关键业务路径未发现新的已确认缺陷，保留原有实现。修复的是Windows验证工具将WSL里的Linux符号链接当成本机普通文件检查的问题：存在性检查不跟随Linux链接，最终仍由真实WSL Redis启动验证。两参数回归先复现旧失败，再验证成功/缺失路径；没有用Redis mock代替实际测试。

实际测试、独立Sentinel实验及未覆盖项见[章节复核](chapter13-review.md)、[本轮测试原始结果](../benchmarks/review-2026-11-05.json)与[验证报告](../benchmarks/results.md)。书末讨论的附件、端到端加密、全文搜索及跨地域边缘缓存仍为扩展项；上游产品具备某功能，不代表本地已经实现或验证。
