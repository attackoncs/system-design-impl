# 第 24 章：分布式邮件服务

标准库 Python 参考实现：HTTP 网关、2 个用户主分片、2 个异步只读副本、对象服务、本机 SMTP 网关与 2 个发送工作进程，共 9 个真实进程。支持邮件收发、附件、已读筛选、主题/发件人/正文搜索、投递去重、隔离和保留磁盘的进程恢复。

要求 Python 3.10+，无额外包或外部服务。邮件只在已配置的本机 `local.test` 邮箱流转，不查 MX、不向外部地址投递。测试扫描规则不是真实反病毒/反垃圾。

## 运行

```powershell
$env:PYTHONPATH = 'src'
python examples/network_demo.py
python -m unittest discover -s tests -v
python tools/benchmark.py --output benchmarks/results.json
```

在本目录执行。实验自动选择本机端口、生成私有凭据、启动并清理自有九个进程；数据与日志保留在忽略提交的 `.runtime/lab-*`。系统临时目录受限时创建 `.runtime/tmp`，将 `TEMP`/`TMP` 设为其绝对路径。证据见 [验证报告](benchmarks/results.md)。

## 手动启动

```powershell
$env:PYTHONPATH = 'src'
python tools/init_config.py --test-scanner
# 每条在独立终端运行，使用相同 PYTHONPATH：
python -m distributed_email.cli objects --config .runtime/secrets.json --port 9841
python -m distributed_email.cli primary --config .runtime/secrets.json --shard 0 --port 9842
python -m distributed_email.cli primary --config .runtime/secrets.json --shard 1 --port 9843
python -m distributed_email.cli replica --config .runtime/secrets.json --shard 0 --port 9844
python -m distributed_email.cli replica --config .runtime/secrets.json --shard 1 --port 9845
python -m distributed_email.cli smtp --config .runtime/secrets.json --port 9846
python -m distributed_email.cli api --config .runtime/secrets.json --port 9840
python -m distributed_email.cli worker --config .runtime/secrets.json --shard 0
python -m distributed_email.cli worker --config .runtime/secrets.json --shard 1
```

SMTP 协议端口为 9847，9846 是其管理 HTTP 接口。配置生成器拒绝覆盖现有文件，提供独立的用户、内部、SMTP、管理令牌与节点 URL。所有服务器仅绑定回环地址。省略 `--test-scanner` 时使用未配置扫描器策略：所有接收邮件进入隔离区并标记 `unscanned`。不要公开配置和运行数据。

## HTTP 邮箱接口

使用邮箱主人的 Bearer 令牌：

| 接口 | 功能 |
| --- | --- |
| `POST /v1/uploads` | `{filename,data}`，data 为 base64，返回本人 `upload_id` |
| `POST /v1/messages` | `{request_key,to,subject,body,attachment_ids}`，持久化发送队列 |
| `GET /v1/messages` | 最多 50 条；`folder/read/before/q/field` 筛选，`replica=true` 可选副本 |
| `GET /v1/messages/{id}` | 本人邮件元数据、纯文本正文、版本及复制位置 |
| `PUT /v1/messages/{id}` | `{version,read}`，版本条件更新 |
| `DELETE /v1/messages/{id}?version=N` | 条件删除及同步移除索引 |
| `GET /v1/messages/{id}/attachments/{index}` | 按本人邮件授权并校验 SHA256，index 从 0 开始 |

发送正文示例：

```json
{"request_key":"mail-001","to":["bob@local.test"],"subject":"Project update","body":"Searchable project notes","attachment_ids":[]}
```

可运行的上传/发送 Python 示例见 [英文说明](README.md)。相同用户请求键和相同内容返回原发送 ID，不同内容返回 409。收件人为 1–10 个不同的已配置本地邮箱。附件最多 3 个、每个 2 MiB，完整 RFC/MIME 最大 8 MiB（含编码膨胀），正文最多 65536 字符、主题 200 字符。禁止外部收件人。

文件夹为 `inbox`、`sent`、`spam`、`quarantine`，默认 inbox。`read=true/false` 按已读状态筛选，`next_before` 用于向前翻页。搜索 `field=subject/sender/body/all`，使用大小写折叠后的 Unicode 词项 AND 匹配，最多 5 个词、100 字符；不是子串、词干或附件内容搜索。每封邮件最多 4096 个字段词项。正文不可变，已读/删除需当前版本，并发冲突返回 409。

## 语义与运维

