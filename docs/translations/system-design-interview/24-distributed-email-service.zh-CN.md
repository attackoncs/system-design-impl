# 24 分布式电子邮件服务

本章将设计一个大规模电子邮件服务，例如 Gmail、Outlook 或 Yahoo Mail。互联网的发展使电子邮件数量呈爆炸式增长。2020 年，Gmail 的活跃用户超过 18 亿，Outlook 的全球用户超过 4 亿 \[1\] \[2\]。

![图中并排展示了三家不同的电子邮件服务提供商。最左侧是 Outlook.com 标志：一个经过艺术化处理的蓝色方块，上面有白色圆形和部分叠加的信封图标，后面是“outlook.com”字样。中间是 Gmail 标志，即红白相间的信封图标，旁边是“gmail.com”字样。最右侧是紫色方块标志，其中有白色“Y!”，后面是“yahoo.com”字样。三个标志之间没有连接或信息流，只是分别代表不同的邮件平台。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fdistributed-email-service%2Ffigure-1-popular-email-providers-3H2F466J.png&w=3840&q=75)

图 1 常见电子邮件服务提供商

## 第 1 步：理解问题并确定设计范围

多年来，电子邮件服务的复杂度和规模都发生了显著变化。现代电子邮件服务是一个包含众多功能的复杂系统。我们不可能在 45 分钟内设计出一个真实生产系统。因此，在开始设计之前，一定要先提出澄清问题，缩小设计范围。

**候选人**：有多少人使用这个产品？

**面试官**：10 亿用户。

**候选人**：我认为以下功能很重要：

-   身份验证。
    
-   发送和接收邮件。
    
-   获取所有邮件。
    
-   按已读和未读状态筛选邮件。
    
-   按主题、发件人和正文搜索邮件。
    
-   反垃圾邮件和防病毒。
    

**面试官**：这些功能列得很好。我们不需要考虑身份验证，重点讨论你提到的其他功能。

**候选人**：用户如何连接邮件服务器？

**面试官**：传统上，用户通过原生客户端连接邮件服务器，使用 SMTP、POP、IMAP 以及厂商专有协议。这些协议在一定程度上已属于传统协议，但仍然十分流行。本次面试假设客户端与服务器之间使用 HTTP 通信。

**候选人**：邮件可以包含附件吗？

**面试官**：可以。

### 非功能需求

接下来，讨论最重要的非功能需求。

**可靠性。**不能丢失邮件数据。

**可用性**。邮件和用户数据应自动复制到多个节点，以确保可用性。此外，即使系统局部发生故障，系统也应继续运行。

**可扩展性**。随着用户数量增长，系统应能够处理不断增加的用户和邮件。系统性能不应随着用户或邮件数量增加而下降。

**灵活性和可扩充性**。灵活、可扩充的系统允许我们通过添加新组件，轻松增加新功能或提高性能。POP、IMAP 等传统邮件协议功能非常有限（将在总体设计中进一步讨论）。因此，我们可能需要自定义协议来满足灵活性和可扩充性要求。

### 粗略估算

下面进行粗略计算，确定系统规模，并找出方案需要应对的一些挑战。电子邮件应用本身就是存储密集型应用。

-   10 亿用户。
    
-   假设每人每天平均发送 10 封邮件。发送邮件的 QPS = 10^9 \* 10 / (10^5) = 100,000。
    
-   假设每人每天平均接收 40 封邮件 \[3\]，邮件元数据的平均大小为 50KB。元数据指除附件文件之外，与邮件有关的所有内容。
    
-   假设元数据存储在数据库中。保存 1 年元数据的存储需求：10 亿用户 \* 40 封邮件/天 \* 365 天 \* 50 KB = 730 PB。
    
-   假设 20% 的邮件包含附件，附件平均大小为 500 KB。
    
-   1 年附件的存储需求：10 亿用户 \* 40 封邮件/天 \* 365 天 \* 20% \* 500 KB = 1,460 PB
    

从粗略估算可以看出，我们需要处理海量数据。因此，很可能需要分布式数据库方案。

## 第 2 步：提出总体设计并达成共识

本节首先讨论邮件服务器的一些基础知识，以及邮件服务器随时间的发展演变，然后介绍分布式邮件服务器的总体设计。内容安排如下：

-   电子邮件基础知识
    
-   传统邮件服务器
    
-   分布式邮件服务器
    

### 电子邮件基础知识

发送和接收邮件可以使用多种邮件协议。过去，大多数邮件服务器使用 POP、IMAP、SMTP 等邮件协议。

#### 邮件协议

**SMTP**：简单邮件传输协议（SMTP）是在邮件服务器之间**发送**邮件的标准协议。

最常用的邮件**获取**协议是邮局协议（POP）和互联网邮件访问协议（IMAP）。

**POP** 是一种标准邮件协议，用于将邮件从远程邮件服务器接收并下载到本地邮件客户端。邮件下载到计算机或手机后，会从邮件服务器中删除，这意味着你只能在一台计算机或手机上访问这些邮件。RFC 1939 \[4\] 介绍了 POP 的详细信息。POP 要求邮件客户端下载整封邮件。如果邮件包含大型附件，下载可能需要很长时间。

**IMAP** 也是本地邮件客户端接收邮件的标准协议。阅读邮件时，客户端会连接外部邮件服务器，数据随后传输到本地设备。IMAP 只有在你点击某封邮件时才下载它，并且不会从邮件服务器中删除邮件，因此可以通过多台设备访问邮件。IMAP 是个人邮箱账户最常用的协议。它在网络连接较慢时也表现良好，因为在打开邮件前只下载邮件头信息。

**HTTPS** 严格来说不是邮件协议，但可以用来访问邮箱，尤其是网页邮箱。例如，Microsoft Outlook 通常通过 HTTPS，使用名为 ActiveSync \[5\] 的定制协议与移动设备通信。

#### 域名服务（DNS）

DNS 服务器用于查询收件人域名对应的邮件交换记录（MX 记录）。在命令行中对 gmail.com 执行 DNS 查询，可能会得到图 2 所示的 MX 记录。

