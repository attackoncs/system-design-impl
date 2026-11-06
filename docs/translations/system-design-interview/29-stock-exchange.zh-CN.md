# 29 证券交易所

本章将设计一个电子证券交易所系统。

交易所的基本功能是高效撮合买方和卖方，这项根本功能始终没有改变。在计算机兴起之前，人们通过以物易物和相互喊价来交换有形商品，寻找成交机会。如今，超级计算机悄无声息地处理订单，人们交易不仅是为了交换商品，也为了投机和套利。技术极大改变了交易格局，使电子市场交易量呈指数级增长。

提到证券交易所，大多数人会想到纽约证券交易所（NYSE）、纳斯达克等已有五十多年历史的主要市场参与者。实际上，还有许多其他类型的交易所。有些专注于金融行业的垂直细分领域，尤其重视技术 \[1\]，另一些则强调公平性 \[2\]。在深入设计之前，务必与面试官确认目标交易所的规模和重要特点。

先感受一下我们面对的问题规模：纽约证券交易所每天撮合数十亿笔交易 \[3\]，香港交易所每天的交易量约为 2000 亿股 \[4\]。图 1 展示了按市值计算跻身“万亿美元俱乐部”的大型交易所。

![图中用世界地图展示全球主要证券交易所的位置，通过不同颜色区分地区，并用图形大小表示市值。北美地区有三个橄榄绿色六边形，分别代表美国纳斯达克、纽约证券交易所和 TMX 集团，大小体现相对市值。欧洲地区有多个蓝色六边形，代表纳斯达克 OMX 北欧交易所、德意志交易所、泛欧交易所和瑞士证券交易所，同样以大小表示相对市值。亚洲地区有多个紫色六边形，代表深圳证券交易所、上海证券交易所、韩国交易所、日本交易所集团和香港交易及结算所有限公司，大小反映相对市值。澳大利亚地区只有一个棕色六边形，代表澳大利亚证券交易所。左侧独立图例使用同心八边形表示以万亿美元为单位的市值范围（20 万亿美元、5 万亿美元、0.5 万亿美元）。地图背景是浅灰色点阵，与六边形的交易所标记形成视觉对比。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fstock-exchange%2Ffigure-1-largest-stock-exchanges-VOCAUYH7.png&w=3840&q=75)

图 1 最大的证券交易所（来源：\[5\]）

## 第 1 步：理解问题并确定设计范围

现代交易所是复杂的系统，对延迟、吞吐量和稳健性都有严格要求。开始之前，先向面试官提出几个问题，澄清需求。

**候选人**：我们交易哪些证券？股票、期权还是期货？

**面试官**：为简化问题，只考虑股票。

**候选人**：支持哪些订单操作：下新单、撤单还是改单？需要支持限价单、市价单或条件单吗？

**面试官**：需要支持下新单和撤单。订单类型只需考虑限价单。

**候选人**：系统需要支持盘后交易吗？

**面试官**：不需要，只需支持正常交易时段。

**候选人**：能否介绍一下交易所的基本功能，以及用户数量、证券代码数量、订单数量等规模信息？

**面试官**：客户可以提交新的限价单或撤单，并实时接收撮合成交结果。客户可以查看实时订单簿，即买卖订单列表。交易所需要支持至少数万用户同时交易，以及至少 100 个证券代码。交易量方面，应支持每天数十亿笔订单。此外，交易所属于受监管机构，因此必须执行风控检查。

**候选人**：能否详细说明风控检查？

**面试官**：只做简单的风控检查。例如，单个用户每天最多只能交易 100 万股苹果股票。

**候选人**：我注意到您没有提及用户钱包管理。这也是我们需要考虑的吗？

**面试官**：考虑得很周到！我们需要确保用户下单时资金充足。如果订单在订单簿中等待成交，就需要冻结该订单所需的资金，防止超额支出。

#### 非功能需求

向面试官确认功能需求之后，还应确定非功能需求。实际上，“至少 100 个证券代码”和“数万用户”之类的要求表明，面试官希望我们设计一家中小规模交易所。在此基础上，还应确保设计可以扩展，以支持更多证券代码和用户。许多面试官会围绕可扩充性进一步追问。

非功能需求如下：

-   **可用性。**至少为 99.99%。可用性对交易所至关重要。即使只有几秒钟的停机，也可能损害声誉。
    
-   **容错。**需要容错能力和快速恢复机制，以限制生产事故的影响。
    
-   **延迟。**往返延迟应达到毫秒级，尤其关注第 99 百分位延迟。往返延迟的计时从市价单进入交易所开始，到市价单以已成交执行回报的形式返回为止。持续偏高的第 99 百分位延迟，会让少数用户的体验非常糟糕。
    
-   **安全。**交易所应具备账户管理系统。为符合法律法规，在开设新账户之前，交易所会执行 KYC（了解你的客户）检查，核验用户身份。对于包含行情数据的网页等公共资源，应防范分布式拒绝服务（DDoS）\[6\] 攻击。
    

#### 粗略估算

下面进行一些简单的粗略计算，了解系统规模：

-   100 个证券代码
    
-   每天 10 亿笔订单
    
-   纽约证券交易所的交易时间为周一至周五美国东部时间上午 9:30 至下午 4:00，总计 6.5 小时。
    
-   QPS：10 亿 / 6.5 / 3600 = ~43,000
    
-   峰值 QPS：5 \* QPS = 215,000。早间刚开盘和下午收盘前的交易量明显更高。
    

## 第 2 步：提出总体设计并达成共识

在深入总体设计之前，先简要介绍一些有助于设计交易所的基本概念和术语。

### 业务基础知识

**经纪商**

大多数散户通过经纪商在交易所交易。你可能熟悉的经纪商包括嘉信理财、Robinhood、Etrade、富达等。它们提供易用的界面，方便散户下单和查看行情数据。

**机构客户**

机构客户使用专门的交易软件开展大额交易。不同机构客户的需求也不同。例如，养老基金追求稳定收益，交易频率较低，但单笔交易量很大。它们需要订单拆分等功能，以尽量减少大额订单对市场的冲击 \[7\]。有些对冲基金专门从事做市，通过佣金返还获得收入。它们需要低延迟交易能力，显然不能像散户一样，只是在网页或移动应用中查看行情数据。

**限价单**

限价单是指定固定价格的买单或卖单，可能无法立即撮合，也可能只会部分成交。

**市价单**

市价单不指定价格，而是立即以当前市场价格执行。市价单以牺牲成本控制来保证成交，在某些快速变化的市场环境中很有用。

**行情数据级别**

美国股票市场的报价分为三个级别：L1（第一级）、L2 和 L3。L1 行情数据包括最优买价、最优卖价和相应数量（图 2）。买价指买方愿意购买股票的最高价格，卖价指卖方愿意卖出股票的最低价格。