- HTTP 接受发送只表示发送副本和任务持久化。`smtp-accepted` 表示 SMTP 网关已接收，不等同所有收件箱可见。工作进程使用稳定原始消息、投递 ID、持久任务和租约重试。
- SMTP 网关在确认前保存对象引用和信封；确认丢失重传在网关和收件主库都不会重复入箱。收件去重凭据和删除墓碑保留，防止重放复活已删除邮件。
- 默认未配置扫描器时进入隔离区。测试规则只识别 `LOCAL_TEST_VIRUS_MARKER`、`LOCAL_TEST_SPAM_MARKER`，标注 `test-markers-only`；`test-policy-pass` 不表示无病毒。没有真实 AV 实现。扫描不可用保留任务重试，隔离附件禁止下载。
- 正文、原始 RFC 邮件和附件保存在独立对象服务，元数据库只含引用。对象读写检查内容 SHA256；用户不能凭猜到对象键绕过邮件所有权/隔离校验。
- 主库邮件与倒排索引同事务更新，主库搜索没有额外索引队列延迟。副本通过 HTTP 校验变更日志并原子更新索引/检查点，搜索和已读/删除可见性可能落后。
- 网关优先读取主库，主节点不可用时读只读副本；也可显式 `replica=true`。响应带来源、lineage 和 position。写入不自动切主，主库故障时明确失败；副本旧版本写主库可能冲突，需刷新后重试。
- 复制只包含邮箱元数据/索引，不包含可用于提升写主的发送队列、上传所有权或控制凭据。副本不能自动替代丢失的主库。

主/副本 `/internal/status` 提供复制位置、身份、待发送任务/错误和副本轮询状态；需内部令牌。变更、入箱和任务接口不通过公网网关转发。对象接口同样需内部令牌。

SMTP 管理接口 `/admin/status` 和 `/admin/control` 使用管理令牌；控制正文 `{"lose_ack":true,"scanner_down":true}` 模拟持久接收后返回 451 和扫描服务不可用，全部重置 false 后恢复。本地合成测试不会外发。

生产边界：暂无公网 SMTP、真实反病毒、完整 OAuth、TLS、DKIM/SPF/DMARC、HTML 客户端或 IMAP/POP。副本异步且无选主；对象与 SMTP 队列仍为单节点。无自动隐私保留、对象 GC 或变更日志清理。验证保留本地磁盘，不覆盖机器/磁盘丢失，也不代表原书十亿用户规模。更多细节见 [设计文档](docs/design.md)。

## 第24章工程复核（2026-10-26）

新增 `GET /v1/folders`（四个固定文件夹、总数及未读数）、
`GET /v1/folders/{folder}/messages` 和 `GET /v1/changes?after=0&wait=2`。
变更仅含本人邮件的 ID、版本、已读/删除标记和文件夹，不返回正文或附件。
续取需同时带 `after` 与返回的 `lineage`，每页最多100事件，`more` 表示还有下一页；
`position` 是续取游标，可能越过同分片其他用户的事件，`head` 是快照分片水位。
长轮询最多2秒，是持久化 HTTP 通知回退，并非 WebSocket/JMAP。
旧副本缺少历史日志时返回409，需重新拉取邮箱后从当前水位续取。

GET 可带 `min_position=N&lineage=L`：落后返回503，lineage更换返回409。
写回执包含同事务水位；未指定屏障的副本故障回退仍允许陈旧读取。
文件夹/列表元数据用按用户、筛选及精确lineage/position绑定的进程内LRU，
最多128项/4 MiB，返回私有副本，新提交后不复用旧缓存；没有真实Redis分布式缓存。

拆分记录契约、复制应用、接收spool、路由、direct传输及有界服务器生命周期。
发送claim租约30秒、共享20秒I/O预算；接收claim租约90秒、共享60秒预算，
持写锁后检查token和到期时间。邮件/索引/变更/复制位点原子提交，副本校验历史重放
并保留完整变更日志。SMTP AUTH用户名为 `local-worker`，配合私有SMTP令牌。
HTTP与SMTP默认各32并发，关闭时取消socket并排空自有线程；无法强杀任意SQL/CPU/DNS。
MIME验证解码后缺陷、重复字段、字符集及大小；嵌套message/rfc822或multipart容器附件
明确拒绝，普通附件使用octet-stream。对象有界读取、严格回执与SHA256/长度校验，
64个固定发布锁条带修复Windows重复上传替换竞争；对象目录仍须单进程拥有。

最终 **96项测试通过，30.741秒**，使用 `-W error` 和真实本机HTTP/SMTP九进程。
新增演示验证唯一入箱通知、续取版本2/3、副本读屏障及其他用户零事件。
详见[章节记录](docs/chapter24-review.md)和[实测报告](benchmarks/results.md)。未提交或推送。

真实AV/反垃圾和投递反馈、Cc/Bcc/会话线程、完整文件夹管理、公网SMTP/MX、TLS/OAuth/
DKIM/SPF/DMARC、共识/自动切主、对象与spool复制、备份/保留及十亿用户跨机验收仍未实现。
本地参考实现不代表章节全部生产要求已经完成。
