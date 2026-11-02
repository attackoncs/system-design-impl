# 第 15 章：开源源码对照与实现核对

日期：2026-11-02。对照《System Design Interview.md》第 15 章“Design YouTube”。

## 开源参考

本轮通过 GitHub 搜索定位视频平台，并阅读上传校验、HLS 任务处理和转码任务的实际
源码。以下链接固定到检索所得提交，便于复核；参考其职责划分和失败处理思路，未复制
代码或增加这些项目的运行依赖。

| 项目与固定版本 | 实际阅读的源码 | 与本章相关的设计 |
| --- | --- | --- |
| [PeerTube](https://github.com/Chocobozzz/PeerTube/tree/fa7d2b5d8a44bbec99159b11e91763f7d2308faa) | [上传校验](https://github.com/Chocobozzz/PeerTube/blob/fa7d2b5d8a44bbec99159b11e91763f7d2308faa/server/core/middlewares/validators/resumable-upload.ts)、[HLS 任务](https://github.com/Chocobozzz/PeerTube/blob/fa7d2b5d8a44bbec99159b11e91763f7d2308faa/server/core/lib/runners/job-handlers/vod-hls-transcoding-job-handler.ts)、[任务生命周期](https://github.com/Chocobozzz/PeerTube/blob/fa7d2b5d8a44bbec99159b11e91763f7d2308faa/server/core/lib/runners/job-handlers/abstract-job-handler.ts) | 上传初始化只保留允许字段；完成入口跨进程互斥。HLS 任务持久记录标识、优先级、父依赖；所需音视频流齐备后才推进视频状态。任务结束唤醒依赖，失败有限重排，取消传播到子任务，并处理停服与迟到完成之间的竞争。 |
| [MediaCMS](https://github.com/mediacms-io/mediacms/tree/9bf46be5f4dc00005dd66e868f1d1d23371acadb) | [chunkize_media、EncodingTask、encode_media](https://github.com/mediacms-io/mediacms/blob/9bf46be5f4dc00005dd66e868f1d1d23371acadb/files/tasks.py#L221-L545) | FFmpeg stream-copy 生成中间块，按“清晰度 × 块”派发 Celery 任务，低清晰度优先。持久记录编码状态和进度，识别部分永久媒体错误，其他失败有限重试；成功前检查输出非空且包含媒体流。 |

### 如何用于本项目审查

1. **上传完成具有幂等语义。** 客户端收不到响应时可能重试，元数据更新、DAG 创建和
   对象引用必须在重复请求下保持一致。PeerTube 的完成入口互斥提供了相同问题的
   实际案例；本项目采用数据库事务和状态复核，不能直接照搬 Redis 会话锁。
2. **任务完成与视频可播放是两步。** 单个编码任务完成不足以发布视频；应等待所有
   必需输出，验证对象，并限制迟到结果对当前状态的影响。PeerTube 检查缺失流、
   分离任务完成状态，为本项目的发布任务和完成出站队列提供对照。
3. **重试应有终点，取消必须阻止后续推进。** 复核持久尝试预算、父依赖失败、下架、
   租约到期和停服期间的结果接收。MediaCMS 的错误分类与 PeerTube 的子任务传播
   是检查依据；本项目仍由现有租约和状态机维护这些不变量。
4. **并行粒度需要准确说明。** MediaCMS 展示了逐块、逐清晰度派发；本项目的持久
   GOP 预处理供重试复用，编码并发粒度为清晰度。引入逐 GOP 分布式编码还需要
   音视频边界、参数一致性和合并验证，属于额外实现范围，不能把已有预处理写成完成
   了逐 GOP 分布式编码。

PeerTube 和 MediaCMS 都是完整视频平台，其用户界面、社区功能和部署组件超出本章
上传、转码、播放的核心范围。使用这些源码进行核对，不据此声称本项目具备其全部
功能或生产规模。

## 章节覆盖与本轮判断

重新阅读根目录《System Design Interview.md》第15章（4765–5368行）。书中的主要
上传、转码和播放链路已有实现，本轮没有再引入一套任务队列或调度抽象。下面区分
可执行机制和需要基础设施/真实访问数据的部署优化。

| 章节要点 | 当前代码与验证 | 边界 |
| --- | --- | --- |
| 最大1GB、快速上传、断点续传与并行分片 | `VideoService.create/put_part/finish`：独立签名分片、幂等、长度/摘要、完整性复核；不同分片可并发写对象 | 字节分片，非客户端GOP切分；参考API接收PUT再写存储，非S3原生直传 |
| 视频上传与元数据并行 | 独立 `POST /metadata`；磁盘写入/组装在元数据锁外，最终事务接受引用 | 元数据仍有全局写锁 |
| 原始存储、编码存储与缓存 | LocalObjects/S3Objects 内容寻址对象、有限LRU、远端权威存在性和下载校验 | 单机缓存不是全球CDN；S3实验为Moto协议验证 |
| 配置生成DAG、服务端GOP预处理和中间结果复用 | Pipeline、JobQueue、FFmpegMedia；实际GOP→重启→编码复用测试 | 内置任务模板；编码并行粒度为清晰度，未实现逐GOP分布式编码/合并 |
| 任务优先级、Worker能力与运行队列 | 持久任务、依赖分页、能力筛选、空闲槽位拉取、1..32并行、租约续期和栅栏 | 没有集群最优放置、GPU利用率队列；过期恢复仍一次读取全部过期任务 |
| 多清晰度与平滑播放 | FFmpeg真实H.264/AAC HLS、对齐关键帧、范围/HEAD、两档HTTP解码 | 播放器UI和多协议支持未实现；本项目选择HLS |
| 缩略图与内容保护 | 可选缩略图；AES-128与签名播放授权覆盖清单、分片、密钥；下架覆盖缓存 | DRM、水印是书中其他选项，当前没有接入 |
| 完成队列与READY发布 | 持久outbox、输出对象齐备后更新状态，重启重放与下架竞争测试 | 数据库承担队列持久化；没有额外消息中间件 |
| 可恢复/不可恢复错误、工作进程退出 | 有限退避、持久尝试上限、完整媒体检查、过期接管、迟到结果拒绝、独立进程退出恢复 | 暂停且未提交的孤立产物可能在GC宽限后被清理，需要重试 |
| 数据库主备与API故障恢复 | PostgreSQL主节点发现/重连、未知提交不重放、跨连接申领；本轮真实PG18集成 | Patroni提升属于历史证据，本轮未复跑；元数据未分片 |
| 成本、就近上传、热门/冷门/地域策略 | [成本模型](../benchmarks/results.md#存储与分发成本模型)、现有部署适配接口 | 全球上传中心、CDN冷热分发、地域复制、按需编码尚未落地；需访问统计和真实服务验证 |

本轮保留现有职责边界：Service协调状态，Storage处理字节，JobQueue管理依赖与租约，
Worker运行FFmpeg，HTTP只做协议适配。未发现需要改动的Worker/授权功能缺陷；
现有并发、租约、失败与关闭测试用于核对这些机制，不把测试通过写成吞吐提升。

## 确认的问题与最小修复

### 上传后分片损坏被当成新源接收

原组装仅按对象键找到分片并拼接。若上传后磁盘内容被改成等长字节，未传整源
checksum的finish仍可能接受错误源、创建转码任务；总大小检查不能发现这种损坏。

`LocalObjects.assemble`现在在原有流式读取中逐片计算SHA-256，EOF时比较已接受的
对象键。不一致抛出`OSError`，write的finally删除组装临时文件；finish尚未创建任务，
视频保持UPLOADING。用原授权和原字节重传，原有_publish可以修复对象，再次finish
成功。校验复用1MiB缓冲，额外摘要计算在元数据锁外执行，无整文件内存副本或新锁。

新增 [test_storage_review.py](../tests/test_storage_review.py) 的4项回归先在原代码
全部失败，再在修复后通过；包括首/尾分片等长损坏、超过一个读取缓冲后的损坏、
无整源checksum时不得入队、无暂存残留、原授权修复重试。

### 测试错误响应未关闭

真实HTTP测试捕获416和403的HTTPError后未关闭响应。原31项断言虽通过，进程退出
时仍因socket ResourceWarning返回1。现用contextlib.closing包住原断言，不过滤警告。
tracemalloc与`-W error`下该文件31项通过且正常退出。

## 验证证据与复现

本轮覆盖全部 **164个不同测试**，分两次执行，未把重复运行的用例重复计数：

- 常规完整套件：**158 passed、6 skipped，17.86秒**；6项跳过明确需要PostgreSQL DSN。
- 独占PostgreSQL18 UTF8与Moto S3：`test_distributed.py` **9 passed，30.07秒**，
  包含上述6项及3项已在常规套件通过的S3测试。实际执行跨连接申领/重连、黑洞连接、
  未知提交、回滚、离线迁移，以及独立Worker经S3转码、HTTP解密播放。
- Worker/DAG专项30通过；HTTP/授权专项52通过。完整套件还包含真实FFmpeg、
  持久GOP复用、并发上限、慢I/O、停服排空、租约丢失和杀死Worker后接管。
- 自有WSL数据库、Moto服务和子进程已回收；数据库助手退出0，清理标记存在，
  自有Linux集群目录已不存在。复用缓存二进制，没有安装或下载服务。

在项目目录运行（PostgreSQL DSN指向可清理的测试实例；测试自行创建/删除隔离schema）：

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -W error -m pytest tests -q -p no:cacheprovider

$env:VIDEO_TEST_DATABASE_DSN='host=127.0.0.1 port=PORT user=USER dbname=postgres'
python -W error -m pytest tests/test_distributed.py -q -p no:cacheprovider
```

首次沙箱运行因本地硬链接/套接字受限产生WinError5/10013；允许本机文件和回环服务
后执行上述真实测试。未将环境权限错误通过放宽代码或跳过核心测试处理。

CodeGraph提供Service、HTTP、授权及存储/任务调用路径和当前源码；增量更新后的
code-review-graph对storage.py一层影响分析指出9个节点、4个附加文件，复核其S3继承
和存储测试调用方。动态调用和未跟踪文件有解析缺口，图不替代上述实际执行。

[本轮实测](../benchmarks/review-2026-11-02.json)完成三秒素材的144p/240p加密HTTP
解码，源120,988字节、输出377,358字节。单Worker顺序任务493.515ms，100次顺序
回环读取P99为27.561ms。这是小样本可运行性记录，没有同环境前后对照，不证明
并发容量、生产延迟或五百万日活规模；本轮未重新演练Patroni提升、S3存储HA或
跨物理主机网络分区。[测量方法](../benchmarks/results.md)

已同步中英文README、需求、设计、任务、根目录进度、开源索引和学习指南；
学习指南增加逐片完整性流程图及关键函数解读。

最终静态核验：33个项目Python文件通过3.9语法解析；源码及本轮测试的Ruff E9/F
通过，存储实现与新增测试两文件格式通过。test_video.py保留原有格式差异，只修改
两处响应关闭。17份文档的482个本地链接通过，27份学习指南生成一致，3幅Mermaid
图实际渲染通过，限定范围git diff --check通过。
