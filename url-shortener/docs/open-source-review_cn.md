# 第9章：原书、开源实现与本地代码核对

核对日期：2026-10-31；收尾复验：2026-11-01。原书范围为仓库《System Design Interview.md》第9章；通过 CodeGraph 跟踪 `shorten → get_or_create → strategy → save` 和 `resolve → cache/storage → HTTP`。下表区分已运行的本地行为与部署层讨论。

## GitHub 检索与参考范围

| 项目 | 本次阅读入口 | 参考价值 |
|---|---|---|
| [YOURLS/YOURLS](https://github.com/YOURLS/YOURLS) | [创建短链源码](https://github.com/YOURLS/YOURLS/blob/master/includes/functions-shorturls.php)、[许可证](https://github.com/YOURLS/YOURLS/blob/master/LICENSE) | 数字 ID 转字符码、长 URL 去重、写库时仍需处理并发冲突 |
| [shlinkio/shlink](https://github.com/shlinkio/shlink) | [README](https://github.com/shlinkio/shlink/blob/develop/README.md) | 已验证存在可部署的 PHP 短链项目；本次未把其内部实现作为已核对的代码依据 |

YOURLS 的 `yourls_add_new_link` 会检查重复长 URL、从数字 ID 构造候选码，并处理插入阶段的并发异常。这说明“检查过不存在”仍不能替代数据库写入约束；本地采用 SQLite 唯一约束和事务保证接受结果。以上是设计参考，没有复制 PHP 实现或引入依赖；链接指向检索时的分支，后续内容可能变化。

## 章节逐项对照

| 书中内容 | 本地实现与证据 | 边界 |
|---|---|---|
| 创建 API、不可变映射、相同 URL 复用短码 | `URLShortener.shorten`、两种存储的 `get_or_create`；[回归测试](../tests/test_chapter_review.py) | 按输入字符串精确去重，不合并语义相近但字符不同的 URL |
| 哈希与冲突重试 | 默认 CRC32，加后缀重试，最终保存仍拒绝覆盖 | 没有同时提供 MD5/SHA-1；CRC32 只有32位，补足7位不扩大空间 |
| Base62 与唯一 ID | `Base62Strategy`、`SQLiteStorage.next_id`；书中 `11157 → 2TX`、`2009215674938 → zn9edcu` 已测试 | 七位容纳 `62**7` 种值；更大的 ID 继续增长，绝不截断 |
| ID 在重启和多实例间唯一 | 同一 SQLite 文件中的计数行；分配与保存共用写事务 | 所有 Base62 写入者必须采用该分配器；不是跨数据库或跨机房发号 |
| 映射持久化、双向唯一索引 | SQLite 的 code/url 唯一约束、`BEGIN IMMEDIATE` 内二次查询 | SQLite 为单写者；操作结束关闭连接，无分片和主从复制 |
| 重定向301/302 | WSGI `create_app`；302带 `Cache-Control: no-store` | 浏览器可能缓存301，因此服务端无法统计绕过自身的点击 |
| 缓存旁路读取 | 有容量上限的实例内 LRU，默认1024，0禁用；数据库回源在缓存锁外 | 不跨进程共享；不可变映射不需要更新失效协议 |
| 统计 | 每次成功 `resolve` 记录点击，命中缓存也记录 | 点击明细未持久化且无容量上限；高流量应接外部事件/统计系统 |
| 限流、无状态 Web 扩展、负载均衡、分片、复制、可用性 | 说明接入位置和部署边界 | 没有伪装成已实现的生产集群；可接第5章限流器和生产 WSGI 服务 |
| 可选 Bloom filter | 仍以存储唯一约束为准 | 本地没有 Bloom filter；当前规模增加它没有必要 |

## 本轮修复

1. 新增 SQLite 持久 ID 分配。`Base62Strategy(id_generator=storage)` 与 `URLShortener(storage=storage)` 使用**同一个对象**，使分配和映射保存复用事务；失败回滚时不会留下半条映射。独立调用 `next_id()` 会单独提交，允许出现跳号。
2. 已有不可变 URL 先只读返回，不再等待别的写入者释放写事务；未命中后仍取得写锁并重查，保留原子去重。新增测试持有另一条连接的写事务，确认重复请求能完成。
3. URL 校验统一到 `urls.redirect_uri`：非法主机、端口、空白、反斜杠、控制字符和不可编码 Unicode 统一报 `URLValidationError`。不访问目标网站。
4. 保存和库解析保留原 URL；HTTP `Location` 使用 IDNA 主机及 UTF-8 百分号编码，已有 `%xx` 不重复编码。中文 URL、IPv6 和已转义路径均有回归覆盖。
5. 只接受301/302配置；不再把0等错误值当成默认302。深度嵌套 JSON 返回400。
6. 演示服务器复用 WSGI 适配层，移除第二套 `/shorten`、`/stats` 演示协议。统一创建入口为 `POST /api/v1/data/shorten`，输入 `longUrl`，返回200和 `shortURL`；统计通过库方法读取。演示服务器仍是单线程，生产并发由 WSGI 服务器负责。

## URL转换的收尾复查

保留原URI中的IPvFuture方括号、显式空查询 `?` 和显式空片段 `#`。后者会影响重定向时是否继承源短链的片段；不能简单用 `urlunsplit` 丢弃空片段。[RFC9110 Location定义](https://www.rfc-editor.org/rfc/rfc9110.html#section-10.2.2)。

[Python标准库IDNA说明](https://docs.python.org/3.13/library/codecs.html#module-encodings.idna)指出其采用IDNA2003，IDNA2008需额外实现。本项目保持零运行时依赖：普通中文域名正常转换；若Unicode域名经标准库转换再解码后无法按NFC还原，则抛 `URLValidationError`，要求调用方明确提供ASCII ACE域名。例如拒绝静默把 `faß.de` 变成 `fass.de`，但接受显式 `xn--fa-hia.de`。这不是完整IDNA2008验证器，也不查验域名是否可访问。

## ID 与旧数据库

首次建立 `id_sequence` 时以已有 `urls.id` 最大值为起点，支持旧版本从1开始逐次创建的本地 Base62 数据；之后只读持久计数行，不扫描短码。SQLite ID 上限为 `2**63-1`，耗尽时抛 `OverflowError`，不会回绕。

`urls.id` 不一定等于自定义生成器的 ID，因此不能据此迁移任意外部发号数据。切换此类数据库之前，应核对并迁移序列；也不要在同一个短码空间混用不协调的 Hash/Base62/外部 ID 写入者。唯一约束始终拒绝覆盖，但无法自动修复不协调的生成策略。

## 验证

- 原有109项测试通过；新增36项针对持久分配、回滚、并发读取、输入和 HTTP 的回归测试，合计145项。
- 最初新增的25项测试在修复前有19项失败，修复后全部通过；另补2项序列耗尽/旧库初始化测试，以及9项URI边界测试（修复前7项失败、2项通过）。
- [跨进程与真实 HTTP 验证工具](../tools/verify_concurrency.py)：4个进程，共1000次创建调用，500条不同 URL 加1条共享 URL，恰好501个唯一映射；重启后全部可解析且新建不复用旧 ID。
- 真实回环 HTTP 验证200创建、中文 Location 的302、非法 URL 的400与未知短码的404；[原始结果](../benchmarks/review-2026-11-01.json)。该小规模验证不代表书中每天一亿条新链接的处理能力。
- 中英 README、设计、需求、任务清单及[学习路线](learning-guide_cn.md)同步更新。
- 收尾检查：21个Python文件通过3.9语法解析，17份文档中的494个本地链接有效，12段README示例可运行，学习路线两张Mermaid图成功渲染；Ruff关键错误检查、学习文档生成一致性及diff空白检查通过。