![图中简化展示了苹果股票的订单簿，只显示最优买价、最优卖价及相应数量。顶行标明对应股票：“苹果股票”。下方表格分为三列：第一列标记订单类型（“最优卖价”和“最优买价”），第二列显示每股价格（最优卖价为 100.10，最优买价为 100.08），第三列显示对应价格下可交易的股数（最优卖价对应 1800 股，最优买价对应 2000 股）。数量以方框呈现，其中最优买价的数量用浅绿色突出显示，与最优卖价的数量区分。图中没有 URL 或参数，仅展示某一时刻订单簿快照的静态数据。](https://bytebytego.com/images/courses/system-design-interview/stock-exchange/figure-2-level-1-data-A5YJ6H3X.svg)

图 2 L1 数据

L2 包含比 L1 更多的价格档位（图 3）。

![图中简化展示了苹果股票的订单簿，显示最优买卖价及其相应数量。示意图分为“卖单簿”和“买单簿”两部分。“卖单簿”展示卖方愿意出售的价格（卖价），从最优卖价 100.10 向下排列到“卖盘深度”100.13，各档价格对应的数量分别为 1800、2000、1500 和 300。同样，“买单簿”展示买方愿意购买的价格（买价），从最优买价 100.08 向上排列到“买盘深度”100.05，相应数量分别为 2000、800、2000 和 600。两部分都按“价格”和“数量”两列排列，清楚显示股票价格，以及每个价位的卖出或买入股数。数量采用表格形式展示，通过位置和标签隐含突出最优买价与最优卖价。整个结构直观呈现苹果股票当前的市场深度与流动性，展示各个价格档位的可用买卖订单。](https://bytebytego.com/images/courses/system-design-interview/stock-exchange/figure-3-level-2-data-7KGI4YP5.svg)

图 3 L2 数据

L3 展示各价格档位以及每档排队等待的数量（图 4）。

![图中展示了苹果股票的订单簿，包含买卖价格及对应数量。上方标注“卖单簿”，左侧一列列出卖价（100.13、100.12、100.11、100.10），右侧表格显示相应数量。最高卖价（100.10）标注为“最优卖价”。数量以阶梯形排列，表示各价格档位的卖盘深度。一条水平箭头从数量表指向“价格档位”标签。下方标注“买单簿”，以同样方式展示买价（100.08、100.07、100.06、100.05）和对应数量。最高买价（100.08）标注为“最优买价”。买单簿中的数量也呈阶梯形，展示各价格档位的买盘深度。最优买价对应的数量单元格以浅绿色突出显示。整张图标题为“苹果股票”，清楚说明标的资产。](https://bytebytego.com/images/courses/system-design-interview/stock-exchange/figure-4-level-3-data-BSBEFO33.svg)

图 4 L3 数据

**K 线图**

K 线图表示某一时间段内的股票价格。典型的单根 K 线如图 5 所示。一根 K 线展示某一时间区间内市场的开盘价、收盘价、最高价和最低价。常见时间区间为 1 分钟、5 分钟、1 小时、1 天、1 周和 1 个月。

![图中展示了 K 线图的一个元素，即单根 K 线，说明资产在特定时间段内的价格变动。竖直、较粗的矩形“实体”表示开盘价（“开盘”）与收盘价（“收盘”）之间的价格范围。箭头分别从“开盘”和“收盘”标签指向矩形的顶部和底部。从实体顶部向上延伸的细线称为“上影线”，表示该时间段达到的最高价（“最高”）；箭头从“最高”指向上影线顶端。同样，从实体底部向下延伸的细线为“下影线”，表示达到的最低价（“最低”）；箭头从“最低”指向下影线底端。实体与影线构成的整个结构竖直对齐，由一条粗竖线连接，表示该时间段的价格波动轨迹。](https://bytebytego.com/images/courses/system-design-interview/stock-exchange/figure-5-a-single-candlestick-chart-4FUB4O22.svg)

图 5 单根 K 线

**FIX**

FIX 协议 \[8\] 的全称是金融信息交换协议（Financial Information eXchange），创建于 1991 年，是一种不依赖特定厂商、用于交换证券交易信息的通信协议。下面是使用 FIX 编码的一笔证券交易示例。

8=FIX.4.2 | 9=176 | 35=8 | 49=PHLX | 56=PERS | 52=20071123-05:30:00.000 | 11=ATOMNOCCC9990900 | 20=3 | 150=E | 39=E | 55=MSFT | 167=CS | 54=1 | 38=15 | 40=2 | 44=15 | 58=PHLX EQUITY TESTING | 59=0 | 47=C | 32=0 | 31=0 | 151=15 | 14=0 | 6=0 | 10=128 |

FIX 示例 \[8\]

### 总体设计

了解关键概念的基础知识之后，下面来看图 6 所示的总体设计。

![图中展示了股票交易平台的系统架构。客户的计算机（1）连接到经纪商（14），后者再与客户端网关（13）交互。客户端网关（13）将订单信息（3）发送到订单管理器（其中包括钱包（6））。订单管理器执行汇总风控检查（4、5），再将订单（7）发送到序列器（8）。序列器（8）随后将订单（10）发送到交易所内的撮合引擎（9），撮合引擎与订单簿（9）交互。行情数据沿 M1、M2、M3 从行情数据发布器流向数据服务，并通过虚线返回订单管理器，该虚线表示报表流程（R1、R2）。订单管理器还向报表生成器发送数据（R1），由其将订单和成交数据保存在数据库中。数字 1—14 表示关键路径（实线），虚线表示行情数据流程（M1、M2、M3）和报表流程（R1、R2）。整个系统被虚线框包围，代表整个平台。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fstock-exchange%2Ffigure-6-high-level-design-A3NJ5JVE.png&w=3840&q=75)

图 6 总体设计

下面沿订单在图中各组件间流转的生命周期，了解各部分如何配合。

首先，跟随订单经过**交易流程**。这条关键路径有严格的延迟要求，流程中的所有操作都必须快速完成：

第 1 步：客户通过经纪商的网页或移动应用下单。

第 2 步：经纪商将订单发送到交易所。

第 3 步：订单通过客户端网关进入交易所。客户端网关执行输入校验、限流、身份验证、规范化等基本把关功能，然后将订单转发到订单管理器。

第 4—5 步：订单管理器根据风控管理器设定的规则执行风控检查。

第 6 步：风控检查通过后，订单管理器验证钱包中的资金是否足以支付订单。

第 7—9 步：订单发送到撮合引擎。找到可匹配订单时，撮合引擎产生两份成交执行回报（也称成交回报），分别对应买方和卖方。为确保重放时撮合结果具有确定性，订单和成交回报都要在序列器中排序（稍后详述）。

第 10—14 步：成交回报返回给客户。

接下来，沿着**行情数据流程**，跟随订单成交数据从撮合引擎经数据服务流向经纪商。

第 M1 步：撮合引擎在完成撮合时生成成交回报流，并将其发送到行情数据发布器。

第 M2 步：行情数据发布器根据成交回报流和订单流构建 K 线图和订单簿，作为行情数据，再将行情数据发送到数据服务。

第 M3 步：行情数据保存到用于实时分析的专用存储中。经纪商连接数据服务，获取及时的行情数据，再将这些数据转发给客户。

最后，考察**报表生成流程**。

第 R1—R2 步（报表流程）：报表生成器从订单和成交回报中收集所有必需的报表字段（例如 client\_id、price、quantity、order\_type、filled\_quantity、remaining\_quantity），将整合后的记录写入数据库。

请注意，交易流程（第 1 至第 14 步）位于关键路径，而行情数据流程和报表流程不在关键路径上。它们的延迟要求不同。

下面更详细地研究这三个流程。

#### 交易流程

交易流程位于交易所的关键路径上，所有操作都必须快速完成。交易流程的核心是撮合引擎，我们先从它开始。

**撮合引擎**

撮合引擎也称交叉撮合引擎，主要职责如下：

1.  维护每个证券代码的订单簿。订单簿是某个证券代码对应的买卖订单列表。稍后的“数据模型”一节将解释订单簿的构建方式。
    
2.  撮合买单和卖单。每次撮合产生两份成交回报，分别对应买方和卖方。撮合功能必须快速、准确。
    
3.  将成交回报流作为行情数据分发。
    

高可用的撮合引擎实现必须能够按照确定的顺序产生成交结果。也就是说，给定一个已知订单序列作为输入，重放该序列时，撮合引擎必须输出同样的成交回报序列。这种确定性是高可用性的基础，我们将在深入设计部分详细讨论。

**序列器**

序列器是确保撮合引擎具有确定性的关键组件。每笔传入订单在交给撮合引擎处理之前，都会被序列器加上序列 ID。撮合引擎完成的每对成交回报也会被加上序列 ID。换言之，序列器包含入站和出站两个实例，各自维护自己的序列。每个序列器生成的序列必须是连续的数字，以便轻松发现缺号。详情见图 7。

![图中简化展示了订单处理系统的架构。中央组件是一个标注“订单管理器”的矩形框，代表管理订单的核心逻辑。右侧另一个矩形框为“撮合引擎”，负责撮合买卖订单。订单管理器与撮合引擎之间，竖直排列着“入站序列器”（上方）和“出站序列器”（下方）两个矩形框，以多条竖线组成的数组表示，暗示类似队列的结构。箭头表示信息流向：订单管理器向两个序列器发送数据，入站序列器将数据转发给撮合引擎，撮合引擎通过出站序列器将数据发回订单管理器。这表明序列器负责安排处理顺序，管理订单管理器与撮合引擎之间的通信，可能还会缓冲数据，确保处理高效且有序。](https://bytebytego.com/images/courses/system-design-interview/stock-exchange/figure-7-inbound_and_outbound_sequencers-3MF75AIL.svg)

图 7 入站和出站序列器

为传入订单和传出成交回报添加序列 ID，主要有以下原因：

1.  及时性和公平性
    
2.  快速恢复/重放
    
3.  恰好一次保证
    

序列器不仅生成序列 ID，还充当消息队列：一个队列向撮合引擎发送消息（传入订单），另一个队列向订单管理器返回消息（成交回报）。它还是订单和成交回报的事件存储。这类似于为撮合引擎连接两条 Kafka 事件流，一条用于传入订单，另一条用于传出成交回报。实际上，如果 Kafka 的延迟更低、更可预测，就可以使用 Kafka。我们将在深入设计部分讨论低延迟交易所环境中序列器的实现方式。

**订单管理器**

订单管理器一端接收订单，另一端接收成交回报，负责管理订单状态。下面详细介绍。

订单管理器从客户端网关接收传入订单，并执行以下操作：

-   将订单送去进行风控检查。我们的风控检查需求很简单，例如验证用户每天的交易金额低于 100 万美元。
    
-   结合用户钱包检查订单，验证资金是否足以支付交易。“数字钱包”一章已详细讨论钱包，可参考该章了解适用于交易所的实现。
    
-   将订单发送到序列器，为订单添加序列 ID。排序后的订单由撮合引擎处理。一笔新订单包含许多属性，但无需将所有属性都发送给撮合引擎。为减小数据传输中的消息体积，订单管理器只发送必要属性。
    

另一端，订单管理器通过序列器接收撮合引擎的成交回报，并通过客户端网关将已成交订单的成交回报返回给经纪商。

订单管理器应快速、高效、准确，维护订单的当前状态。实际上，管理各种状态转换的难题，是订单管理器复杂度的主要来源。真实交易所系统中可能涉及数万种情况。事件溯源 \[9\] 非常适合订单管理器的设计。我们将在深入设计部分讨论基于事件溯源的设计。

**客户端网关**

客户端网关是交易所的入口把关组件。它接收客户提交的订单，并将其路由到订单管理器。图 8 展示了网关提供的功能。

![图中简化展示了系统网关的架构，网关以标注“网关”的虚线框表示。框内有五个以网格排列的矩形组件，每个代表一项独立功能。从上到下、从左到右依次为“身份验证”“校验”（数据校验）、“限流”（流量控制）、“规范化”（数据转换）和“FIXT 支持”（推测指支持金融信息交换协议 FIXT）。这些组件之间没有明确连线，暗示它们在网关内顺序或并行处理传入请求。数据隐含流经这些组件，例如请求会依次经过身份验证、校验、限流、规范化，最后按需进行 FIXT 协议处理，再进入图中未展示的后端系统。该图仅关注网关的内部结构和功能，未展示外部交互或依赖。](https://bytebytego.com/images/courses/system-design-interview/stock-exchange/figure-8-client-gateway-components-COTYQAOK.svg)

图 8 客户端网关组件

客户端网关位于关键路径，对延迟敏感，因此应保持轻量，尽快将订单传送到正确的目的地。上述功能虽然关键，也必须尽快完成。哪些功能放入客户端网关，哪些不放，是一个设计权衡。一般而言，应将复杂功能留给撮合引擎和风控检查组件。

针对散户和机构客户，需要不同类型的客户端网关。主要考虑因素是延迟、交易量和安全要求。例如，做市商等机构提供了交易所流动性的很大一部分，它们要求极低延迟。图 9 展示了连接交易所的不同客户端网关。一种极端情况是主机托管（colo）引擎，即运行在经纪商租用的交易所数据中心服务器上的交易引擎软件。此时，延迟基本就是光从托管服务器传播到交易所服务器所需的时间 \[10\]。

![图中展示了交易所的系统架构，有三个外部实体与交易所内部组件交互。“交易所网站/应用”通过 HTTP 与“应用/Web 网关”通信。“经纪商/交易商”和“其他 API 用户”同时连接到“API 网关（FIX/非 FIX）”及“托管引擎”。“应用/Web 网关”和“API 网关”都将流量传入“分片服务”，后者在更大的“交易所”组件中以服务集群表示。箭头指明信息流向，展示外部实体的请求如何经过网关处理，并最终到达交易所内部的分片服务。“托管引擎”可能代表交易所使用的主机托管设施或基础设施组件。](https://bytebytego.com/images/courses/system-design-interview/stock-exchange/figure-9-client-gateway-W5TCL3VV.svg)

图 9 客户端网关

#### 行情数据流程

行情数据发布器（MDP）从撮合引擎接收成交回报，并根据成交回报流构建订单簿和 K 线图。订单簿和 K 线图统称为行情数据，稍后的“数据模型”一节将进一步讨论。行情数据发送到数据服务，供订阅者使用。图 10 展示了 MDP 的一种实现，以及它与行情数据流程中其他组件的关系。

![图中简化展示了行情数据提供器（MDP）系统的架构。左侧“撮合引擎”组件处理订单，并输出“订单、撮合结果”。输出被送入 MDP 组件，后者是一个更大的方框，内部包含三个“订单簿”组件，暗示不同资产或市场对应多个订单簿。订单簿下方的“K 线图”组件处理撮合后的订单数据，可能用于生成可视化的 K 线图。“K 线图”组件的输出流向“持久化”组件，由其保存处理后的数据。最后，“数据服务”组件访问持久化数据，可能通过 API 或接口供外部系统获取行情数据。整个流程从订单撮合单向流向数据存储和外部访问。](https://bytebytego.com/images/courses/system-design-interview/stock-exchange/figure-10-market-data-publisher-NW4AZKEC.svg)

图 10 行情数据发布器

#### 报表流程

报表是交易所不可或缺的一部分。报表生成器虽然不在交易关键路径上，却是系统的重要组成部分。它提供交易历史、税务申报、合规报告、结算等功能。效率和延迟对交易流程至关重要，而报表生成器对延迟不那么敏感，其关键要求是准确性和合规性。

将传入订单和传出成交回报中的属性拼接起来，是一种常见做法。传入的新订单只包含订单详情，而传出的成交回报通常只包含订单 ID、价格、数量和执行状态。报表生成器合并这两个来源的属性，生成报表。图 11 展示了报表流程中各组件如何配合。

![图中简化展示了订单处理和报表系统的架构。“订单管理器”向“撮合管理器”发送“订单”，后者返回“成交/拒绝”结果。“订单管理器”还向“报表生成器”组件发送信息。“报表生成器”内部处理 `NewOrderReq`（新订单请求），生成 `NewOrderAck`（新订单确认）和作为“响应”的 `Fill`（成交）文档。这些内部操作最终生成 `ExecutionReport`（执行报告）文档，再将其传给“交收与清算”和“账簿与记录”组件。这两个组件随后向“报表”组件提供数据，最终将数据写入圆柱体符号表示的数据库。“交收与清算”组件还从一个数据库（以圆柱体表示）接收输入，“账簿与记录”组件则输出到另一个数据库（以圆柱体表示）。箭头表示信息或数据在不同组件之间的流向。](https://bytebytego.com/images/courses/system-design-interview/stock-exchange/figure-11-reporter-OKUG5GXU.svg)

图 11 报表生成器

细心的读者可能注意到，本章“第 2 步：提出总体设计并达成共识”的章节顺序与其他章略有不同。本章将 API 设计和数据模型放在总体设计之后。这是因为这些小节需要用到总体设计中引入的一些概念。

### API 设计

了解总体设计后，下面来看 API 设计。

客户通过经纪商与证券交易所交互，下单、查看成交回报、查看行情数据、下载历史数据进行分析等。下面使用 RESTful 风格的 API 定义经纪商与客户端网关之间的接口。下文提及的资源可参见“数据模型”一节。

请注意，REST API 可能无法满足对冲基金等机构客户的延迟要求。为这些机构开发的专用软件可能采用其他协议，但无论采用什么协议，都需要支持下述基本功能。

**订单**

```
POST /v1/order
```

此端点用于下单，需要身份验证。

参数

```
symbol: the stock symbol. String
side: buy or sell. String
price: the price of the limit order. Long
orderType: limit or market (note we only support limit orders in our design). String
quantity: the quantity of the order. Long
```

响应

响应体：

```
id: the ID of the order. Long
creationTime: the system creation time of the order. Long
filledQuantity: the quantity that has been successfully executed. Long
remainingQuantity: the quantity still to be executed. Long
status: new/canceled/filled. String
rest of the attributes are the same as the input parameters
```

状态码：

```
200: successful
40x: parameter error/access denied/unauthorized
500: server error
```

**成交回报**

```
GET /execution?symbol={:symbol}&orderId={:orderId}&startTime={:startTime}&endTime={:endTime}
```

此端点查询成交信息，需要身份验证。

参数

```
symbol: the stock symbol. String
orderId: the ID of the order. Optional. String
startTime: query start time in epoch \[11\]. Long
endTime: query end time in epoch. Long
```

响应

响应体：

```
executions: array with each execution in scope (see attributes below). Array
id: the ID of the execution. Long
orderId: the ID of the order. Long
symbol: the stock symbol. String
side: buy or sell. String
price: the price of the execution. Long
orderType: limit or market. String
quantity: the filled quantity. Long
```

状态码：

```
200: successful
40x: parameter error/not found/access denied/unauthorized
500: server error
```

**订单簿**

```
GET /marketdata/orderBook/L2?symbol={:symbol}&depth={:depth}
```

此端点按指定深度查询某个证券代码的 L2 订单簿信息。

参数

```
symbol: the stock symbol. String
depth: order book depth per side. Int
```

响应

响应体：

```
bids: array with price and size. Array
asks: array with price and size. Array
```

状态码：

```
200: successful
40x: parameter error/not found/access denied/unauthorized
500: server error
```

**历史价格（K 线图）**

```
GET /marketdata/candles?symbol={:symbol}&resolution={:resolution}&startTime={:startTime}&endTime={:endTime}
```

此端点根据给定的时间范围和时间粒度，查询某个证券代码的 K 线图数据（参见“数据模型”一节的 K 线图）。

参数

```
symbol: the stock symbol. String
resolution: window length of the candlestick chart in seconds. Long
startTime: start time of the window in epoch. Long
endTime: end time of the window in epoch. Long
```

响应

响应体：

```
candles: array with each candlestick data (attributes listed below). Array
open: open price of each candlestick. Double
close: close price of each candlestick. Double
high: high price of each candlestick. Double
low: low price of each candlestick. Double
```

状态码：

```
200: successful
40x: parameter error/not found/access denied/unauthorized
500: server error
```

### 数据模型

证券交易所主要有三类数据，下面逐一介绍。

-   产品、订单和成交回报
    
-   订单簿
    
-   K 线图
    

#### 产品、订单、成交回报

产品描述某个交易证券代码的属性，例如产品类型、交易代码、界面显示代码、结算货币、每手数量、最小价格变动单位等。这些数据不经常变化，主要用于界面显示，可以保存在任何数据库中，而且非常适合缓存。

订单表示传入的买入或卖出指令，成交回报表示传出的撮合结果。成交执行结果也称为成交（fill）。并非每笔订单都有成交回报。撮合引擎的输出包含两份成交回报，分别代表撮合交易的买卖双方。

图 12 的逻辑模型图展示了三个实体之间的关系。请注意，它不是数据库模式。

![图中展示了包含“订单”“成交回报”和“产品”三个实体的数据库模式。“订单”实体包含 `orderID`（UUID）、`productID`（int）、`price`（long）、`quantity`（long）、`side`（Side）、`orderStatus`（OrderStatus）、`orderType`（OrderType）、`timeInForce`（TimeInForce）、`symbol`（long）、`userID`（long）、`clientOrderID`（string）、`broker`（string）、`accountID`（long）、`entryTime`（long）和 `transactionTime`（long）等属性，它与“成交回报”实体之间是一对多关系（1:0..n）。“成交回报”实体包含 `execID`（UUID）、`orderID`（UUID）等属性，与“订单”中的多个属性相同，另外增加了 `feeCurrency`（Currency）、`feeRate`（long）、`feeAmount`（long）和 `execStatus`（ExecStatus）。最后，“订单”实体与“产品”实体是一对一关系（1:1）。“产品”实体包含 `productID`（int）、`symbol`（type）、`lotSize`（int）、`tickSize`（decimal）、`quoteCurrency`（Currency）、`settleCurrency`（Currency）、`description`（string）和 `field`（type）属性。关系由带标签的箭头表示，标签给出连接的基数。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fstock-exchange%2Ffigure-12-product-order-execution-CMK5RE7H.png&w=3840&q=75)

图 12 产品、订单和成交回报

订单和成交回报是交易所最重要的数据，在总体设计介绍的三个流程中都会出现，只是形式略有不同。

-   在关键交易路径上，订单和成交回报不存储到数据库中。为实现高性能，这条路径在内存中执行交易，并利用硬盘或共享内存持久化和共享订单及成交回报。具体来说，订单和成交回报保存在序列器中，用于快速恢复；收盘后，再归档数据。我们将在深入设计部分讨论序列器的一种高效实现。
    
-   报表生成器将订单和成交回报写入数据库，以支持对账、税务申报等报表使用场景。
    
-   成交回报转发给行情数据处理器，用于重建订单簿和 K 线图数据。下面讨论这些数据类型。
    

#### 订单簿

订单簿是针对特定证券或金融工具、按价格档位组织的买卖订单列表 \[12\] \[13\]，是撮合引擎实现快速订单撮合的关键数据结构。高效的订单簿数据结构必须满足以下要求：

-   常数时间查找。操作包括获取某个价格档位或一段价格区间内的数量。
    
-   快速添加、撤销和执行操作，最好具有 O(1) 时间复杂度。操作包括下新单、撤单和撮合订单。
    
-   快速更新。对应操作为改单。
    
-   查询最优买价/卖价。
    
-   遍历价格档位。
    

下面通过图 13 的例子，说明订单如何在订单簿上成交。

![图中展示了苹果股票订单簿的买卖双方。上方标注“卖单簿”，表示卖盘，价格从 100.13 到 100.10 降序排列，每个价格档位都有以表格展示的对应数量。“最优卖价”（最低卖出价）为 100.10。下方标注“买单簿”，表示买盘，价格从 100.05 到 100.08 升序排列，各档同样以表格列出对应数量。“最优买价”（最高买入价）为 100.08。虚线将“卖盘深度”与“最优卖价”分开，买盘也采用类似方式。箭头指示一笔买入 2700 股的订单。该订单从最优卖价开始，逐档向上匹配卖盘的数量，直到累计达到 2700 股。最后的计算“2700 - 200 - 400 - 1100 - 100 - 900 = 0”表明整笔订单全部成交。成交数量在卖盘中以浅粉色突出显示，在买盘中以浅绿色突出显示。“价格档位”标签表示价格的水平排列方式。](https://bytebytego.com/images/courses/system-design-interview/stock-exchange/figure-13-limit-order-book-illustrated-4HLSPSXB.svg)

图 13 限价订单簿示意

在上例中，有一笔买入 2700 股苹果股票的大额市价单。买单与最优卖价队列中的全部卖单，以及 100.11 价格队列中的第一笔卖单撮合。这笔大单成交后，买卖价差扩大，价格上升一个档位（此时最优卖价为 100.11）。

下面的代码片段展示订单簿的一种实现。

```
class PriceLevel{
    private Price limitPrice;
    private long totalVolume;
    private List<Order> orders;
}
class Book<Side> {
    private Side side;
    private Map<Price, PriceLevel> limitMap;
}
class OrderBook {
    private Book<Buy> buyBook;
    private Book<Sell> sellBook;
    private PriceLevel bestBid;
    private PriceLevel bestOffer;
    private Map<OrderID, Order> orderMap;
}
```

这段代码满足上面列出的所有设计要求吗？例如，添加或撤销限价单时，时间复杂度是否为 O(1)？答案是否定的，因为这里使用了普通列表（**\*private** List<Order> orders\*）。要构建更高效的订单簿，需要将“orders”的数据结构改为双向链表，使删除类操作（撤单和撮合）也具有 O(1) 复杂度。下面说明如何让这些操作达到 O(1) 时间复杂度：

1.  下新单意味着在 _PriceLevel_ 尾部添加一个新的 _Order_。对于双向链表，这一操作的时间复杂度为 O(1)。
    
2.  撮合订单意味着从 PriceLevel 头部删除一个 _Order_。对于双向链表，这一操作的时间复杂度为 O(1)。
    
3.  撤单意味着从 OrderBook 中删除一个 _Order_。我们利用 _OrderBook_ 中的辅助数据结构 _Map<OrderID, Order> orderMap_，在 O(1) 时间内找到需要撤销的 _Order_。找到订单之后，如果“orders”是单向链表，代码必须遍历整个链表，定位前驱指针，才能删除订单，需要 O(n) 时间。现在使用双向链表，订单自身就有指向前一个订单的指针，因此可以直接删除，无需遍历整个订单列表。
    

图 14 解释了这三种操作的工作方式。

![图中展示了使用 LinkedHashMap 和双向链表实现买单簿订单系统的设计。标注为 `LinkedHashMap<Price, PriceLevel> limitMap` 的 LinkedHashMap 以价格档位为键，以对应的 `PriceLevel` 对象为值。每个 `PriceLevel` 看起来都是一个订单双向链表（`DoubleLinkedList<Order> orders`），以包含 `Order` 对象的方框表示，通过 `before` 和 `after` 指针说明其在链表中的位置。订单具有 `quantity` 属性。新买单（例如 `price = 100.07, quantity = 200`，标注 1）会添加到相应 `PriceLevel` 的链表中。已撮合的买单（例如 `price = 100.08, quantity = 500`，标注 2）会从其 `PriceLevel` 链表中移除。撤单（例如 `price = 100.06, quantity = 400`，标注 3）首先在独立的 `HashMap<OrderID, Order> orderMap` 中找到订单，再从相应 `PriceLevel` 的链表中删除。图中展示了下单和撤单后链表的 `before` 与 `after` 指针如何更新，说明数据结构的动态变化。`head` 和 `tail` 指针管理每个 `PriceLevel` 内的链表。](https://bytebytego.com/images/courses/system-design-interview/stock-exchange/figure-14-place-match-and-cancel-an-order-in-o1-GG4SP5FK.svg)

图 14 在 O(1) 时间内完成下单、撮合和撤单

更多细节请参见参考资料 \[14\]。

订单簿数据结构也广泛用于行情数据处理器，根据撮合引擎生成的成交回报流重建 L1、L2 和 L3 数据。

#### K 线图

除了订单簿，K 线图是行情数据处理器生成行情数据的另一种关键数据结构。

我们使用 Candlestick 类和 CandlestickChart 类对此建模。当前 K 线的时间区间结束后，为下一个区间实例化一个新的 Candlestick，并将其加入 CandleStickChart 实例中的链表。

```
class Candlestick {
    private long openPrice;
    private long closePrice;
    private long highPrice;
    private long lowPrice;
    private long volume;
    private long timestamp;
    private int interval;
}
class CandlestickChart {
    private LinkedList<Candlestick> sticks;
}
```

为多个证券代码、多个时间区间的 K 线图跟踪历史价格，会消耗大量内存。如何优化？可以采用以下两种方法：

1.  使用预先分配的环形缓冲区保存 K 线，减少新对象分配次数。
    
2.  限制内存中的 K 线数量，将其余数据持久化到磁盘。
    

我们将在深入设计的“行情数据发布器”一节讨论这些优化。

行情数据通常保存在内存列式数据库中（例如 KDB \[15\]），用于实时分析。收盘后，数据持久化到历史数据库。

## 第 3 步：深入设计

现在，我们已从整体上了解交易所的工作方式，下面研究现代交易所如何演变到今天的形态。现代交易所是什么样的？答案可能令许多读者惊讶：有些大型交易所几乎把所有功能都运行在一台超大型服务器上。虽然听起来很极端，但其中有许多值得借鉴的经验。

下面深入讨论。

### 性能

正如非功能需求中所述，延迟对交易所十分重要。不仅平均延迟要低，整体延迟还必须稳定。衡量稳定程度的一项良好指标是第 99 百分位延迟。

延迟可以分解为各组成部分，如下式所示：

```
Latency = ∑executionTimeAlongCriticalPath
```

降低延迟有两种方法：

1.  减少关键路径上的任务数量。
    
2.  缩短每项任务的耗时：
    
    1.  减少或消除网络和磁盘使用
        
    2.  减少每项任务的执行时间
        

先看第一点。如总体设计所示，关键交易路径包括：

```
gateway -> order manager -> sequencer -> matching engine
```

关键路径只保留必需组件；为实现低延迟，连日志记录都从关键路径中移除了。

再看第二点。在总体设计中，关键路径上的组件分别运行在通过网络连接的独立服务器上。网络往返延迟约为 500 微秒。关键路径上多个组件都通过网络通信时，总网络延迟会累积到几毫秒。此外，序列器是一个将事件持久化到磁盘的事件存储。即使假设采用高效设计，利用顺序写的性能优势，磁盘访问延迟仍达到几十毫秒。关于网络和磁盘访问延迟的更多信息，请参见《每位程序员都应该知道的延迟数字》\[16\]。

将网络和磁盘访问延迟都计入后，端到端总延迟达到几十毫秒。在交易所发展的早期，这样的数字已经相当可观；但在各家交易所竞相追求超低延迟的今天，它已无法满足要求。

为保持竞争优势，交易所逐步演进其设计，将关键路径的端到端延迟降到几十微秒，主要方式是探索减少或消除网络和磁盘访问延迟。一种久经验证的设计是将所有组件放在同一台服务器上，消除网络跳转。所有组件都在同一台服务器上时，就可以通过作为事件存储的 mmap \[17\] 通信（稍后详述）。

图 15 展示了所有组件运行在单台服务器上的低延迟设计：

![图中展示了交易系统的单服务器架构。三个主要组件分别为“订单管理器”“撮合引擎”和“行情数据发布器”，每个组件都包含“应用循环”，以一系列代表处理步骤的竖条表示。这三个组件与标注“mmap”的共享内存区域交互。“mmap”区域还由“报表生成器”“日志记录”“汇总风控检查”和“持仓管理器”四个组件访问。双向箭头表示应用循环与“mmap”区域之间的数据流。“mmap”作为中央数据存储，使三个主要组件能够共享和访问数据，另外四个组件则读写“mmap”，完成报表、日志、风险评估和持仓跟踪。整个系统被标注“单台服务器”的虚线框包围，表示所有组件都位于同一台服务器上。](https://bytebytego.com/images/courses/system-design-interview/stock-exchange/figure-15-a-low-latency-single-server-exchange-design-T5ZFOSZB.svg)

图 15 低延迟单服务器交易所设计

其中有几个有意思的设计决策值得进一步研究。

先关注上图中的应用循环。应用循环是一个有意思的概念：它在 while 循环中不断轮询待执行任务，是主要的任务执行机制。为满足严格的延迟预算，应用循环只应处理最关键的任务。其目标是缩短每个组件的执行时间，并确保执行时间高度可预测（即较低的第 99 百分位延迟）。图中的每个方框代表一个组件，一个组件对应服务器上的一个进程。为尽量提高 CPU 效率，每个应用循环（可以理解为主处理循环）都采用单线程，且线程绑定到固定的 CPU 核心。以订单管理器为例，其结构如图 16 所示。

![图中简化展示了订单管理器系统的架构。系统从“输入线程/网络循环”组件开始，以“订单”作为输入，再将订单“派发”给“应用循环线程”；该线程用一叠水平矩形表示多个任务。线程处理订单，并向“订单状态”组件发送“更新”信息，后者推测保存所有订单的当前状态。关键之处在于，应用循环线程“绑定到 CPU 1”，表示被分配到特定 CPU 核心，可能是为了优化性能。CPU 分配标记右侧的小矩阵可能表示 CPU 核心分配，其中“1”被突出显示，确认线程绑定情况。处理之后，“应用循环线程”将结果“派发”给“输出线程/网络循环”，后者再发送处理后的“订单”。整个流程包含在标注“订单管理器”的较大方框内，表示系统整体边界。应用循环线程周围的虚线暗示它是一组任务的逻辑集合，而非独立的物理组件。](https://bytebytego.com/images/courses/system-design-interview/stock-exchange/figure-16-application-loop-thread-in-order-manager-F76ALFG4.svg)

图 16 订单管理器中的应用循环线程

图中，订单管理器的应用循环绑定到 CPU 1。将应用循环绑定到 CPU，有以下显著好处：

1.  没有上下文切换 \[18\]。CPU 1 完全分配给订单管理器的应用循环。
    
2.  只有一个线程更新状态，因此无需锁，也就没有锁竞争。
    

这两点都有助于降低第 99 百分位延迟。

CPU 绑定的代价是编码更加复杂。工程师需要仔细分析每项任务的耗时，防止它过长时间占用应用循环线程，从而阻塞后续任务。

接下来，关注图 15 中央标注“mmap”的长矩形。“mmap”指名为 \`mmap(2)\` 的、符合 POSIX 标准的 UNIX 系统调用，它将文件映射到进程内存中。

\`mmap(2)\` 提供了进程间高性能共享内存的机制。如果底层文件位于 \`/dev/shm\`，性能优势会进一步提高。\`/dev/shm\` 是以内存为后端的文件系统。对 \`/dev/shm\` 中的文件执行 \`mmap(2)\` 后，访问共享内存完全不会产生磁盘访问。

现代交易所利用这一点，尽可能消除关键路径上的磁盘访问。在服务器中，使用 \`mmap(2)\` 实现消息总线，关键路径上的组件通过它通信。通信路径不涉及网络或磁盘访问，在 mmap 消息总线上发送一条消息的耗时不到一微秒。利用 mmap 构建事件存储，再结合接下来讨论的事件溯源设计范式，现代交易所可以在单台服务器内部构建低延迟微服务。

### 事件溯源

“数字钱包”一章讨论了事件溯源，有关其深入介绍，请参阅该章。

事件溯源的概念并不难理解。传统应用将状态持久化到数据库中。出现问题时，很难追溯问题源头。数据库只保存当前状态，没有记录导致当前状态的事件。

事件溯源保存的不是当前状态，而是所有引起状态变化的事件的不可变日志。这些事件是权威事实来源。图 17 展示了二者的对比。

![图中对比了非事件溯源与事件溯源两种数据管理方式。两个圆形节点代表订单状态：“订单 V1”标注“新建”，“订单 V2”标注“已成交”。一条标注 `OrderFilledEvent` 的箭头从“订单 V1”指向“订单 V2”，说明状态转换。下方标注“订单”的表格显示订单版本和状态（“V1”为“新建”，“V2”为“已成交”），表示只保存当前状态的非事件溯源方式。另一个标注“事件”的表格显示“事件序列”（100、101）和“事件类型”（`NewOrderEvent`、`OrderFilledEvent`），表示以事件序列跟踪状态变化的事件溯源方式。“订单”表下标注“非事件溯源”，“事件”表下标注“事件溯源”，明确区分两种方式。该图直观展示事件（`OrderFilledEvent`）如何触发从“新建”到“已成交”的状态变化，以及两种范式如何以不同方式记录这一变化。](https://bytebytego.com/images/courses/system-design-interview/stock-exchange/figure-17-non-event-sourcing-vs-event-sourcing-J5HGKUDM.svg)

图 17 非事件溯源与事件溯源的比较

左侧是经典数据库模式，跟踪订单的当前状态，却不包含订单如何到达该状态的信息。右侧是对应的事件溯源方式，跟踪所有改变订单状态的事件，并能按顺序重放全部事件来恢复订单状态。

图 18 展示了将 mmap 事件存储用作消息总线的事件溯源设计。这与 Kafka 的发布—订阅模型非常相似。实际上，如果没有严格的延迟要求，就可以使用 Kafka。

![图中展示了订单撮合引擎的系统设计。系统通过网关从外部域接收 FIX（金融信息交换）消息，并存入事件存储客户端。这些事件（具体为 `NewOrderEvent`）随后由撮合引擎内的事件存储客户端处理。撮合引擎包含订单管理器和撮合核心：订单管理器校验并更新订单状态，撮合核心通过从事件存储客户端拉取事件的应用循环，接收订单管理器传来的订单信息（“发送去撮合”）。`OrderFilledEvent` 也保存到事件存储（mmap，即内存映射）中。这一共享事件存储（mmap）同时向行情数据发布器和报表生成器提供数据。报表生成器属于另一个交易域（SBE），使用事件更新报表域（可自行选择）中的订单管理器。图中详细展示事件存储条目，包含序列、事件类型、SBE 编码的载荷，以及在存储中的位置。一条虚线将外部 FIX 域与内部 SBE 域分开。](https://bytebytego.com/images/courses/system-design-interview/stock-exchange/figure-18-an-event-sourcing-design-VUZUWA5X.svg)

图 18 事件溯源设计

图中，外部域使用“业务基础知识”一节介绍的 FIX 与交易域通信。

-   网关将 FIX 转换为“基于简单二进制编码（SBE）的 FIX”，实现快速、紧凑的编码，再通过事件存储客户端，以预定义格式将每笔订单作为 NewOrderEvent 发送（参见图中的事件存储条目）。
    
-   订单管理器（嵌入在撮合引擎中）从事件存储接收 NewOrderEvent，校验后将其加入内部订单状态，再将订单发送到撮合核心。
    
-   如果订单撮合成功，就生成 OrderFilledEvent，并发送到事件存储。
    
-   行情数据处理器、报表生成器等其他组件订阅事件存储，并相应处理这些事件。
    

这一设计基本遵循总体设计，但作出了一些调整，以便在事件溯源范式下更高效地运行。

第一个区别是订单管理器。订单管理器变成了嵌入不同组件的可复用库。这种设计很合理，因为订单状态对多个组件都很重要。如果使用集中式订单管理器，让其他组件更新或查询订单状态，就会损害延迟表现，尤其当这些组件不在关键交易路径上时，例如图中的报表生成器。虽然各个组件自行维护订单状态，但事件溯源能够保证这些状态一致且可重放。

另一个重要区别是，序列器已经不见踪影。它去哪儿了？

采用事件溯源设计后，所有消息共用一个事件存储。请注意，事件存储条目包含一个“sequence”字段，该字段由序列器注入。

每个事件存储只对应一个序列器。使用多个序列器并不是好做法，因为它们会争夺事件存储的写入权。在交易所这样的繁忙系统中，锁竞争会浪费大量时间。因此，序列器是单一写入者，在将事件发送到事件存储前为其排序。与总体设计中同时充当消息存储的序列器不同，这里的序列器只做一件简单的事，速度极快。图 19 展示了内存映射（MMap）环境中的序列器设计。

序列器从每个组件的本地环形缓冲区拉取事件，为每个事件添加序列 ID，并发送到事件存储。可以设置备用序列器，在主序列器宕机时保障高可用性。

![图中展示了事件处理流水线中的数据流架构。顶部“网关”组件通过编号箭头（1）连接到一个“环形缓冲区”，作为传入事件的临时存储。另一个“环形缓冲区”连接“撮合引擎”，暗示事件的并行处理。编号箭头（2）表示两个环形缓冲区都向“序列器”提供数据。最后，箭头（3）表示序列器将处理后的事件写入“事件存储（mmap）”，后者以一系列竖条表示内存映射文件，支持高效存取。整个流程展示事件经网关进入、缓冲、由撮合引擎处理、排序，并最终持久保存在事件存储中的过程。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fstock-exchange%2Ffigure-19-sample-design-of-sequencer-XIKDOBN7.png&w=3840&q=75)

图 19 序列器设计示例

### 高可用性

我们的高可用性设计目标是四个九（99.99%），这意味着交易所每天最多只能停机 8.64 秒。因此，服务宕机后几乎必须立即恢复。

为实现高可用性，需要考虑以下几点：

-   首先，识别交易所架构中的单点故障。例如，撮合引擎故障对交易所可能是灾难。因此，需要在主实例之外设置冗余实例。
    
-   其次，应快速检测故障，并作出故障转移到备用实例的决定。
    

对于客户端网关等无状态服务，可以通过增加服务器轻松实现水平扩展。对于订单管理器和撮合引擎等有状态组件，需要能够在副本之间复制状态数据。

图 20 展示了复制数据的一个例子。热态撮合引擎作为主实例，温备引擎接收并处理完全相同的事件，但不向事件存储输出任何事件。主实例宕机时，温备实例可以立即接管主实例职责并开始输出事件。温备从实例宕机后，重启时总能从事件存储恢复所有状态。事件溯源非常适合交易所架构，其固有的确定性使状态恢复既简单又准确。

![图中展示了两级撮合引擎的系统架构，包含“热态”撮合引擎（白色矩形）和“温备”撮合引擎（灰色阴影矩形）。两个引擎都从共享“事件存储”接收 `NewOrderEvent` 数据；事件存储以标注“事件存储（mmap）”的长矩形表示，使用内存映射（`mmap`）实现。“热态”引擎处理 `NewOrderEvent` 数据，随后将 `OrderFilledEvent` 写回“事件存储”。“热态”引擎与“事件存储”之间的弯曲箭头表示循环流程。“温备”引擎只从“事件存储”接收 `NewOrderEvent` 数据，用一条向上箭头表示。该图强调“事件存储”作为订单事件中央仓库的数据流，同时为“热态”（推测用于实时处理）和“温备”（可能用于备份或时间要求较低的操作）撮合引擎提供事件。](https://bytebytego.com/images/courses/system-design-interview/stock-exchange/figure-20-hot-warm-matching-engine-QHSZ5QAN.svg)

图 20 热态—温备撮合引擎

需要设计一种机制，检测主实例中的潜在问题。除了常规的硬件和进程监控，还可以让撮合引擎发送心跳。如果未及时收到心跳，撮合引擎可能出现了问题。

这种热态—温备设计的问题是，只能在单台服务器范围内工作。为实现高可用性，必须将这一概念扩展到多台机器，甚至多个数据中心。在这种部署中，整台服务器都处于热态或温备状态，整个事件存储从热态服务器复制到所有温备副本。跨机器复制整个事件存储需要时间。可以使用可靠 UDP \[19\]，将事件消息高效广播到所有温备服务器。Aeron \[20\] 的设计就是一个例子。

下一节讨论如何改进热态—温备设计，实现高可用性。

### 容错

上述热态—温备设计相对简单，工作效果尚可，但如果温备实例也宕机了呢？这种事件概率低，却具有灾难性后果，因此应提前准备。

大型科技公司也面临这一问题，它们通过将核心数据复制到多个城市的数据中心来应对，降低地震、大规模停电等自然灾害的风险。为使系统具备容错能力，必须回答许多问题：

1.  主实例宕机后，如何决定以及何时决定故障转移到备用实例？
    
2.  如何在备用实例中选举领导者？
    
3.  需要多长的恢复时间（RTO——恢复时间目标）？
    
4.  需要恢复哪些功能（RPO——恢复点目标）？系统能否在降级状态下运行？
    

下面逐一回答。

首先，必须理解“宕机”的确切含义。这并不像看起来那么简单。请考虑以下情况。

1.  系统可能发出误报，导致不必要的故障转移。
    
2.  代码缺陷可能导致主实例宕机。故障转移后，相同缺陷也可能使备用实例宕机。当所有备用实例都被该缺陷击倒时，系统就不可用了。
    

这些问题很难解决，可以考虑以下建议。首次发布新系统时，可能需要手动执行故障转移。只有收集了足够多的信号和运维经验，对系统建立更强信心后，才将故障检测过程自动化。混沌工程 \[21\] 是一种良好实践，能更快暴露边界情况、积累运维经验。

在正确作出故障转移决定后，如何决定由哪台服务器接管？幸运的是，这个问题已有成熟认识，也有许多久经考验的领导者选举算法。这里以 Raft \[22\] 为例。

图 21 展示了一个由五台服务器组成的 Raft 集群，每台服务器都有自己的事件存储。当前领导者将数据发送给所有其他实例（跟随者）。在 Raft 中执行操作所需的最少票数为 (N/2 + 1)，其中 N 是集群成员数量。在这个例子中，最少需要 3 票。

下图（图 21）展示跟随者通过 RPC 从领导者接收新事件。这些事件保存到跟随者自己的 mmap 事件存储中。

![图中展示了订单撮合引擎的系统架构。顶部有两个撮合引擎：“撮合引擎（热态）”和以灰色显示的“撮合引擎（温备）”。“热态”引擎接收 `NewOrderEvent` 消息，并发送 `OrderFilledEvent` 消息。两个引擎都与采用内存映射文件（mmap）实现的“事件存储”交互。事件存储由五个标注“事件存储（mmap）”的水平矩形框表示，每个矩形代表存储的一个区段。事件存储下方显示“AppendEntries RPC”，表示使用多个远程过程调用追加数据。箭头展示信息流：“NewOrderEvent”从“热态”引擎和“温备”引擎流向事件存储，“OrderFilledEvent”从“热态”引擎流向事件存储。多条弯曲箭头将“AppendEntries RPC”连接到最上方的事件存储区段，暗示这些 RPC 向事件存储写入数据。最上方区段左侧为浅紫色，其他四个区段左侧为浅青色，可能表示不同数据分区或处理阶段。](https://bytebytego.com/images/courses/system-design-interview/stock-exchange/figure-21-event-replication-in-raft-cluster-PVVZDWHT.svg)

图 21 Raft 集群中的事件复制

下面简要介绍领导者选举过程。领导者向跟随者发送心跳消息（图 21 中没有内容的 AppendEnties）。如果跟随者一段时间没有收到心跳，就会触发选举超时，发起新一轮选举。第一个达到选举超时的跟随者成为候选者，并请求其余跟随者投票（RequestVote）。如果该跟随者获得多数票，就成为新的领导者。如果它的任期值低于新节点，就不能成为领导者。如果多个跟随者同时成为候选者，就会发生“分票”。这种情况下，选举超时后会发起新一轮选举。“任期”的解释参见图 22。Raft 将时间划分为任意长度的区间，用来表示正常运行和选举过程。

![图中展示了一条时间轴，包含从左到右依次排列的“任期 1”到“任期 5”五个任期，表示时间流逝。每个任期以矩形表示，左右两端与相邻任期连接，表示时间连续推进。每个任期矩形起始处有一个较小的蓝绿色矩形，表示选举阶段（“选举”）。这些蓝绿色矩形出现在任期 1、任期 2 和任期 4 的开始，说明这些任期以选举开始。任期 3 没有蓝绿色矩形，表示开始时没有选举。一个更大的蓝绿色矩形占据任期 4 的全部，标注“分票”，暗示该任期发生重要事件或变化。箭头从“选举”“正常运行”和“分票”分别指向对应任期，展示这些事件对各任期的影响。最后，一条标注“时间”的箭头指向右侧，表示五个任期中的时间推进。](https://bytebytego.com/images/courses/system-design-interview/stock-exchange/figure-22-raft-terms-PGF2N5J6.svg)

图 22 Raft 任期（来源：\[23\]）

接下来看恢复时间。恢复时间目标（RTO）指应用可以停机而不对业务造成重大损害的最长时间。对于证券交易所，需要达到秒级 RTO，这无疑要求服务自动故障转移。为此，根据优先级对服务分类，并制定降级策略，以维持最低服务水平。

最后，需要明确对数据丢失的容忍程度。恢复点目标（RPO）指在对业务造成重大损害之前可以丢失的数据量，也就是可容忍的丢失量。在实践中，这意味着频繁备份数据。对于证券交易所，数据丢失不可接受，因此 RPO 接近零。使用 Raft 后，数据有多个副本，能够保证集群节点就状态达成一致。当前领导者崩溃时，新领导者应能立即投入运行。

### 撮合算法

下面稍作延伸，深入讨论撮合算法。下方伪代码从整体上解释撮合的工作方式。

```
Context handleOrder(OrderBook orderBook, OrderEvent orderEvent) {
    if (orderEvent.getSequenceId() != nextSequence) {
        return Error(OUT_OF_ORDER, nextSequence);
    }

    if (!validateOrder(symbol, price, quantity)) {
        return ERROR(INVALID_ORDER, orderEvent);
    }

    Order order = createOrderFromEvent(orderEvent);
    switch (msgType):
        case NEW:
            return handleNew(orderBook, order);
        case CANCEL:
            return handleCancel(orderBook, order);
        default:
            return ERROR(INVALID_MSG_TYPE, msgType);

}

Context handleNew(OrderBook orderBook, Order order) {
    if (BUY.equals(order.side)) {
        return match(orderBook.sellBook, order);
    } else {
        return match(orderBook.buyBook, order);
    }
}

Context handleCancel(OrderBook orderBook, Order order) {
    if (!orderBook.orderMap.contains(order.orderId)) {
        return ERROR(CANNOT_CANCEL_ALREADY_MATCHED, order);
    }
    removeOrder(order);
    setOrderStatus(order, CANCELED);
    return SUCCESS(CANCEL_SUCCESS, order);
}

Context match(OrderBook book, Order order) {
    Quantity leavesQuantity = order.quantity - order.matchedQuantity;
    Iterator<Order> limitIter = book.limitMap.get(order.price).orders;
    while (limitIter.hasNext() && leavesQuantity > 0) {
        Quantity matched = min(limitIter.next.quantity, order.quantity);
        order.matchedQuantity += matched;
        leavesQuantity = order.quantity - order.matchedQuantity;
        remove(limitIter.next);
        generateMatchedFill();
    }
    return SUCCESS(MATCH_SUCCESS, order);
}
```

上述伪代码使用 FIFO（先进先出）撮合算法。在同一价格档位，先到的订单先撮合，最后到的订单最后撮合。

撮合算法有很多种，广泛用于期货交易。例如，带有 LMM（首席做市商）的 FIFO 算法，会按照预定义比例，在 FIFO 队列之前先为 LMM 分配一定数量，LMM 公司通过与交易所协商获得这一优先权。更多撮合算法可参见 CME 网站 \[24\]。撮合算法也用于许多其他场景，典型例子是暗池 \[25\]。

### 确定性

确定性包括功能确定性和延迟确定性。前面已经介绍功能确定性。序列器、事件溯源等设计选择，保证事件按相同顺序重放时，结果也相同。

对于功能确定性，大多数情况下，事件实际发生的时间并不重要，重要的是事件顺序。在图 23 中，事件时间戳从时间维度上间隔不均的离散点，转换为连续排列的点，从而可以大幅缩短重放/恢复耗时。

![图中有两条时间轴，展示不同的事件排列方式。上方时间轴包含六个事件，标注“事件 1”至“事件 6”，用线条连接的圆表示，沿右端标注“时间”的时间轴从左向右推进。各事件依次连接到下一个事件，表示特定的发生顺序。下方时间轴同样沿另一条标注“时间”的时间轴从左向右推进，包含六个未命名事件，也以线条连接的圆表示。主要区别在于，下方时间轴在与上方相同的整体时间跨度内显示了更高的事件频率，暗示事件发生的粒度或速率不同。两条时间轴都使用简单的线性表示，直观展示事件随时间推进的过程。](https://bytebytego.com/images/courses/system-design-interview/stock-exchange/figure-23-time-in-event-sourcing-ZUOQYUVH.svg)

图 23 事件溯源中的时间

延迟确定性意味着每笔交易经过系统时，延迟几乎相同。这对业务至关重要。可以用数学方式衡量：第 99 百分位延迟，甚至更严格的第 99.99 百分位延迟。可以使用 HdrHistogram \[26\] 计算延迟。如果第 99 百分位延迟较低，说明交易所在几乎所有交易中都能提供稳定性能。

应当调查大幅的延迟波动。例如，在 Java 中，安全点经常是原因。HotSpot JVM \[27\] 中的 Stop-the-World（全局暂停）垃圾回收就是一个著名例子。

至此，我们完成了对关键交易路径的深入讨论。本章剩余部分将仔细研究交易所其他部分中一些更有意思的问题。

### 行情数据发布器优化

从撮合算法可以看出，L3 订单簿数据能让我们更全面地了解市场。可以从 Google Finance 免费获取日 K 线数据，但更详细的 L2/L3 订单簿数据价格昂贵。许多对冲基金通过交易所的实时 API 自行记录数据，构建自己的 K 线图及其他技术分析图表。

行情数据发布器（MDP）从撮合引擎接收撮合结果，据此重建订单簿和 K 线图，再将数据发布给订阅者。

重建订单簿的过程类似于上面“撮合算法”一节的伪代码。MDP 是一种具有多个服务等级的服务。例如，散户默认只能查看 L2 数据中的 5 档，需要额外付费才能查看 10 档。MDP 的内存不可能无限扩展，因此必须限制 K 线数量。关于 K 线图的介绍，可以回顾“数据模型”一节。MDP 的设计如图 24 所示。

![图中简化展示了一个可能用于金融交易的系统架构。左侧“撮合引擎”组件处理订单，将“订单、撮合结果”发送给中央“MDP”（行情数据提供器）组件。MDP 内有三个“订单簿”实例，暗示系统并行处理多个订单簿。MDP 将数据提供给“K 线图”组件，后者生成 1 分钟、1 小时和 1 天三种时间粒度的图表。这些图表在“K 线图”方框内以圆形表示。虚线箭头表示外部系统可能访问日 K 线数据。“K 线图”组件向“持久化”组件输出数据，后者可能是保存图表数据的数据库。最后，底部“数据服务”组件暗示存在一个访问层，用于访问并可能分发持久化数据。整个流程从订单撮合出发，经行情数据处理和图表生成，到达数据持久化和服务访问。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fstock-exchange%2Ffigure-24-market-data-publisher-UJ7LL7ZE.png&w=3840&q=75)

图 24 行情数据发布器

这一设计使用环形缓冲区。环形缓冲区也称循环缓冲区，是首尾相接的定长队列。生产者持续生成数据，一个或多个消费者从中拉取数据。环形缓冲区的空间预先分配，因此无需创建或释放对象。这种数据结构还是无锁的。还有其他技术可以进一步提高其效率。例如，填充可以确保环形缓冲区的序列号永远不会与其他数据处于同一缓存行。更多细节请参见 \[28\]。

### 行情数据分发的公平性

在股票交易中，延迟比其他人更低，就像拥有预知未来的神谕。对于受监管的交易所，保证所有行情数据接收方同时获得数据十分重要。为什么？例如，MDP 维护了一份数据订阅者列表，订阅者的排列顺序由连接到发布器的先后顺序决定，排在第一位的总是最先收到数据。猜猜接下来会发生什么？精明的客户会在开盘时争抢列表中的第一位。

有几种方法可以缓解这一问题。使用可靠 UDP 的多播，是同时向大量参与者广播更新的良好方案。MDP 也可以在订阅者连接时，为其随机分配顺序。下面详细介绍多播。

### 多播

数据可以通过三种不同类型的协议在互联网上传输。下面简要介绍。

1.  单播：从一个源发送到一个目的地。
    
2.  广播：从一个源发送到整个子网络。
    
3.  多播：从一个源发送到一组主机，这些主机可以位于不同子网络。
    

多播是交易所设计中常用的协议。将多个接收方配置到同一个多播组后，理论上它们会同时收到数据。不过，UDP 是不可靠协议，数据报可能无法到达所有接收方。已有方案可以处理重传 \[29\]。

### 主机托管

说到公平性，事实上，许多交易所都会提供主机托管服务，将对冲基金或经纪商的服务器放在与交易所相同的数据中心。向撮合引擎下单的延迟基本与线缆长度成正比。主机托管并不违背公平性理念，可以视为付费 VIP 服务。

### 网络安全

交易所通常会提供一些公共接口，DDoS 攻击是一个现实挑战。以下是一些抵御 DDoS 的技术：

1.  将公共服务及其数据与私有服务隔离，使 DDoS 攻击不会影响最重要的客户。如果提供相同数据，可以设置多个只读副本，以隔离问题。
    
2.  使用缓存层保存不经常更新的数据。缓存设计得当时，大多数查询都不会访问数据库。
    
3.  加固 URL，抵御 DDoS 攻击。例如，对于 `https://my.website.com/data?from=123&to=456` 这样的 URL，攻击者只需修改查询字符串，就能轻松生成大量不同请求。采用 `https://my.website.com/data/recent` 这样的 URL 效果更好，而且还能在 CDN 层缓存。
    
4.  需要有效的允许名单/阻止名单机制，许多网络网关产品都提供这类功能。
    
5.  限流也经常用于防御 DDoS 攻击。
    

## 总结

读完本章，你可能会得出一个结论：大型交易所的理想部署模型，是将所有组件放在一台超大型服务器上，甚至放在单个进程中。确实，有些交易所正是这样设计的！

随着近年来加密货币行业的发展，许多加密货币交易所使用云基础设施部署服务 \[30\]。有些去中心化金融项目基于 AMM（自动做市）的理念，甚至不需要订单簿。

云生态系统提供的便利改变了部分设计，降低了进入行业的门槛。这无疑将为金融世界注入创新活力。

恭喜你读到这里！现在可以拍拍自己的肩膀，鼓励一下自己。做得很好！

## 本章小结

![图中以分层结构展示证券交易所系统的设计过程。中心节点标注“证券交易所”，分出四个主要步骤：第 1 步（“估算”和“总体设计”）、第 2 步（“API 设计”和“数据模型”）、第 3 步（关注系统性能、容错、撮合算法、数据发布优化、公平性、多播、主机托管和网络安全）以及第 4 步（“总结”）。第 1 步从“非功能需求”开始，列出“可用性：99.99%”“容错”“毫秒级延迟”和“安全”，然后给出“100 个证券代码”和“峰值 QPS 为 21.5 万”的估算。总体设计包括“交易流程”“行情数据流程”和“报表流程”。第 2 步详细介绍 API 设计，涵盖“订单”“成交回报”“订单簿”和“历史价格”；数据模型包括“产品、订单、成交回报”“订单簿”和“K 线图”。第 3 步深入探讨系统实现的各个方面，包括高可用性、容错、具体算法和优化。最后，第 4 步代表设计过程的总结阶段。每个步骤向外分支，展示设计证券交易所系统涉及的具体考虑因素和组件。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fstock-exchange%2Fchapter-summary-KL4KCPJZ.png&w=3840&q=75)

## 参考资料

\[1\] LMAX 交易所以其开源 Disruptor 闻名： [<u>https://www.lmax.com/exchange</u>](https://www.lmax.com/exchange)

\[2\] IEX 以“公平交易”吸引投资者，也被称为“闪电男孩交易所”：

[<u>https://en.wikipedia.org/wiki/IEX</u>](https://en.wikipedia.org/wiki/IEX)

\[3\] 纽约证券交易所撮合成交量： [<u>https://www.nyse.com/markets/us-equity-volumes</u>](https://www.nyse.com/markets/us-equity-volumes)

\[4\] 香港交易所每日交易量：

[<u>https://www.hkex.com.hk/Market-Data/Statistics/Consolidated-Reports/Securities-Statistics-Archive/Trading_Value_Volume_And_Number_Of_Deals?sc_lang=en#select1=0</u>](https://www.hkex.com.hk/Market-Data/Statistics/Consolidated-Reports/Securities-Statistics-Archive/Trading_Value_Volume_And_Number_Of_Deals?sc_lang=en#select1=0)

\[5\] 按规模排列的全球证券交易所：

[<u>http://money.visualcapitalist.com/all-of-the-worlds-stock-exchanges-by-size/</u>](http://money.visualcapitalist.com/all-of-the-worlds-stock-exchanges-by-size/)

\[6\] 拒绝服务攻击： [<u>https://en.wikipedia.org/wiki/Denial-of-service_attack</u>](https://en.wikipedia.org/wiki/Denial-of-service_attack)

\[7\] 市场冲击： [<u>https://en.wikipedia.org/wiki/Market_impact</u>](https://en.wikipedia.org/wiki/Market_impact)

\[8\] FIX 交易： [<u>https://www.fixtrading.org/</u>](https://www.fixtrading.org/)

\[9\] 事件溯源： [<u>https://martinfowler.com/eaaDev/EventSourcing.html</u>](https://martinfowler.com/eaaDev/EventSourcing.html)

\[10\] CME 主机托管和数据中心服务：

[<u>https://www.cmegroup.com/trading/colocation/co-location-services.html</u>](https://www.cmegroup.com/trading/colocation/co-location-services.html)

\[11\] 纪元时间： [<u>https://www.epoch101.com/</u>](https://www.epoch101.com/)

\[12\] 订单簿： [<u>https://www.investopedia.com/terms/o/order-book.asp</u>](https://www.investopedia.com/terms/o/order-book.asp)

\[13\] 订单簿： [<u>https://en.wikipedia.org/wiki/Order_book</u>](https://en.wikipedia.org/wiki/Order_book)

\[14\] 如何构建快速限价订单簿： [<u>https://bit.ly/3ngMtEO</u>](https://bit.ly/3ngMtEO)

\[15\] 使用 kdb+ 和 q 语言开发： [<u>https://code.kx.com/q/</u>](https://code.kx.com/q/)

\[16\] 每位程序员都应该知道的延迟数字： [<u>https://gist.github.com/jboner/2841832</u>](https://gist.github.com/jboner/2841832)

\[17\] mmap: [<u>https://en.wikipedia.org/wiki/Memory_map</u>](https://en.wikipedia.org/wiki/Mmap)

\[18\] 上下文切换： [<u>https://bit.ly/3pva7A6</u>](https://bit.ly/3pva7A6)

\[19\] 可靠用户数据报协议： [<u>https://en.wikipedia.org/wiki/Reliable_User_Datagram_Protocol</u>](https://en.wikipedia.org/wiki/Reliable_User_Datagram_Protocol)

\[20\] Aeron: [<u>https://github.com/real-logic/aeron/wiki/Design-Overview</u>](https://github.com/real-logic/aeron/wiki/Design-Overview)

\[21\] 混沌工程： [<u>https://en.wikipedia.org/wiki/Chaos_engineering</u>](https://en.wikipedia.org/wiki/Chaos_engineering)

\[22\] Raft: [<u>https://raft.github.io/</u>](https://raft.github.io/)

\[23\] 为易理解性而设计：Raft 共识算法： [<u>https://raft.github.io/slides/uiuc2016.pdf</u>](https://raft.github.io/slides/uiuc2016.pdf)

\[24\] 支持的撮合算法： [<u>https://bit.ly/3aYoCEo</u>](https://bit.ly/3aYoCEo)

\[25\] 暗池： [<u>https://www.investopedia.com/terms/d/dark-pool.asp</u>](https://www.investopedia.com/terms/d/dark-pool.asp)

\[26\] HdrHistogram：高动态范围直方图： [<u>http://hdrhistogram.org/</u>](http://hdrhistogram.org/)

\[27\] HotSpot（虚拟机）： [<u>https://en.wikipedia.org/wiki/HotSpot_(virtual_machine)</u>](https://en.wikipedia.org/wiki/HotSpot_(virtual_machine))

\[28\] 缓存行填充： [<u>https://bit.ly/3lZTFWz</u>](https://bit.ly/3lZTFWz)

\[29\] 基于 NACK 的可靠多播： [<u>https://en.wikipedia.org/wiki/NACK-Oriented_Reliable_Multicast</u>](https://en.wikipedia.org/wiki/NACK-Oriented_Reliable_Multicast)

\[30\] AWS Coinbase 案例研究： [<u>https://aws.amazon.com/solutions/case-studies/coinbase/</u>](https://aws.amazon.com/solutions/case-studies/coinbase/)

****