![图中展示了 nslookup 命令的输出，说明查询域名 `gmail.com` 的邮件交换记录（MX 记录）的过程。顶部显示 nslookup 命令的执行：`set q=mx` 将查询类型设置为 MX 记录，随后查询 `gmail.com`。图中同时显示了服务器及其地址（192.168.86.1#53）。下方的“非权威应答”部分列出了 `gmail.com` 的五条 MX 记录。每条记录包含域名（`gmail.com`）、“邮件交换器”标签、等号、优先级数值（20、30、40、5、10），最后是相应的邮件服务器地址（例如 `alt2.gmail-smtp-in.l.google.com`）。底部三条箭头将输出连接到带标签的组成部分：“MX”指向“邮件交换器”部分，“优先级”指向优先级数值，“邮件服务器”指向邮件服务器地址。该图将查询、响应以及 MX 记录的组成部分直观联系起来，说明优先级如何决定联系邮件服务器的顺序。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fdistributed-email-service%2Ffigure-2-mx-records-BAWCU7VV.png&w=3840&q=75)

图 2 MX 记录

优先级数值表示优先选择顺序，数值越小的邮件服务器越优先。在图 2 中，首先使用 gmail-smtp-in.l.google.com（优先级为 5）。发送方邮件服务器会先尝试连接这台服务器并发送邮件。如果连接失败，则尝试连接优先级数值次小的邮件服务器，即 alt1.gmail-smtp-in.l.google.com（优先级为 10）。

#### 附件

邮件附件随邮件一起发送，通常采用 Base64 编码 \[6\]。邮件附件一般有大小限制。例如，截至 2021 年 6 月，Outlook 和 Gmail 的附件大小上限分别为 20MB 和 25MB。这一数值有很大的配置空间，个人账户和企业账户也有所不同。多用途互联网邮件扩展（MIME）\[7\] 是一种允许通过互联网发送附件的规范。

### 传统邮件服务器

在深入讨论分布式邮件服务器之前，先回顾一下历史，了解传统邮件服务器的工作原理。这有助于我们理解如何扩展邮件服务器系统。可以将传统邮件服务器理解为一种服务于有限数量邮件用户的系统，通常运行在单台服务器上。

#### 传统邮件服务器架构

图 3 展示了使用传统邮件服务器时，Alice 向 Bob 发送邮件的过程。

![图中用简化示意图展示了 Outlook.com 与 Gmail 之间发送和接收邮件的过程。Outlook.com 邮件客户端以 Outlook 标志表示，并标注“1 发送”，从这里发出邮件。邮件先发送到 Outlook.com SMTP 服务器，随后保存在 Outlook.com 邮件服务器的存储中，标注为“存储”和“outlook.com 邮件服务器”。接着，邮件被发送到 Gmail SMTP 服务器，标注“2 发送”，并保存到 Gmail 邮件服务器的存储中，标注为“存储”和“gmail.com 邮件服务器”。最后，Gmail 客户端以 Gmail 标志表示，并标注“4 获取”，通过 Gmail IMAP/POP 服务器获取邮件；该服务器从 Gmail 邮件服务器的存储中读取邮件。Outlook.com 和 Gmail 都使用 SMTP 服务器发送邮件，使用 IMAP/POP 服务器接收邮件。箭头表示邮件在不同组件之间的流动方向。“获取”和“存储”等标签分别说明 IMAP/POP 服务器和 SMTP 服务器执行的操作。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fdistributed-email-service%2Ffigure-3-traditional-mail-servers-F4GDMZFR.png&w=3840&q=75)

图 3 传统邮件服务器

整个过程分为 4 个步骤：

1.  Alice 登录 Outlook 客户端，撰写邮件并点击“发送”按钮。邮件被发送到 Outlook 邮件服务器。Outlook 客户端与邮件服务器之间使用 SMTP 协议通信。
    
2.  Outlook 邮件服务器查询 DNS（图中未显示），找到收件人的 SMTP 服务器地址。在本例中，该地址是 Gmail 的 SMTP 服务器地址。然后，它将邮件传送到 Gmail 邮件服务器。邮件服务器之间使用 SMTP 协议通信。
    
3.  Gmail 服务器存储邮件，使收件人 Bob 可以访问。
    
4.  Bob 登录 Gmail 时，Gmail 客户端通过 IMAP/POP 服务器获取新邮件。
    

#### 存储

在传统邮件服务器中，邮件存储在本地文件目录中，每封邮件保存为一个具有唯一名称的独立文件。每个用户都有自己的用户目录，用于保存配置数据和邮箱。Maildir 曾是邮件服务器上一种常用的邮件存储方式（图 4）。

![图中展示了一个分层文件系统结构。根节点是标记为“/”的文件夹，其下有一个“home”文件夹。“home”分出“user1”和“user2”两个文件夹。每个用户文件夹都连接到一个“Maildir”文件夹，而每个“Maildir”文件夹又有“cur”“new”和“tmp”三个子文件夹。文件夹之间的连线直观表示文件系统中的父子关系，展示各文件夹的嵌套方式。每个文件夹的标签都清楚标明其名称，简明展示了典型的用户主目录结构，其中专门的邮件目录包含当前邮件、新邮件和临时邮件的子目录。](https://bytebytego.com/images/courses/system-design-interview/distributed-email-service/figure-4-maildir-CI7G42PH.svg)

图 4 Maildir

用户规模较小时，文件目录的方式运行良好，但获取和备份数十亿封邮件十分困难。随着邮件量增长、文件结构愈加复杂，磁盘 I/O 成为瓶颈。本地目录也无法满足高可用性和可靠性要求。磁盘可能损坏，服务器也可能宕机。我们需要更加可靠的分布式存储层。

电子邮件自 20 世纪 60 年代诞生以来，功能已取得长足发展，从纯文本格式发展到支持多媒体、会话串联 \[8\]、搜索、标签等丰富功能。但是，邮件协议（POP、IMAP 和 SMTP）诞生已久，其设计既没有考虑这些新功能，也无法扩展到支持数十亿用户。

### 分布式邮件服务器

分布式邮件服务器旨在支持现代使用场景，解决规模和弹性问题。本节介绍邮件 API、分布式邮件服务器架构，以及邮件的发送和接收流程。

#### 邮件 API

对于不同的邮件客户端，或邮件生命周期的不同阶段，邮件 API 的含义可能截然不同。例如：

-   供原生移动客户端使用的 SMTP/POP/IMAP API。
    
-   发件方与收件方邮件服务器之间的 SMTP 通信。
    
-   基于 HTTP 的 RESTful API，用于功能完善、交互丰富的网页邮件应用。
    

受本书篇幅限制，我们只介绍网页邮箱中一些最重要的 API。网页邮箱通常通过 HTTP 协议通信。

**1\. 端点**：**POST /v1/messages**

向 To、Cc 和 Bcc 邮件头中指定的收件人发送邮件。

**2\. 端点**：**GET /v1/folders**

返回某个邮件账户的所有文件夹。

**响应**：

```
[{id: string        Unique folder identifier.
  name: string      Name of the folder.
                    According to RFC6154 [9], the default folders can be one of
                    the following: All, Archive, Drafts, Flagged, Junk, Sent,
                    and Trash.
  user_id: string   Reference to the account owner
}]

```

**3\. 端点**：**GET /v1/folders/{:folder\_id}/messages**

返回某个文件夹下的所有邮件。请注意，这是一个高度简化的 API，实际使用时还需要支持分页。

**响应**：

邮件对象列表。

**4\. 端点**：**GET /v1/messages/{:message\_id}**

获取指定邮件的所有信息。邮件是邮件应用的核心组成部分，包含发件人、收件人、邮件主题、正文、附件等信息。

**响应**：

一个邮件对象。

```
{
  user_id: string                      // Reference to the account owner.
  from: {name: string, email: string}  // <name, email> pair of the sender.
  to: [{name: string, email: string}]  // A list of <name, email> paris
  subject: string                      // Subject of an email
  body: string                         //  Message body
  is_read: boolean                     //  Indicate if a message is read or not.
}

```

#### 分布式邮件服务器架构

搭建一台服务少量用户的邮件服务器很容易，但要扩展到多台服务器则很困难。这主要是因为传统邮件服务器的设计只考虑了单服务器运行。在服务器之间同步数据并不容易，而且要防止邮件被收件人的邮件服务器误判为垃圾邮件，也极具挑战。本节将探讨如何利用云技术简化这些工作。总体设计如图 5 所示。

![图中展示了网页邮件应用的系统架构。顶部标注“网页邮箱”的方框中显示了 Edge、Chrome 和 Safari 浏览器图标，表示系统可通过不同的网页浏览器访问。下方的“Web 服务器”通过“https”连接浏览器，表示标准网页邮箱功能使用安全的 HTTP 通信。另一组“实时服务器”通过“WebSocket”连接浏览器，支持即时消息或通知等实时功能。底部为“存储层”，包含四个数据库圆柱体：“元数据数据库”（推测用于存储邮件元数据）、“附件存储”（存储邮件附件）、“分布式缓存”（用于性能优化）和“搜索存储”（用于邮件搜索）。该架构明确区分了表示层（浏览器）、应用层（Web 服务器和实时服务器）以及数据层（存储）。数据从浏览器单向流向服务器，再流向存储层，响应则沿原路径返回。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fdistributed-email-service%2Ffigure-5-high-level-design-AZNVIABQ.png&w=3840&q=75)

图 5 总体设计

下面详细了解各个组件。

**网页邮箱**。用户通过网页浏览器接收和发送邮件。

**Web 服务器。**Web 服务器是面向公网的请求/响应服务，负责登录、注册、用户资料等功能。在我们的设计中，发送邮件、加载邮件文件夹、加载文件夹下的所有邮件等所有邮件 API 请求，都经过 Web 服务器。

**实时服务器。**实时服务器负责将新邮件更新实时推送给客户端。由于需要维护持久连接，实时服务器属于有状态服务器。长轮询和 WebSocket 等方案都能支持实时通信。WebSocket 是更优雅的方案，但其缺点之一是浏览器兼容性。一个可行办法是尽可能建立 WebSocket 连接，并以长轮询作为后备方案。

现实中有一个邮件服务器实例 Apache James \[10\]，它实现了基于 WebSocket 的 JSON 元应用协议（JMAP）子协议 \[11\]。

**元数据数据库。**该数据库存储邮件元数据，包括邮件主题、正文、发件用户、收件用户等。我们将在深入设计部分讨论数据库选型。

**附件存储。**我们选择 Amazon 简单存储服务（S3）等对象存储来保存附件。S3 是一种可扩展的存储基础设施，适合保存图片、视频、文件等大型对象。附件大小可能达到 25MB。Cassandra 等 NoSQL 列族数据库可能不太适合，主要有以下两个原因：

-   虽然 Cassandra 支持 blob 数据类型，且 blob 的理论大小上限为 2GB，但实际限制小于 1MB \[12\]。
    
-   将附件放入 Cassandra 的另一个问题是，附件占用的内存空间过大，无法使用行缓存。
    

**分布式缓存。**客户端会反复加载最近的邮件，因此将近期邮件缓存在内存中，可以显著缩短加载时间。这里可以使用 Redis，因为它提供列表等丰富功能，而且容易扩展。

**搜索存储。**搜索存储是一种分布式文档存储，使用称为倒排索引 \[13\] 的数据结构，能够非常快速地执行全文搜索。我们将在深入设计部分进一步讨论。

讨论了构建分布式邮件服务器的一些关键组件之后，下面将它们组合成两个主要工作流程。

-   邮件发送流程。
    
-   邮件接收流程。
    

#### 邮件发送流程

邮件发送流程如图 6 所示。

![图中展示了网页邮件应用的系统架构。流程起点为网页邮箱客户端，以 Chrome、Edge 和 Safari 浏览器图标表示，客户端连接到负载均衡器（1）。负载均衡器将请求分发到以粉色显示的 Web 服务器（2）。这些 Web 服务器与存储层交互（3），存储层包括三个数据库（元数据、搜索存储和附件）以及一个对象存储，它们都向缓存提供数据。Web 服务器还将以信封图标表示的邮件（4.a 和 4.b）发送到同样以粉色显示的 SMTP 出站服务器（5）。SMTP 出站服务器执行垃圾邮件和病毒检查，必要时进行重试，然后将邮件发送到互联网（7）。数字 1—7 表示信息流经系统的过程。缓存保存最新邮件，以便更快地获取。整个架构展示了带有负载均衡、邮件处理和多数据库存储系统的客户端—服务器模型。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fdistributed-email-service%2Ffigure-6-email-sending-flow-U5I4QIEM.png&w=3840&q=75)

图 6 邮件发送流程

1.  用户在网页邮箱中撰写邮件并点击“发送”按钮，请求发送到负载均衡器。
    
2.  负载均衡器确保流量未超过速率限制，并将流量路由到 Web 服务器。
    
3.  Web 服务器负责以下工作：
    
    3a. 基本邮件校验。按照预定义规则检查每封传入邮件，例如邮件大小限制。
    
    3b. 检查收件人邮件地址的域名是否与发件人的域名相同。如果相同，Web 服务器会确保邮件数据不包含垃圾邮件或病毒。检查通过后，将邮件数据插入发件人的“已发送文件夹”和收件人的“收件箱”。收件人可以直接通过 RESTful API 获取邮件，无需执行第 4 步。
    
4.  消息队列。
    
    4a. 如果基本邮件校验通过，邮件数据会进入出站队列。如果附件太大，无法放入队列，可以将附件保存到对象存储，在队列消息中保存对象引用。
    
    4b. 如果基本邮件校验失败，邮件会进入错误队列。
    
5.  SMTP 出站工作进程从出站队列中拉取消息，并确保邮件不包含垃圾邮件或病毒。
    
6.  待发送邮件存储到存储层的“已发送文件夹”中。
    
7.  SMTP 出站工作进程将邮件发送到收件人的邮件服务器。
    

出站队列中的每条消息都包含创建邮件所需的全部元数据。分布式消息队列是支持异步邮件处理的关键组件。将 SMTP 出站工作进程与 Web 服务器解耦后，就可以独立扩展 SMTP 出站工作进程。

我们需要密切监控出站队列的长度。如果大量邮件滞留在队列中，就需要分析原因。可能的原因包括：

-   收件人的邮件服务器不可用。这种情况下，需要稍后重新尝试发送邮件。指数退避 \[14\] 可能是一种合适的重试策略。
    
-   发送邮件的消费者数量不足。这种情况下，可能需要增加消费者，以缩短处理时间。
    

#### 邮件接收流程

下图展示邮件接收流程。

![图中展示了邮件服务的系统架构。邮件（1）进入系统，首先由负载均衡器（2）处理，并被分发到 SMTP 服务器（2）。这些服务器按照邮件接收策略（1）运行。随后，邮件（4）进入邮件处理单元（5），执行垃圾邮件和病毒检查（5）。处理完成后，邮件（7）发送到实时服务器（7），由其负责实时更新和通知。同时，处理后的邮件保存在存储层（6）中，该层包含存储元数据及搜索索引的数据库、保存附件的对象存储，以及保存最新邮件的缓存。用户通过网页邮箱（8）访问邮件，实时更新使用 WebSocket（8），常规访问使用 HTTPS（9），连接 Web 服务器（9），最终从实时服务器（10）和存储层获取数据。数字标注（1—10）表示邮件和数据流经系统的过程。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fdistributed-email-service%2Ffigure-7-email-receiving-flow-HRTYUDLO.png&w=3840&q=75)

图 7 邮件接收流程

1.  传入邮件到达 SMTP 负载均衡器。
    
2.  负载均衡器将流量分发到 SMTP 服务器。可以在 SMTP 连接层配置并应用邮件接收策略。例如，退回无效邮件，以避免不必要的邮件处理。
    
3.  如果邮件附件过大，无法放入队列，可以将其放入附件存储（s3）。
    
4.  邮件放入入站邮件队列。队列将邮件处理工作进程与 SMTP 服务器解耦，使二者可以独立扩展。此外，邮件量激增时，队列也能发挥缓冲作用。
    
5.  邮件处理工作进程负责许多任务，包括过滤垃圾邮件、阻止病毒等。以下步骤假设邮件已通过校验。
    
6.  邮件存储到邮件存储、缓存和对象数据存储中。
    
7.  如果收件人当前在线，就将邮件推送到实时服务器。
    
8.  实时服务器是 WebSocket 服务器，使客户端可以实时接收新邮件。
    
9.  对于离线用户，邮件保存在存储层。用户重新上线后，网页邮箱客户端通过 RESTful API 连接 Web 服务器。
    
10.  Web 服务器从存储层拉取新邮件并返回给客户端。


## 第 3 步：深入设计

介绍完邮件服务器的各个部分后，下面深入讨论一些关键组件，并研究如何扩展系统。

-   元数据数据库
    
-   搜索
    
-   送达率
    
-   可扩展性
    

### 元数据数据库

本节讨论邮件元数据的特点、合适数据库的选择、数据模型，以及会话线程（加分项）。

#### 邮件元数据的特点

-   邮件头通常较小，访问频繁。
    
-   邮件正文大小不一，访问频率却较低。通常一封邮件只会读一次。
    
-   获取邮件、将邮件标记为已读、搜索等大多数邮件操作都局限于单个用户。也就是说，某个用户拥有的邮件只能由该用户访问，而且所有邮件操作都由同一用户完成。
    
-   数据的新旧程度影响其使用频率。用户通常只阅读最近的邮件。82% 的读取查询针对的是距今不足 16 天的数据 \[15\]。
    
-   数据的可靠性要求很高，不能接受数据丢失。
    

#### 选择合适的数据库

在 Gmail 或 Outlook 这样的规模下，数据库系统通常经过定制，以减少每秒输入/输出操作数（IOPS）\[16\]，因为它很容易成为系统的主要制约因素。选择合适的数据库并不容易。在决定最适合的方案之前，先考虑所有可选方案会很有帮助。

-   关系型数据库。选择它的主要动机是高效搜索邮件。可以为邮件头和正文建立索引。有了索引，简单的搜索查询就会很快。然而，关系型数据库通常针对较小的数据条目优化，并不适合较大的条目。一般邮件通常大于几 KB，包含 HTML 时很容易超过 100KB。你可能会说，BLOB 数据类型就是为了支持大型数据条目而设计的。但是，对非结构化 BLOB 数据进行搜索查询的效率不高。因此，MySQL、PostgreSQL 等关系型数据库并不合适。
    
-   分布式对象存储。另一种潜在方案是将原始邮件保存在 Amazon S3 等云存储中。这是备份存储的不错选择，但很难高效支持将邮件标记为已读、按关键词搜索邮件、组织邮件会话等功能。
    
-   NoSQL 数据库。Gmail 使用 Google Bigtable，因此它无疑是一种可行方案。但是，Bigtable 并未开源，其邮件搜索的实现方式仍然不为人知。Cassandra 也可能是不错的选择，但我们还没有看到大型邮件服务提供商使用它。
    

根据上述分析，现有方案中似乎很少有能完全满足需求的方案。大型邮件服务提供商往往会构建自己的高度定制数据库。不过，面试中没有时间设计一个新的分布式数据库，但解释数据库应具备的以下特点很重要。

-   单列大小可以达到几 MB。
    
-   数据强一致性。
    
-   设计以减少磁盘 I/O 为目标。
    
-   具备高可用性和容错能力。
    
-   易于创建增量备份。
    

#### 数据模型

一种数据存储方式是使用 _user\_id_ 作为分区键，使单个用户的数据保存在同一个分片上。这种数据模型的局限之一是，邮件无法由多个用户共享。但本次面试没有这项要求，因此无需考虑。

下面定义数据表。主键包含两个部分：分区键和聚簇键。

-   分区键：负责将数据分布到各个节点。通常，我们希望数据分布均匀。
    
-   聚簇键：负责对分区内的数据排序。
    

从整体上看，邮件服务需要在数据层支持以下查询：

-   第一种查询：获取某个用户的所有文件夹。
    
-   第二种查询：显示指定文件夹中的所有邮件。
    
-   第三种查询：创建、删除或获取指定邮件。
    
-   第四种查询：获取所有已读或未读邮件。
    
-   加分项：获取会话线程。
    

下面逐一介绍。

**查询 1：获取某个用户的所有文件夹。**

如表 1 所示，_user\_id_ 是分区键，因此同一用户拥有的文件夹位于同一个分区中。

![图中展示了名为“folders_by_user”的数据库表的模式设计。上方方框定义键的类型：“K”表示分区键，“C↑”表示升序聚簇键，“C↓”表示降序聚簇键。这些键用于组织表中的数据并支持高效查询。下方方框给出表结构本身，包含三列：“user_id”（UUID 类型，作为分区键）、“folder_id”（也是 UUID 类型）和“folder_name”（TEXT 类型）。“user_id”旁边的“K”明确表示它是分区键。其他列旁边没有“C↑”或“C↓”，说明在此设计中它们不是聚簇键。该表用于保存文件夹信息，通过相应的 UUID 将每个文件夹关联到特定用户。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fdistributed-email-service%2Ftable-1-folders-by-user-Q23KYRQB.png&w=3840&q=75)

表 1 按用户组织的文件夹

**查询 2：显示指定文件夹中的所有邮件。**

用户加载收件箱时，邮件通常按时间戳排序，最新邮件显示在最上方。为了将同一文件夹的所有邮件保存在一个分区中，使用复合分区键 _<user\_id, folder\_id>_。另一个需要注意的列是 _email\_id_。它的数据类型为 TIMEUUID \[17\]，并作为聚簇键，按时间顺序排列邮件。

![图中展示了名为“emails_by_folder”的表模式。该表有七列：`user_id`（UUID 数据类型，以“K”表示主键）、`folder_id`（UUID 数据类型，以“K”表示主键）、`email_id`（TIMEUUID 数据类型，以“C↓”表示主键，暗示其为聚簇主键）、`from`（TEXT 数据类型）、`subject`（TEXT 数据类型）、`preview`（TEXT 数据类型）和 `is_read`（BOOLEAN 数据类型）。该表似乎用于存储按用户和文件夹组织的邮件信息，每封邮件由其 `email_id` 唯一标识。“K”表示主键约束，“C↓”暗示 `email_id` 为聚簇主键，即数据行按此列物理排序，以便高效获取。数据类型说明各列保存的信息种类，例如通用唯一标识符（UUID）、结合时间戳的 UUID（TIMEUUID）、文本字符串和布尔值。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fdistributed-email-service%2Ftable-2-emails-by-folder-Y3QS42Z5.png&w=3840&q=75)

表 2 按文件夹组织的邮件

**查询 3：创建、删除或获取邮件**

受篇幅限制，这里只介绍如何获取邮件的详细信息。表 3 中的两个数据表就是为支持这一查询而设计的。简单的查询如下所示：

```
SELECT * FROM emails_by_user WHERE email_id = 123;
```

一封邮件可以包含多个附件，可以通过 _email\_id_ 和 _filename_ 字段的组合获取这些附件。

![图中展示了包含 `emails_by_user` 和 `attachments` 两个表的数据库模式设计。`emails_by_user` 表包含 `user_id`（UUID，主键，以“K”表示）、`email_id`（TIMEUUID，子键，以“C↓”表示）、`from`（TEXT）、`to`（LIST<TEXT>）、`subject`（TEXT）、`body`（TEXT）和 `attachments`（LIST<filename|size>）列。`attachments` 表包含 `email_id`（TIMEUUID，主键，以“K”表示）、`filename`（TEXT）和 `url`（TEXT）列。两个表之间存在一对多关系：`email_id` 列既作为 `emails_by_user` 中的子键，又作为 `attachments` 中的主键。这意味着 `emails_by_user` 中的一条邮件记录可以关联 `attachments` 表中的多条附件记录，它们通过共同的 `email_id` 连接。图中给出了各列的数据类型：UUID 用于唯一标识符，TIMEUUID 用于基于时间的唯一标识符，TEXT 用于文本字符串，LIST 用于值列表。“C↓”表示引用 `attachments` 表主键的子键。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fdistributed-email-service%2Ftable-3-emails-by-user-TDJAPLOC.png&w=3840&q=75)

表 3 按用户组织的邮件

**查询 4：获取所有已读或未读邮件**

如果领域模型面向关系型数据库，那么获取所有已读邮件的查询如下所示：

```
SELECT * FROM emails_by_folder
WHERE user_id = <user_id> and folder_id = <folder_id> and is_read = true
ORDER BY email_id;
```

获取所有未读邮件的查询非常相似，只需将上述查询中的“is\_read = true”改为“is\_read = false”。

不过，我们的数据模型是为 NoSQL 设计的。NoSQL 数据库通常只支持针对分区键和聚簇键的查询。由于 _emails\_by\_folder_ 表中的 _is\_read_ 两者都不是，大多数 NoSQL 数据库会拒绝这一查询。

绕过这一限制的一种方法是，获取用户整个文件夹的数据，再在应用程序中筛选。这种方法可能适用于小型邮件服务，但在我们设计的规模下效果不佳。

在 NoSQL 中，通常通过反规范化解决这一问题。为了支持已读/未读查询，将 _emails\_by\_folder_ 的数据反规范化为表 4 所示的两个表。

-   read\_emails：保存所有处于已读状态的邮件。
    
-   unread\_emails：保存所有处于未读状态的邮件。
    

将一封未读（UNREAD）邮件标记为已读（READ）时，先从 _unread\_emails_ 中删除这封邮件，再将其插入 read\_emails。

要获取指定文件夹中的所有未读邮件，可以执行如下查询：

```
SELECT * FROM unread_emails
WHERE user_id = <user_id> and folder_id = <folder_id>
ORDER BY email_id;
```

![图中并排展示了 `read_emails` 和 `unread_emails` 两个数据库表。每个表都包含相同的列：`user_id`（UUID 数据类型，标记为键“K”）、`folder_id`（UUID，标记“K”）、`email_id`（TIMEUUID，标记为聚簇键“C↓”）、`from`（TEXT）、`subject`（TEXT）和 `preview`（TEXT）。“K”表示主键，“C↓”表示聚簇键，暗示 `email_id` 决定各表中的行顺序。这些表很可能表示按用户和文件夹区分的已读与未读邮件数据，其中 `email_id` 唯一标识每封邮件，并为每个用户文件夹中的邮件提供基于时间的排序。“C↓”旁的向下箭头直观表示聚簇排序方向。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fdistributed-email-service%2Ftable-4-read-and-unread-emails-I5YQBZY7.png&w=3840&q=75)

表 4 已读和未读邮件

上述反规范化是一种常见做法。它会增加应用代码的复杂度和维护难度，但能够提高大规模场景中这类查询的读取性能。

**加分项：会话线程**

许多邮件客户端支持会话线程功能，将回复邮件与原始邮件分组 \[8\]，使用户可以获取同一会话中的所有邮件。传统上，会话线程通常通过 JWZ 算法 \[18\] 等算法实现。这里不讨论算法细节，只解释核心思想。邮件头通常包含以下三个字段：

```
{
  "headers" {
     "Message-Id": "<7BA04B2A-430C-4D12-8B57-862103C34501@gmail.com>",
     "In-Reply-To": "<CAEWTXuPfN=LzECjDJtgY9Vu03kgFvJnJUSHTt6TW@gmail.com>",
     "References": ["<7BA04B2A-430C-4D12-8B57-862103C34501@gmail.com>"]
  }
}
```

| Message-Id  | 邮件 ID 的值，由客户端在发送邮件时生成。 |
| ----------- | ------------------------------------------------------------ |
| In-Reply-To | 当前邮件所回复的父邮件的 Message-Id。          |
| References  | 与某个会话线程相关的邮件 ID 列表。                   |

表 5 邮件头

只要回复链中的所有邮件都已预加载，邮件客户端就能利用这些字段，从邮件中重建邮件会话。

#### 一致性权衡

依靠复制实现高可用性的分布式数据库，必须在一致性与可用性之间作出根本权衡。正确性对邮件系统十分重要，因此在设计上，我们希望每个邮箱只有一个主节点。发生故障转移时，客户端无法访问邮箱，其同步/更新操作会暂停，直到故障转移结束。这是以可用性换取一致性。

### 邮件送达率

搭建邮件服务器并开始发送邮件很容易，难的是让邮件真正进入用户的收件箱。如果邮件最终进入垃圾邮件文件夹，收件人很可能不会阅读它。垃圾邮件是一个严重问题。Statista 的研究 \[19\] 表明，所有发出的邮件中，超过 50% 是垃圾邮件。如果搭建一台新的邮件服务器，由于新服务器没有信誉记录，我们发送的邮件很可能进入垃圾邮件文件夹。要提高邮件送达率，需要考虑以下几个因素。

**专用 IP**。建议使用专用 IP 地址发送邮件。对于没有历史记录的新 IP 地址，邮件服务提供商通常不太愿意接收其发送的邮件。

**邮件分类**。使用不同的 IP 地址发送不同类别的邮件。例如，应避免从同一组服务器发送营销邮件和重要邮件，否则互联网服务提供商（ISP）可能将所有邮件都标记为推广邮件。

**发件人信誉。**逐步预热新邮件服务器的 IP 地址，建立良好信誉，这样 Office365、Gmail、Yahoo Mail 等大型服务提供商就不太会将我们的邮件放入垃圾邮件文件夹。根据 Amazon 简单邮件服务 \[20\] 的说明，预热一个新 IP 地址大约需要 2 至 6 周。

**及时封禁垃圾邮件发送者**。应在垃圾邮件发送者对服务器信誉造成明显影响之前，迅速将其封禁。

**反馈处理**。与 ISP 建立反馈回路十分重要，这有助于保持较低的投诉率，并及时封禁垃圾邮件账户。如果邮件未能送达，或者用户投诉，会出现以下情况之一：

-   硬退信。表示收件人的邮件地址无效，因此邮件被 ISP 拒绝。
    
-   软退信。表示邮件因临时原因未能送达，例如 ISP 过于繁忙。
    
-   投诉。表示收件人点击了“举报垃圾邮件”按钮。
    

图 8 展示了收集和处理退信/投诉的过程。我们为软退信、硬退信和投诉分别设置队列，以便单独管理。

![图中展示了一个简化的邮件反馈回路。流程从 ISP（互联网服务提供商）方框开始，它将“退信和投诉”发送到中央“反馈处理”单元。该单元处理接收到的信息，并将其分类为三个独立的输出流，以圆柱形容器表示。顶部圆柱体标注“软退信”，其中有三个邮件图标，表示临时投递失败。中间圆柱体标注“硬退信”，同样包含三个邮件图标，表示永久投递失败。底部圆柱体标注“投诉”，也包含三个邮件图标，表示用户举报不需要的邮件。箭头表示信息从 ISP 单向流向反馈处理单元，再流向三个输出类别。](https://bytebytego.com/images/courses/system-design-interview/distributed-email-service/figure-8-handle-feedback-loop-PFZ6YW6X.svg)

图 8 处理反馈回路

**邮件身份验证。**根据 Verizon 发布的《2018 年数据泄露调查报告》，网络钓鱼和借口欺骗占数据泄露事件的 93% \[21\]。常用的反钓鱼技术包括：发件人策略框架（SPF）\[22\]、域名密钥识别邮件（DKIM）\[23\]，以及基于域的消息身份验证、报告与一致性（DMARC）\[24\]。

图 9 展示了 Gmail 邮件头的一个示例。可以看到，发件人 @info6.citi.com 通过了 SPF、DKIM 和 DMARC 身份验证。

![图中展示了一个详细的邮件头，包含某封邮件的相关信息。邮件头由一系列键值对组成，每项独占一行。“邮件 ID”显示唯一标识符 `<617.3471674588.202105030141197779035.0039766680@info6.citi.com>`。“创建时间”表示邮件的创建时间戳：“2021 年 5 月 2 日，星期日，下午 6:41”，同时列出投递耗时（创建后 17 秒送达）。“发件人”显示使用“XyzMailer”的“Citi 提醒 <alerts@info6.citi.com>”。“收件人”字段部分遮盖了收件人的邮件地址，只显示“@gmail.com”。“主题”一行写着“您的 Citi® 账户对账单已准备就绪”。底部列出邮件身份验证结果：“SPF”显示“通过”，发送 IP 地址为 63.239.204.146；“DKIM”显示域名 info6.citi.com“通过”；“DMARC”同样显示“通过”。每项验证结果都包含“了解详情”超链接，推测指向相应验证方法的更多信息。各部分之间没有信息流，这是邮件元数据的静态展示。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fdistributed-email-service%2Ffigure-9-an-example-of-a-gmail-header-OW5IKA3C.png&w=3840&q=75)

图 9 Gmail 邮件头示例

不必记住所有这些术语。需要记住的是，要让电子邮件按预期工作十分困难。这不仅需要领域知识，也需要与 ISP 保持良好关系。

### 搜索

基本邮件搜索是指查找主题或正文包含任意输入关键词的邮件。更高级的功能包括按“发件人”“主题”“未读”或其他属性进行筛选。一方面，每当发送、接收或删除邮件时，都需要重建索引；另一方面，只有用户点击“搜索”按钮时，才会执行搜索查询。这意味着邮件系统的搜索功能中，写操作远多于读操作。与 Google 搜索相比，邮件搜索具有非常不同的特点，如表 6 所示。

| **范围**     | **排序**          | **准确性**                                                 |
| ------------- | -------------------- | ------------------------------------------------------------ |
| Google 搜索 | 整个互联网   | 按相关性排序                                            |
| 邮件搜索  | 用户自己的邮箱 | 按时间、是否有附件、日期范围、是否未读等属性排序。 |

表 6 Google 搜索与邮件搜索的比较

为支持搜索功能，我们比较两种方案：Elasticsearch，以及内置于数据存储中的原生搜索。

**方案 1：Elasticsearch**

使用 Elasticsearch 进行邮件搜索的总体设计如图 10 所示。由于查询主要在用户自己的邮件服务器上进行，可以使用 _user\_id_ 作为分区键，将底层文档分组到同一节点。

![图中展示了邮件管理的系统架构。顶层四个组件“搜索”“发送邮件”“接收邮件”和“删除邮件”代表用户操作。“发送邮件”“接收邮件”和“删除邮件”异步向中央“Kafka”消息队列发送数据。“搜索”功能则通过标注“同步”的连接访问下层组件。Kafka 中的数据由“Kafka 消费者”处理，再送入“RESTful API”。最后，RESTful API 与“Elasticsearch 集群”交互；该集群以三个数据库圆柱体表示，推测用于数据存储和获取。箭头表示数据流向，展示用户操作如何在系统中得到处理和存储。](https://bytebytego.com/images/courses/system-design-interview/distributed-email-service/figure-10-elasticsearch-MNL6TTHR.svg)

图 10 Elasticsearch

用户点击“搜索”按钮后，需要等待搜索响应返回。搜索请求是同步的。触发“发送邮件”“接收邮件”或“删除邮件”等事件时，无需向客户端返回任何与搜索有关的内容。此时需要重建索引，这项工作可以通过离线任务完成。设计中使用 Kafka，将触发索引重建的服务与实际执行索引重建的服务解耦。

截至 2021 年 6 月，Elasticsearch 是最流行的搜索引擎数据库 \[25\]，能够很好地支持邮件全文搜索。引入 Elasticsearch 的一个挑战是，保持主邮件存储与它同步。中国最大的邮件服务提供商之一腾讯 QQ 邮箱就使用了 Elasticsearch \[26\]。

**方案 2：自定义搜索方案**

大型邮件服务提供商通常自行开发定制搜索引擎，以满足特定需求。设计邮件搜索引擎是一项十分复杂的任务，超出了本章范围。这里只简要讨论磁盘 I/O 瓶颈，这是自定义搜索引擎面临的主要挑战之一。

粗略估算表明，每天新增的元数据和附件达到 PB 级。同时，一个邮件账户很容易积累超过 50 万封邮件。索引服务器的主要瓶颈通常是磁盘 I/O。

由于建立索引的过程以写入为主，一个不错的策略是使用日志结构合并树（LSM）\[27\] 组织磁盘上的索引数据（图 11）。写入路径只执行顺序写，从而得到优化。LSM 树是 BigTable、Cassandra、RocksDB 等数据库的核心数据结构。新邮件到达时，首先加入第 0 层的内存缓存；当内存中的数据量达到预设阈值时，数据被合并到下一层。使用 LSM 的另一个原因是，将频繁变化的数据与不常变化的数据分开。例如，邮件数据通常不会变化，而文件夹信息会因不同的筛选规则而更频繁地变化。在这种情况下，可以将二者放到两个不同区域；如果请求只涉及文件夹变化，就只修改文件夹，而不改动邮件数据。

如果你希望进一步了解邮件搜索，强烈建议阅读 Microsoft Exchange 服务器中搜索功能的工作原理 \[28\]。

![图中以水平分层的方式展示一个分层存储系统，每一层代表存储层次结构中的一级。最底层标注“第 0 层（内存）”，以红色显示，包含许多代表独立内存单元的小圆柱体。其上还有四层，分别标注“第 1 层（磁盘）”“第 2 层（磁盘）”“第 3 层（磁盘）”和“第 4 层（磁盘）”，每向上一层，圆柱体数量更少、体积更大。各层圆柱体表示该层的存储单元；向下接近第 0 层（内存）时，圆柱体的大小和数量逐渐增加。左侧竖直箭头表示时间流逝，暗示数据可能随时间在不同层级间移动。层间虚线直观区分不同存储层的边界，暗示它们具有不同的访问速度和容量。整个结构展示了计算机系统中缓存以及数据在不同存储层间移动的概念：底部内存更快、容量更小，顶部存储更慢、容量更大。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fdistributed-email-service%2Ffigure-11-lsm-tree-QD2BNIN2.png&w=3840&q=75)

图 11 LSM 树

每种方案各有利弊：

| **特性**        | **Elasticsearch**                                            | **自定义搜索引擎**                                     |
| ------------------ | ------------------------------------------------------------ | ------------------------------------------------------------ |
| 可扩展性        | 可以扩展到一定规模。                                     | 可以针对邮件使用场景优化系统，因此更容易扩展。 |
| 系统复杂度  | 需要维护数据存储和 Elasticsearch 两个不同系统。 | 一个系统。                                                  |
| 数据一致性   | 两份数据副本，一份在元数据存储中，另一份在 Elasticsearch 中。维护数据一致性很困难。 | 元数据存储中只有一份数据副本。             |
| 是否可能丢失数据 | 否。发生故障时，可以从主存储重建 Elasticsearch 索引。 | 否。                                                          |
| 开发工作量 | 易于集成。为支持大规模邮件搜索，可能需要专门的 Elasticsearch 团队。 | 开发定制邮件搜索引擎需要投入大量工程工作。 |

表 7 Elasticsearch 与自定义搜索引擎的比较

一般来说，对于规模较小的邮件系统，Elasticsearch 是不错的选择，因为它易于集成，无需投入大量工程工作。对于更大的规模，Elasticsearch 也可能适用，但可能需要专门团队开发和维护邮件搜索基础设施。要支持 Gmail 或 Outlook 规模的邮件系统，在数据库中内置原生搜索功能，可能比独立索引方案更合适。

### 可扩展性和可用性

由于各个用户的数据访问模式相互独立，我们预计系统中的大多数组件都能水平扩展。

为提高可用性，数据会复制到多个数据中心。用户与网络拓扑中物理位置更近的邮件服务器通信。发生网络分区时，用户可以从其他数据中心访问邮件（图 12）。

![图中对比了两种数据库复制架构。上半部分中，一位美国用户连接到美国数据中心的服务器，这些服务器再与同一数据中心内的数据库交互。数据通过一条标注“复制”的单向箭头，从美国数据中心的数据库复制到另一个欧洲数据中心的数据库。下半部分展示了修改后的架构：美国用户旁边出现红色“X”，表示故障或中断。此时，用户直接连接欧洲数据中心的服务器，这些服务器再与欧洲数据中心的数据库交互。两种架构的组成类似：用户、美国和欧洲数据中心中的服务器，以及两个数据中心内的数据库。主要区别在于用户的连接位置；下图暗示美国数据中心连接可能出现故障，强调了故障转移到欧洲数据中心的机制。](https://bytebytego.com/images/courses/system-design-interview/distributed-email-service/figure-12-multi-data-center-setup-FMIYSNFF.svg)

图 12 多数据中心部署

## 第 4 步：总结

本章介绍了构建大规模邮件服务器的设计。首先收集需求并进行粗略估算，以充分了解系统规模。在总体设计中，我们讨论了传统邮件服务器的设计方式，以及它们为何无法满足现代使用场景。还讨论了邮件 API，以及发送和接收流程的总体设计。最后，深入研究了元数据数据库设计、邮件送达率、搜索和可扩展性。

如果面试结束前还有时间，可以进一步讨论以下几点：

-   容错。系统的许多部分都可能发生故障，可以讨论如何处理节点故障、网络问题、事件延迟等情况。
    
-   合规。邮件服务面向全球，需要遵守相关法律法规。例如，必须按照《通用数据保护条例》（GDPR）\[29\] 的要求，处理和存储来自欧洲的个人身份信息（PII）。合法监听是这一领域的另一项典型功能 \[30\]。
    
-   安全。邮件包含敏感信息，因此邮件安全十分重要。Gmail 提供反钓鱼、安全浏览、主动提醒、账户安全、机密模式和邮件加密等安全功能 \[31\]。
    
-   优化。有时，同一封邮件会发送给多位收件人，群发邮件中的相同附件会在对象存储（S3）中重复保存多次。一种可行的优化是在执行成本较高的保存操作前，先检查存储中是否已存在该附件。
    

恭喜你读到这里！现在可以拍拍自己的肩膀，鼓励一下自己。做得很好！

## 本章小结

![图中用思维导图概述邮件服务器系统的设计过程。中心节点为“邮件服务器”，向外分出四个主要步骤。第 1 步详细列出功能需求（发送/接收、获取、筛选、搜索、反垃圾邮件）和非功能需求（可靠性、可扩充性、可扩展性）。第 2 步介绍邮件基础知识，包括协议（POP、IMAP、SMTP）、DNS/MX 记录查询、附件处理（MIME），以及传统邮件服务器与分布式邮件服务器之间的架构选择，并讨论邮件发送和接收流程的总体设计。第 3 步重点讨论数据库设计，包括元数据数据库选型、邮件送达率策略、搜索实现（Elasticsearch 或自定义方案），以及可扩展性、可用性和容错方面的考虑，处理节点故障、网络延迟等潜在问题。最后，第 4 步讨论合规（PII、GDPR）、安全（反钓鱼、加密）和优化（处理多收件人）。每个分支连接到表示具体设计方面的子分支，形成分层结构，直观展示设计过程的推进及各组件之间的关联。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fdistributed-email-service%2Fchapter-summary-MSPMW4ME.png&w=3840&q=75)

## 参考资料

\[1\] Gmail 活跃用户数量： [<u>https://financesonline.com/number-of-active-gmail-users/</u>](https://financesonline.com/number-of-active-gmail-users/)

\[2\] Outlook: [<u>https://en.wikipedia.org/wiki/Outlook.com</u>](https://en.wikipedia.org/wiki/Outlook.com)

\[3\] 2021 年每天发送多少封邮件？：

[<u>https://review42.com/resources/how-many-emails-are-sent-per-day/</u>](https://review42.com/resources/how-many-emails-are-sent-per-day/)

\[4\] RFC 1939——邮局协议第 3 版： [<u>http://www.faqs.org/rfcs/rfc1939.html</u>](http://www.faqs.org/rfcs/rfc1939.html)

\[5\] ActiveSync: [<u>https://en.wikipedia.org/wiki/ActiveSync</u>](https://en.wikipedia.org/wiki/ActiveSync)

\[6\] 邮件附件： [<u>https://en.wikipedia.org/wiki/Email_attachment</u>](https://en.wikipedia.org/wiki/Email_attachment)

\[7\] MIME: [<u>https://en.wikipedia.org/wiki/MIME</u>](https://en.wikipedia.org/wiki/MIME)

\[8\] 会话串联： [<u>https://en.wikipedia.org/wiki/Conversation_threading</u>](https://en.wikipedia.org/wiki/Conversation_threading)

\[9\] 用于特殊用途邮箱的 IMAP LIST 扩展： [<u>https://datatracker.ietf.org/doc/html/rfc6154</u>](https://datatracker.ietf.org/doc/html/rfc6154)

\[10\] Apache James: [<u>https://james.apache.org/</u>](https://james.apache.org/)

\[11\] 基于 WebSocket 的 JSON 元应用协议（JMAP）子协议：

[<u>https://datatracker.ietf.org/doc/rfc8887/</u>](https://datatracker.ietf.org/doc/rfc8887/)

\[12\] Cassandra 的限制：

[<u>https://cwiki.apache.org/confluence/display/CASSANDRA2/CassandraLimitations</u>](https://cwiki.apache.org/confluence/display/CASSANDRA2/CassandraLimitations)

\[13\] 倒排索引： [<u>https://en.wikipedia.org/wiki/Inverted_index</u>](https://en.wikipedia.org/wiki/Inverted_index)

\[14\] 指数退避： [<u>https://en.wikipedia.org/wiki/Exponential_backoff</u>](https://en.wikipedia.org/wiki/Exponential_backoff)

\[15\] QQ 邮箱系统优化（中文）： [<u>https://www.slideshare.net/areyouok/06-qq-5431919</u>](https://www.slideshare.net/areyouok/06-qq-5431919)

\[16\] IOPS: [<u>https://en.wikipedia.org/wiki/IOPS</u>](https://en.wikipedia.org/wiki/IOPS)

\[17\] UUID 和 timeuuid 类型： [<u>https://docs.datastax.com/en/cql-oss/3.3/cql/cql_reference/uuid_type_r.html</u>](https://docs.datastax.com/en/cql-oss/3.3/cql/cql_reference/uuid_type_r.html)

\[18\] 邮件会话串联： [<u>https://www.jwz.org/doc/threading.html</u>](https://www.jwz.org/doc/threading.html)

\[19\] 全球垃圾邮件数量： [<u>https://www.statista.com/statistics/420391/spam-email-traffic-share/</u>](https://www.statista.com/statistics/420391/spam-email-traffic-share/)

\[20\] 专用 IP 地址预热：

[<u>https://docs.aws.amazon.com/ses/latest/dg/dedicated-ip-warming.html</u>](https://docs.aws.amazon.com/ses/latest/dg/dedicated-ip-warming.html)

\[21\] 2018 年数据泄露调查报告：

[<u>https://enterprise.verizon.com/resources/reports/DBIR_2018_Report.pdf</u>](https://enterprise.verizon.com/resources/reports/DBIR_2018_Report.pdf)

\[22\] 发件人策略框架： [<u>https://en.wikipedia.org/wiki/Sender_Policy_Framework</u>](https://en.wikipedia.org/wiki/Sender_Policy_Framework)

\[23\] 域名密钥识别邮件： [<u>https://en.wikipedia.org/wiki/DomainKeys_Identified_Mail</u>](https://en.wikipedia.org/wiki/DomainKeys_Identified_Mail)

\[24\] 基于域的消息身份验证、报告与一致性： [<u>https://dmarc.org/</u>](https://dmarc.org/)

\[25\] DB-Engines 搜索引擎排名： [<u>https://db-engines.com/en/ranking/search+engine</u>](https://db-engines.com/en/ranking/search+engine)

\[26\] 重构实践：基于腾讯云 Elasticsearch 构建 QQ 邮箱全文搜索： [<u>https://www.programmersought.com/article/24097547237/</u>](https://www.programmersought.com/article/24097547237/)

\[27\] 日志结构合并树： [<u>https://en.wikipedia.org/wiki/Log-structured_merge-tree</u>](https://en.wikipedia.org/wiki/Log-structured_merge-tree)

\[28\] Microsoft Exchange 2014 大会：Exchange 中的搜索：

[<u>https://www.youtube.com/watch?v=5EXGCSzzQak&amp;t=2173s</u>](https://www.youtube.com/watch?v=5EXGCSzzQak&t=2173s)

\[29\] 通用数据保护条例：

[<u>https://en.wikipedia.org/wiki/General_Data_Protection_Regulation</u>](https://en.wikipedia.org/wiki/General_Data_Protection_Regulation)

\[30\] 合法监听： [<u>https://en.wikipedia.org/wiki/Lawful_interception</u>](https://en.wikipedia.org/wiki/Lawful_interception)

\[31\] 邮件安全： [<u>https://safety.google/intl/en_us/gmail/</u>](https://safety.google/intl/en_us/gmail/)

****

