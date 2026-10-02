# 聊天系统

基于《System Design Interview》第 13 章实现的 Python 聊天系统参考库。
核心仅依赖标准库，可选 WebSocket 适配层提供真实网络收发。

## 功能

- 令牌认证、独立设备会话、同设备重连替换和令牌撤销。
- 双人聊天、最多 100 人的小群聊、群主添加成员。
- 少于 100,000 字符的文本消息、递增消息 ID。
- SQLite 持久化、消息与收件箱原子写入、按用户扇出。
- 客户端消息 ID 幂等重试、按设备游标分页同步。
- 有界实时队列，慢消费者关闭后通过持久化收件箱恢复。
- 心跳超时、多设备在线状态聚合、状态订阅。
- 可替换离线推送适配器、按地域与容量选择服务的参考注册表。
- Redis 共享存储、独立聊天节点、持久化事件日志、共享在线状态、会话防冲突、
  租约发现、自动重连恢复及可重试推送工作进程。

架构及故障语义见 [设计文档](./docs/design.md)。

## 安装与验证

需要 Python 3.9 或更新版本。

```bash
cd chat-system
pip install -e .
pip install -e ".[dev]"
pytest -q
python examples/basic_chat.py
```

## 使用示例

```python
import asyncio
from chat_system import ChatService, SQLiteMessageStore

async def main():
    store = SQLiteMessageStore("chat.sqlite3")
    chat = ChatService(store)
    try:
        alice = chat.connect(chat.auth.issue("alice"), "phone")
        channel = chat.create_direct(alice, "bob")
        await chat.send(alice, channel.channel_id, "你好", "alice-1")
        bob = chat.connect(chat.auth.issue("bob"), "laptop")
        print(chat.sync(bob, after=0))
    finally:
        store.close()

asyncio.run(main())
```

默认数据库位于内存；传入文件路径才会跨重启保留历史和群成员信息。
默认认证器位于内存，重启后需重新配置令牌，或接入已有认证服务。

## WebSocket 服务

```bash
pip install -e ".[websocket]"
python examples/websocket_server.py
```

服务仅监听本机 `ws://127.0.0.1:8765`，数据库默认为 `chat-demo.sqlite3`，
可通过 `CHAT_DB` 改变路径。控制台会输出三个演示账号的令牌。
连接后使用文本 JSON 请求：

```json
{"request_id":1,"op":"login","token":"控制台中的令牌","device_id":"phone"}
{"request_id":2,"op":"direct","recipient_id":"bob"}
{"request_id":3,"op":"send","channel_id":"创建会话返回的ID","content":"你好","client_message_id":"request-1"}
{"request_id":4,"op":"sync","after":0,"limit":100}
{"request_id":5,"op":"heartbeat"}
```

