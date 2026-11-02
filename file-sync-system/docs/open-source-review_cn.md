# 第16章：开源源码对照与实现核对

日期：2026-11-02。对照根目录《System Design Interview.md》第16章“Design Google Drive”。

## 开源参考

本轮通过 GitHub 搜索定位 Syncthing 和 Seafile，并阅读分块、接收校验、文件替换及
冲突保留的实际源码。链接固定到检索所得提交，便于复核。借鉴职责划分和正确性
边界，未复制上游代码，也未引入两者的运行依赖。

| 项目及固定版本 | 实际阅读源码 | 与本章有关的机制 |
| --- | --- | --- |
| [Syncthing](https://github.com/syncthing/syncthing/tree/ed38528d7fba3dd6a42c9c9fc6780af432dc15d2) | [scanner/blocks.go](https://github.com/syncthing/syncthing/blob/ed38528d7fba3dd6a42c9c9fc6780af432dc15d2/lib/scanner/blocks.go)、[folder_sendrecv.go](https://github.com/syncthing/syncthing/blob/ed38528d7fba3dd6a42c9c9fc6780af432dc15d2/lib/model/folder_sendrecv.go) | 分块流式 SHA-256，复用32KiB读取缓冲。普通接收路径复核块长度和摘要；优先复用本地匹配块，缺失再拉取。待处理下载按字节预算限流。最终替换前检查本地文件是否已变化，冲突时移动旧内容并按配置限制副本数量。 |
| [Seafile 同步守护进程](https://github.com/haiwen/seafile/tree/af6539cebf8484fcd07a4cab7ddfdfc425034a76) | [block-mgr.c](https://github.com/haiwen/seafile/blob/af6539cebf8484fcd07a4cab7ddfdfc425034a76/common/block-mgr.c#L216)、[fs-mgr.c](https://github.com/haiwen/seafile/blob/af6539cebf8484fcd07a4cab7ddfdfc425034a76/common/fs-mgr.c#L267)、[组件说明](https://github.com/haiwen/seafile/blob/af6539cebf8484fcd07a4cab7ddfdfc425034a76/README.markdown) | 块管理器用固定缓冲读取并比对内容摘要。checkout先把服务端版本组装到临时文件，再备份旧文件并替换；替换失败时尝试恢复旧文件，保留冲突内容。下载支持从块偏移继续，错误不直接覆盖目标。 |

### 具体核对依据

1. **命中块索引仍需验证内容。** Syncthing 的 `copyBlockFromFile/verifyBuffer`
   在复用本地块时检查长度和摘要；普通下载也在写入前复核。索引、文件名或缓存存在
   不能单独证明块正确。本项目需分别核对上传块、服务器读取和设备缓存的验证路径。
2. **下载完成后仍可能有新的本地编辑。** Syncthing 的 `performFinish` 在替换前调用
   `scanIfItemChanged`，将尚未重新扫描的修改交回扫描流程。核对本项目时，应关注
   临时文件准备与最终替换之间的晚编辑，而不只检查开始下载时的文件状态。
3. **冲突保留和状态推进必须协调。** Seafile 的临时文件、备份、恢复与冲突路径，
   展示了文件系统替换和持久索引并非同一事务。核对本项目的整页应用、游标持久化、
   请求重试与本地保护副本时，需要确保失败不会被当成已应用，也不会覆盖尚未上传的
   本地修改。
4. **并发应受资源预算约束。** Syncthing 的 puller按待处理块字节数限流，读写缓冲
   复用。其实现可帮助检查内存和磁盘边界；本项目当前采用单设备串行同步、服务端
   有界HTTP与跨进程块锁，不据此声称已具备同等并行下载能力。

### 适用范围与差异

- Syncthing 是设备之间的持续同步系统，其对等设备选择、向量版本和目录索引架构
  不等同于本项目的中心服务、工作区ACL和修订条件写入。接收加密文件夹的块摘要是
  加密令牌，源码明确由另一端验证；不能把普通接收路径结论套用到所有模式。
- `haiwen/seafile` 当前是同步守护进程仓库；服务端核心位于独立的
  [haiwen/seafile-server](https://github.com/haiwen/seafile-server)。本轮阅读的是前者
  的块与checkout实现，未声称已审查后者的完整服务端拓扑。
- 阅读的 Seafile 块验证函数使用SHA-1，这是上游兼容协议的事实，不是本项目的
  算法建议；本地现有SHA-256与AES-256-GCM保持各自职责。Seafile支持的客户端加密
  也不能用来证明本项目服务端存储加密具备端到端加密语义。
- 两套项目都可作为工程实现参考，不能据此把本地SQLite、归档目录和进程锁标为
  已实现全球对象复制、元数据高可用、云冷存储或原生移动端。

## 章节覆盖与实现边界

重新阅读《System Design Interview.md》5369–5878行，核心上传、同步、修订、分享
和通知流程已有实现。本轮以可验证的异常缺口为修改范围，保留现有模块边界。

| 书中要点 | 当前实现和验证 | 限制 |
| --- | --- | --- |
| 任意文件、最大10GB、简单/断点上传 | 默认4MiB块、begin/put/commit、单块PUT /file、请求ID幂等、空文件与完整清单验证 | 实现上限10GiB；未实际上传10GiB，没有浏览器/原生移动端界面 |
| 带宽与存储优化 | SHA-256固定块、工作区内去重、缺失块上传、设备块缓存、zlib压缩 | 固定边界，插入字节导致后续边界移动时不等同rsync；没有按文件类型选择压缩器 |
| 静态加密 | HKDF派生工作区密钥、随机nonce、真实AES-256-GCM和认证元数据；解密/解压/摘要有界校验 | 主密钥随元数据保存，非端到端加密；TLS/KMS/密钥轮换未落地 |
| 强一致元数据、修订历史与冲突 | SQLite WAL写事务、不可变版本、条件base、先提交者获胜、另一份保留冲突、旧删除拒绝 | 单主机共享数据库和对象目录，不是跨主机HA或元数据分片 |
| pending与uploaded并行路径 | begin发送pending，块写入/校验在锁外，commit原子提交修订和upsert；未完成文件不可下载 | 完成由API事务推进，没有云存储回调服务 |
| 共享与撤销 | 工作区读/写ACL；服务器下载前后检查，交付事件前复核权限 | 不能擦除已下载的离线副本 |
| 下载、自动同步与离线恢复 | DeviceClient整页应用后确认，SyncWatcher有界扫描和退避；持久设备游标、通知事件、服务重启恢复 | 单设备串行，扫描非原生OS watcher；事件日志未做生产保留/重新快照 |
| 长轮询、通知容量与缓存 | 持久用户事件、有限HTTP槽位并预留前台容量；只缓存不可变清单并查当前ACL/head | 全局元数据锁及保守GC限制写并发；不是百万长连接服务 |
| 历史保留、去重和冷层 | 引用计数、显式prune、会话保护、保守GC、本地archive/restore | 无访问热度自动策略，也没有云Glacier |
| 服务/存储/队列故障 | 真实API重启、断点续传、设备重开、事件重放、块损坏/修复、事务失败与权限/取消竞争 | 负载均衡主备、对象跨区域复制、数据库提升、复制通知队列均未部署 |

服务器保持Service/BlockStore/EventLog职责，设备保持Client/Transport/Watcher职责。
本轮未发现存储、事务、权限或通知模块需要修改的确认缺陷，不为形式重建公共抽象。
既有有界并发、锁外慢I/O与跨进程块锁经过回归验证；单机结果不能说明生产吞吐。

## 本轮确认与修复

### 截断响应不能作为成功同步

原 `client_transport.request` 为限制内存和总期限使用read1分段读取。read1可在仍有
Content-Length剩余字节时返回EOF；如果前缀已组成合法JSON，旧代码会接受它。
对于changes响应，这可能让客户端采用原本不应接受的同步位置。块下载虽另有摘要
校验，HTTP传输层本身也不能将截断当完整。

现在读取结束后检查response.length是否已归零；剩余声明字节触发OSError。
chunked缺少终止块产生的IncompleteRead也转换为OSError，沿Watcher已有的I/O
退避重试，不额外引入重试器。合法无Content-Length的关闭定界响应仍兼容，原有
读取上限、套接字总期限和finally释放响应/连接/定时器保留。

[6项真实HTTP回归](../tests/test_client_review.py)在原代码下4失败、2通过，修复后
全部通过：JSON和块截断拒绝、完整和关闭定界响应接受、chunked截断进入I/O失败、
changes失败游标保持0且完整重试后推进至5。客户端/设备相关套件32项通过。

### 错误响应资源释放

既有HTTP测试中3处捕获代码共4个HTTPError响应未关闭，导致GC时socket
ResourceWarning。显式closing包住原断言，不过滤警告；tracemalloc与警告即错误
下HTTP套件40项通过。存储19项、服务/通知10项专项严格验证也通过，未修改其源码。

## 验证、测量与复现

原始完整严格基线 **115 passed、1 error（19.45秒）**，错误来自未关闭响应资源。
修复后完整套件 **122 passed（20.06秒），无跳过**，保留原116项并新增6项。
涵盖真实HTTP、独立设备进程、跨进程块锁、自动创建/修改/删除、离线重开、丢失
响应、晚编辑保护、权限竞争、长轮询容量和停服排空。

在项目目录运行：

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -W error -m pytest tests -q -p no:cacheprovider
python -W error tools/benchmark.py --output benchmarks/review-2026-11-02.json
```

本轮测量使用独立API与设备进程，实际回环HTTP、真实zlib/AES-GCM存储，
结束清理自有进程和临时目录。[原始记录](../benchmarks/review-2026-11-02.json)：
12,288字节三块文件首次上传并由第二设备拉取845.967ms；修改中间块后拉取90.401ms，
只下载4,096字节、复用8,192字节；API重启后持久游标恢复。两版逻辑内容24,576字节，
去重后明文块16,384字节，加密对象16,552字节。

首次时间包含进程启动，增量使用已初始化客户端，不是前后性能对照；没有生产
并发容量、10GiB实传或跨区域耐久性验证。[完整测量说明](../benchmarks/results.md)

CodeGraph提供客户端/传输、服务事务、HTTP、通知及块存储的当前源码与调用关系；
增量更新后code-review-graph的一层影响分析定位传输修改影响DeviceClient，进一步
实际验证push/pull、Watcher与游标恢复。动态调用有解析缺口，图不能替代执行测试。

同步更新中英文README、需求、设计、任务、章节核对、根目录进度、开源索引与
学习指南。学习指南增加传输完整性微观解释及第4幅流程图。

最终静态检查：22个项目Python文件通过3.9语法解析，源码和本轮测试的Ruff E9/F
通过，3个改动Python文件格式通过。16份文档的482个本地链接通过，27份学习指南
生成一致，4幅Mermaid图实际渲染通过，限定范围git diff --check通过。