响应通过 `request_id` 关联请求；实时消息和状态事件与响应分开。
支持 `group`、`add_member`、`history`、`subscribe_presence`、`logout`，
完整字段说明见 [英文文档](./README.md#websocket-demo)。

建议每五秒发送应用层心跳，默认三十秒超时。WebSocket 协议 ping
不能替代应用心跳。意外断网保留在线宽限期，显式退出立即关闭会话。

每台设备独立记录已处理完的同步游标。重连后分页拉取并处理消息，再保存
返回的 `next_cursor`。不能只因收到一条较新的实时消息，就跳过此前尚未处理的消息。
重复实时事件可按消息 ID 去重；收到关闭事件后重新连接并同步。
网络同步页同时限制字节大小，大量 Unicode 文本可能让一页少于请求的条数；
按返回的游标继续获取即可。

## 实现边界

- 本地模式使用单进程 SQLite；分布式模式使用独立节点与共享 Redis，
  尚未验证五千万日活吞吐量。
- 本地推送默认仅记录调用；分布式工作进程支持持久化重试和 HTTP 供应商桥接。
- 消息提交后才进行实时投递。中途崩溃可通过同步恢复消息，但实时投递及推送
  不承诺恰好一次。
- 本地注册表仅演示选择策略；分布式发现通过节点租约自动排除故障节点，
  并在会话接入时原子检查容量。
- 远程部署需要 TLS、外部认证和联系人可见性策略。
- 新成员不能查看加入前的消息；附件、端到端加密、搜索及已读回执不在本次范围。

## 分布式运行

安装 `pip install -e ".[distributed]"`。共享 Redis 需要 6.2 或更新版本，
开启 AOF、使用持久化数据目录，并采用 `noeviction` 策略。
项目提供 Redis、两个聊天节点、发现服务与推送工作进程的容器组合：

```bash
docker compose up --build -d
docker compose run --rm node-a token alice
docker compose run --rm node-a token bob
```

令牌保存在共享 Redis 中，重启聊天进程后仍可使用。令牌只在本机配置命令输出，
不提供公开注册接口。也可在已有 Redis 上分开启动进程：

```bash
python -m chat_system.cluster token alice
python -m chat_system.cluster token bob
python -m chat_system.cluster node --node-id node-a --port 8765 --public-url ws://127.0.0.1:8765 --region east
python -m chat_system.cluster node --node-id node-b --port 8766 --public-url ws://127.0.0.1:8766 --region west
python -m chat_system.cluster discovery --port 8080
python -m chat_system.cluster worker
```

通过 `CHAT_REDIS_URL` 配置 Redis；所有协作进程使用相同的 `CHAT_NAMESPACE`。
命令参数 `--redis-url` 放在子命令之前。节点可配置 `--capacity`、`--lease`、
`--poll`、`--heartbeat-timeout`。默认节点租约十秒、设备心跳超时三十秒。

发现服务默认位于 `http://127.0.0.1:8080/discover`，根据地域和利用率返回有效
WebSocket 地址。发送方与接收方可分别连接两个聊天节点，消息通过 Redis
持久化事件日志进行跨进程投递。

### 自动重连与同步

在宿主机配置已生成的令牌并启动客户端：

```powershell
$env:CHAT_TOKEN = '配置命令输出的令牌'
$env:CHAT_DEVICE_ID = 'phone'
python examples/reconnecting_client.py
```

客户端通过发现服务选择节点，周期性发送心跳，并在处理完消息页后将游标保存到
`chat-cursor.txt`。节点退出后，其租约到期，客户端重新发现有效节点并恢复未处理
消息。设备会话 UUID 可防止旧连接在另一节点重连后继续发送消息、覆盖心跳或退出
新会话。在线状态聚合所有仍有有效租约的设备，以 Redis 的时间为准。

### 持久化推送

默认工作进程将去重后的投递记录保存至 Redis。设置 `CHAT_PUSH_URL` 可实际向
供应商桥接接口发送 HTTP POST，`CHAT_PUSH_TOKEN` 可配置认证。请求包含用户与
消息，并携带稳定的 `Idempotency-Key`。供应商需使用该键去重；APNs、FCM 等
凭据由使用者提供，不在项目中硬编码。

工作进程会认领故障消费者留下的任务、持久化指数退避重试，默认五次失败后移入
死信流。外部投递是至少一次语义；处理时仍在线的用户会跳过推送。

### 验证及边界

```powershell
$env:CHAT_TEST_REDIS_URL = 'redis://127.0.0.1:6379/0'
pytest -q
```

分布式测试使用真实 Redis、独立操作系统聊天进程及实际网络连接，并强制终止一个
节点验证发现和恢复。未配置该变量时，相应测试会明确跳过。每项测试只清理自己的
随机命名空间，不清空整库。

消息、收件箱、事件与推送候选任务通过 Lua 原子写入。实时投递可因日志重放而重复，
客户端以持久化游标恢复并按消息 ID 去重。Redis 不可用时返回服务不可用，不回退到
内存并错误确认发送成功。

当前实现了聊天节点故障恢复，但提供的容器组合没有自动 Redis 主备切换或跨地域
复制。所有 Redis 键位于同一哈希槽，未实现存储分片，也未验证五千万日活容量。
消息与事件默认保留，生产部署需配置归档策略。远程部署还需要 TLS、Redis 认证及
可从客户端访问的节点地址。容器组合文件与直接多进程测试的验证范围分开说明。
