# 23 酒店预订系统

本章将为万豪国际这样的连锁酒店设计预订系统。本章的设计思路和技术也适用于其他常见的预订类面试题：

-   设计 Airbnb。
    
-   设计机票预订系统。
    
-   设计电影票预订系统。
    

## 第1步：理解问题，明确设计范围

酒店预订系统比较复杂，其组件会随业务场景而变化。在深入设计之前，应通过向面试官提问来澄清需求，缩小设计范围。

**候选人**：系统规模有多大？

**面试官**：假设我们为一家拥有5000家酒店、共100万间客房的连锁酒店建设网站。

**候选人**：客户是在预订时付款，还是到店时付款？

**面试官**：为简化问题，假设客户在预订时全额付款。

**候选人**：客户只能通过酒店网站预订吗？是否需要支持电话等其他预订渠道？

**面试官**：假设用户通过酒店网站或应用预订客房。

**候选人**：客户可以取消预订吗？

**面试官**：可以。

**候选人**：还有其他需要考虑的问题吗？

**面试官**：有，我们允许10%的超额预订。超额预订是指酒店售出的客房数量超过实际拥有的客房数。酒店之所以这样做，是因为预计部分客户会取消预订。

**候选人**：考虑到时间有限，我假设客房搜索不在讨论范围内，重点实现以下功能。

-   展示酒店相关页面。
    
-   展示客房详情页面。
    
-   预订客房。
    
-   提供管理后台，以新增、删除、更新酒店或客房信息。
    
-   支持超额预订。
    

**面试官**：可以。

**面试官：**还有一点，酒店价格会动态变化。客房价格取决于对某一天入住率的预期。本次面试中，可以假设每天的价格都可能不同。

**候选人**：我会将这一点纳入考虑。

接下来，可以讨论最重要的非功能需求。

### 非功能需求

-   支持高并发。在旺季或大型活动期间，热门酒店可能有很多客户同时尝试预订同一间客房。
    
-   适中的延迟。用户预订时，最好能够快速响应，但处理一次预订请求耗时几秒也是可以接受的。
    

### 粗略估算

-   5000家酒店，共100万间客房。
    
-   假设客房入住率为70%，平均入住时长为3天。
    
-   估算每天的预订量：(100万 \* 0.7) / 3 = 233,333，向上粗略取为约240,000。
    
-   每秒预订量 = 240,000 / 每天约10^5秒 ≈ 3。可见，平均每秒预订事务数（TPS）并不高。
    

接下来粗略估算系统中各类页面的 QPS。典型客户流程包含三个步骤：

1.  查看酒店或客房详情页。用户浏览此页面，属于查询操作。
    
2.  查看预订页面。用户在提交预订前确认日期、入住人数、付款信息等细节，属于查询操作。
    
3.  预订客房。用户点击“预订”按钮，完成客房预订，属于事务操作。
    

假设每个环节约有10%的用户进入下一步，90%的用户在最终步骤之前流失；同时假设没有实现预取功能，即不会在用户进入下一步前提前加载内容。图1粗略估算了各步骤的 QPS。已知最终预订 TPS 为3，沿漏斗反向推算，可得订单确认页 QPS 为30，详情页 QPS 为300。

![在线酒店预订系统的用户流量漏斗图。倒三角包含三个水平层，表示预订过程的三个阶段。最上层最宽，标注“查看酒店或客房详情，QPS=300”，表示每秒300次查询用于浏览详情。第二层较窄，标为“订单预订页面，QPS=30”，流量降至每秒30次。最下层最窄，标为“预订客房，QPS=3”，表示只有每秒3次请求到达最终预订阶段。逐层下降的 QPS 展示典型漏斗效应：大量初始浏览最终只转化为较少的实际预订。](https://bytebytego.com/images/courses/system-design-interview/hotel-reservation-system/figure-1-qps-distribution-4LPSPNX4.svg)

图1　QPS 分布

## 第2步：提出总体设计，达成共识

本节将讨论：

-   API 设计。
    
-   数据模型。
    
-   总体设计。
    

### API 设计

下面按 RESTful 约定列出酒店预订系统最重要的 API。

请注意，本章重点是酒店预订系统。完整的酒店网站还需要直观易用的搜索功能，让客户按多种条件查找客房。这类搜索 API 虽然重要，但技术挑战不大，不在本章范围内。

**酒店相关 API**

| **API**              | **说明**                                                   |
| -------------------- | ------------------------------------------------------------ |
| GET /v1/hotels/ID    | 获取酒店详情。                      |
| POST /v1/hotels      | 新增酒店，仅供酒店工作人员使用。  |
| PUT /v1/hotels/ID    | 更新酒店信息，仅供酒店工作人员使用。 |
| DELETE /v1/hotels/ID | 删除酒店，仅供酒店工作人员使用。   |

表1　酒店相关 API

**客房相关 API**

| **API**                       | **说明**                                                   |
| ----------------------------- | ------------------------------------------------------------ |
| GET /v1/hotels/ID/rooms/ID    | 获取客房详情。                       |
| POST /v1/hotels/ID/rooms      | 新增客房，仅供酒店工作人员使用。       |
| PUT /v1/hotels/ID/rooms/ID    | 更新客房信息，仅供酒店工作人员使用。 |
| DELETE /v1/hotels/ID/rooms/ID | 删除客房，仅供酒店工作人员使用。    |

表2　客房相关 API

**预订相关 API**

| **API**                    | **说明**                                         |
| -------------------------- | -------------------------------------------------- |
| GET /v1/reservations       | 获取当前登录用户的预订历史。 |
| GET /v1/reservations/ID    | 获取某次预订的详情。      |
| POST /v1/reservations      | 创建预订。                            |
| DELETE /v1/reservations/ID | 取消预订。                              |

表3　预订相关 API

创建预订是一项非常重要的功能。创建预订接口 POST /v1/reservations 的请求参数可以如下所示。

```
{
  "startDate":"2021-04-28",
  "endDate":"2021-04-30",
  "hotelID":"245",
  "roomID":"U12354673389",
  "reservationID":"13422445"
}
```

注意，_reservationID_ 用作幂等键，以防止重复预订。这里的重复预订是指同一天的同一间客房被多次预订。详细内容见“深入设计”中的“并发问题”一节。

### 数据模型

在决定使用哪种数据库之前，先仔细分析数据访问模式。酒店预订系统需要支持以下查询：

查询1：查看酒店详情。

查询2：根据日期范围查找可预订的房型。

查询3：记录一笔预订。

查询4：查询某笔预订或历史预订记录。

根据粗略估算，系统规模并不大，但需要应对大型活动期间的流量激增。考虑到这些需求，我们选择关系型数据库，原因如下：

-   关系型数据库适合读密集、写入较少的工作负载。浏览酒店网站或应用的用户数量，比实际预订的用户数量高几个数量级。NoSQL 数据库通常针对写入进行了优化，而关系型数据库已经足以满足这种读密集场景。
    
-   关系型数据库提供 ACID 保证，即原子性、一致性、隔离性和持久性。这些特性对预订系统非常重要。缺少它们，就不容易避免余额为负、重复扣款、重复预订等问题。ACID 能显著简化应用代码，也使整个系统更容易分析和理解。关系型数据库通常能提供这些保证。
    
-   关系型数据库易于为这些数据建模。业务数据结构清晰，酒店、客房、房型等实体之间的关系也比较稳定，因此很适合采用关系模型。
    

既然已经选择关系型数据库作为存储，接下来讨论表结构设计。图2展示了一种直观方案，也是许多候选人为酒店预订系统建模时最自然的做法。

![酒店预订系统的数据库表结构，以实体关系图展示四个服务，每个服务各有一张表。酒店服务的 hotel 表包含主键 hotel_id，以及 name、address、location。房价服务的 room_type_rate 表包含作为主键的 hotel_id、date，以及 rate。宾客服务的 guest 表包含主键 guest_id，以及 first_name、last_name、email。预订服务的 reservation 表包含主键 reservation_id，以及 hotel_id、room_id、start_date、end_date、status、guest_id。图中没有显式画出表之间的连线，但共享的 hotel_id、room_id、guest_id 等属性暗示了外键关系，实际数据库实现需要明确定义这些关系。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fhotel-reservation-system%2Ffigure-2-database-schema-3WL3FJJ5.png&w=3840&q=75)

图2　数据库表结构

大多数属性从名称就能理解，这里只解释 reservation 表中的 _status_ 字段。它可取以下状态：待处理 pending、已付款 paid、已退款 refunded、已取消 canceled、已拒绝 rejected。状态机如图3所示。

![付款生命周期的状态图。中央 Pending（待处理）为初始状态，可转向 Canceled（已取消）、Paid（已付款）、Rejected（已拒绝）三个状态。已取消表示付款在处理前终止，已付款表示付款成功；已付款之后还可以转到 Refunded（已退款），表示后续发生退款。箭头标示允许的状态转换方向，除方框中的状态名之外，没有其他 URL、参数或文字信息。](https://bytebytego.com/images/courses/system-design-interview/hotel-reservation-system/figure-3-reservation-status-J57N4ZZV.svg)

图3　预订状态

这种表结构有一个严重问题。对于 Airbnb 一类公司，这种数据模型是可行的，因为用户预订时已经指定了 _room\_id_，也可能称为 _listing\_id_。但酒店并非如此，用户预订的是某家酒店的**一种房型**，而不是具体某间客房。例如，房型可能是标准间、特大床房、配有两张 queen 床的双大床房等。具体房号是在入住时分配的，而不是预订时分配。因此，需要更新数据模型来体现这一需求，详见“深入设计”中的“改进数据模型”一节。

### 总体设计

本酒店预订系统采用微服务架构。过去几年，微服务架构越来越流行，Amazon、Netflix、Uber、Airbnb、Twitter 等公司都在使用它。如果想进一步了解微服务架构的优势，可以阅读资料 \[1\] \[2\]。

本设计按微服务架构建模，总体设计如图4所示。

![酒店预订系统架构。水平线将外部区域与内部区域分开。外部用户的移动设备通过内容分发网络 CDN 访问公共 API 网关，由网关将请求分发给酒店服务、房价服务、预订服务和支付服务。这四个服务分别使用自己的酒店数据库、房价数据库、预订数据库和支付数据库。酒店服务还使用酒店缓存来提高性能。在内部私有区域，管理员访问内部 API，再由其连接酒店管理服务。箭头表示组件间的信息和请求流向。系统既处理用户查询酒店信息、房价、预订、付款的请求，也提供酒店数据管理界面。](https://bytebytego.com/images/courses/system-design-interview/hotel-reservation-system/figure-4-high-level-design-4TSKHYJP.svg)

图4　总体设计

下面从上到下简要介绍各组件。

-   用户：通过手机或计算机预订酒店客房。
    
-   管理员，即酒店工作人员：获授权的工作人员执行管理操作，例如为客户退款、取消预订、更新客房信息等。
    
-   CDN，即内容分发网络：为提高加载速度，使用 CDN 缓存所有静态资源，包括 JavaScript 打包文件、图片、视频、HTML 等。
    
-   公共 API 网关：全托管服务，支持限流、身份验证等功能。网关依据端点将请求路由到对应服务。例如，加载酒店主页的请求送到酒店服务，预订客房的请求送到预订服务。
    
-   内部 API：仅供获授权的酒店工作人员使用，通过内部软件或网站访问，通常还会使用虚拟专用网络 VPN 进一步保护。
    
-   酒店服务：提供酒店和客房的详细信息。这些数据通常比较静态，因此易于缓存。
    
-   房价服务：提供未来不同日期的客房价格。酒店行业的一个特点是，房价取决于对某一天入住率的预期。
    
-   预订服务：接收预订请求并预订客房，同时在客房被预订或预订被取消时跟踪库存变化。
    
-   支付服务：执行客户付款；支付事务成功后，将预订状态更新为 paid，失败则更新为 rejected。
    
-   酒店管理服务：仅供获授权的酒店工作人员使用，支持查看即将到来的预订、代客户预订客房、取消预订等功能。
    

为便于阅读，图4省略了许多微服务之间的交互箭头。例如，预订服务和房价服务之间本应有一条箭头，如图5所示。预订服务查询房价服务，以计算一笔预订的客房总价。另一个例子是，酒店管理服务与大多数其他服务之间都应有连接。管理员通过酒店管理服务修改数据时，请求会被转发给实际拥有相应数据的服务处理。

![两种服务交互架构。左侧是两个服务之间的简单直接调用：预订服务通过向下的箭头调用房价服务获取信息。右侧为更复杂的微服务架构：中央酒店管理服务充当编排者，将请求分发给酒店服务、房价服务、预订服务，以及省略号所表示的其他服务。箭头从酒店管理服务指向各下属服务，体现中心控制组件把任务委派给专用服务的关系，与左侧简单的直接交互形成对比。图中的说明将左侧称为单体方式、右侧称为微服务方式。](https://bytebytego.com/images/courses/system-design-interview/hotel-reservation-system/figure-5-connections-between-services-7TQS6ISN.svg)

图5　服务之间的连接

在生产系统中，服务间通信通常使用现代高性能远程过程调用框架，例如 gRPC。这些框架有许多优点，关于 gRPC 的更多内容见资料 \[3\]。

## 第3步：深入设计

讨论完总体设计后，接下来深入分析以下方面。

-   改进数据模型。
    
-   并发问题。
    
-   系统扩展。
    
-   解决微服务架构中的数据不一致问题。
    

### 改进数据模型

如总体设计所述，预订酒店客房时，实际预订的是一种房型，而非某个具体房间。为了适应这一点，API 和表结构需要怎样调整？

在预订 API 的请求参数中，用 roomTypeID 替换 roomID。创建预订的 API 如下：

```
POST /v1/reservations
```

请求参数：

```
{
  "startDate":"2021-04-28",
  "endDate":"2021-04-30",
  "hotelID":"245",
  "roomTypeID":"12354673389",
  "roomCount":"3",
  "reservationID":"13422445"
}
```

更新后的表结构如图6所示。

![更新后的酒店预订数据库结构，按四个逻辑服务划分，虚线框中包含一个或多个表。酒店服务包含 hotel 表（主键 hotel_id，以及 name、address、location）和 room 表（主键 room_id，以及 room_type_id、floor、number、hotel_id、name、is_available）。房价服务包含 room_type_rate 表（hotel_id、date 为主键，以及 rate）。宾客服务包含 guest 表（主键 guest_id，以及 first_name、last_name、email）。预订服务包含 room_type_inventory 表，字段为 hotel_id、room_type_id、date、total_inventory、total_reserved，以及 reservation 表，字段为主键 reservation_id、hotel_id、room_type_id、start_date、end_date、status、guest_id。PK 表示主键。图中未显式绘制表间关系，但 hotel_id、room_type_id、guest_id 等共享字段暗示了外键关联。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fhotel-reservation-system%2Ffigure-6-updated-schema-UUO2LOBS.png&w=3840&q=75)

图6　更新后的表结构

下面简要介绍几个最重要的表。

**room**：保存客房信息。

**room\_type\_rate**：保存特定房型未来各日期的价格。

**reservation**：记录宾客预订数据。

**room\_type\_inventory**：保存酒店客房库存。此表对预订系统非常重要，下面逐列说明。

-   hotel\_id：酒店标识。
    
-   room\_type\_id：房型标识。
    
-   date：某个具体日期。
    
-   total\_inventory：客房总数减去暂时下架的数量，例如因维护而暂停售卖的客房。
    
-   total_reserved：指定 \_hotel\_id_、_room\_type\_id_ 和 _date_ 下，已经预订的客房总数。
    

_room\_type\_inventory_ 表还可以采用其他设计，但按日期一行来保存，更便于管理日期区间内的预订和查询。如图6所示，(_hotel\_id、room\_type\_id、date_) 构成复合主键。系统预先填充未来两年所有日期的库存数据，并通过每日定时任务随着日期推进继续补充。

表结构确定后，估算一下存储量。前面假设有5000家酒店，再假设每家酒店有20种房型，则行数为5000家酒店 \* 20种房型 \* 2年 \* 每年365天 = 7300万行。7300万行并不算很多，单个数据库就能存下。不过，单台服务器会形成单点故障。为了实现高可用，可以在多个地域或可用区之间配置数据库复制。

表4展示了 _room\_type\_inventory_ 表的示例数据。

| **hotel\_id** | **room\_type\_id** | **date**   | **total\_inventory** | **total\_reserved** |
| ------------- | ------------------ | ---------- | -------------------- | ------------------- |
| 211           | 1001               | 2021-06-01 | 100                  | 80                  |
| 211           | 1001               | 2021-06-02 | 100                  | 82                  |
| 211           | 1001               | 2021-06-03 | 100                  | 86                  |
| 211           | 1001               | ...        | ...                  |                     |
| 211           | 1001               | 2023-05-31 | 100                  | 0                   |
| 211           | 1002               | 2021-06-01 | 200                  | 16                  |
| 2210          | 101                | 2021-06-01 | 30                   | 23                  |
| 2210          | 101                | 2021-06-02 | 30                   | 25                  |

表4　room\_type\_inventory 表的示例数据

_room\_type\_inventory_ 表用于判断客户能否预订某种房型。预订的输入和输出可以如下所示：

-   输入：startDate（2021-07-01）、endDate（2021-07-03）、roomTypeId、hotelId、numberOfRoomsToReserve。
    
-   输出：如果指定房型有库存、用户可以预订，则返回 true；否则返回 false。
    

从 SQL 的角度，包含以下两步：

1.  查询指定日期范围内的记录。
    
    ```
    SELECT date, total_inventory, total_reserved
    FROM room_type_inventory
    WHERE room_type_id = ${roomTypeId} AND hotel_id = ${hotelId}
    AND date between ${startDate} and ${endDate}
    ```
    
    查询返回的数据如下：
    
    | **date**   | **total\_inventory** | **total\_reserved** |
    | ---------- | -------------------- | ------------------- |
    | 2021-07-01 | 100                  | 97                  |
    | 2021-07-02 | 100                  | 96                  |
    | 2021-07-03 | 100                  | 95                  |
    
    表5　酒店库存
    
2.  应用程序逐条检查下面的条件：
    
    ```
    if (total_reserved + ${numberOfRoomsToReserve}) <= total_inventory
    ```
    
    如果所有记录均满足条件，就说明该日期范围内的每一天都有足够客房。
    
    系统需要支持10%的超额预订。采用新表结构后，实现起来很简单：
    
    ```
    if (total_reserved + ${numberOfRoomsToReserve}) <= 110% * total_inventory
    ```
    

此时，面试官可能追问：“如果预订数据量大到单个数据库无法容纳，应该怎么办？”可以采用以下策略：

-   只保留当前和未来的预订数据。历史预订不常访问，因此可以归档，部分甚至可以迁移到冷存储。
    
-   数据库分片。最常见的查询是创建预订和按姓名查找预订，这两类查询都需要先选择酒店，因此 _hotel\_id_ 是合适的分片键。可以按 hash(hotel\_id) % number\_of\_servers 对数据分片。
    

### 并发问题

另一个重要问题是重复预订。需要解决两种情况：1）同一用户多次点击“预订”按钮；2）多个用户同时尝试预订同一间客房。

先看第一种情况。如图7所示，同一用户创建了两笔预订。

![连续两次用户操作及其对数据库影响的简化设计图。“用户第一次点击”和“用户第二次点击”分别连接一个 INSERT INTO RESERVATION 方框。两个方框内的类 SQL INSERT 语句参数完全相同：121、2、3、2021-06-01、2021-06-04、pending_pay、guest1，表示预订标识、酒店标识、房型标识等数据。两个 INSERT 都向下指向虚线框中的第1行和第2行，内容相同，说明数据库中生成了两条重复记录。右侧 reservation 表结构列出 reservation_id、hotel_id、room_type_id、start_date、end_date、status、guest_id，表明数据写入目的地。该图展示连续点击造成重复预订的潜在并发问题。](https://bytebytego.com/images/courses/system-design-interview/hotel-reservation-system/figure-7-two-reservations-are-made-ZWW4GV4K.svg)

图7　创建了两笔预订

有两种常见的解决办法：

-   客户端处理。发送请求后，将“提交”按钮置灰、隐藏或禁用，通常能避免重复点击。但该方法并不可靠，例如用户可以禁用 JavaScript，从而绕过客户端检查。
    
-   幂等 API。在预订请求中加入幂等键。如果无论调用多少次，API 都产生相同结果，那么该 API 就是幂等的。图8展示了如何通过幂等键 _reservation\_id_ 避免重复预订，详细步骤如下。
    

![用户与预订服务交互的时序图。用户首先向预订服务发送“生成预订订单”请求，即步骤1。服务在步骤2返回“展示预订页面”，其中包含唯一的 reservation_id。随后，用户在步骤3a和3b两次提交预订，携带相同的 reservation_id。第一次提交成功，第二次提交因 reservation_id 已被使用，触发唯一约束违反错误，以红色叉号表示失败。编号表示事件顺序，虚线表示用户与服务之间的异步通信；服务用矩形表示，用户用人物图标表示。](https://bytebytego.com/images/courses/system-design-interview/hotel-reservation-system/figure-8-unique-constraint-PN2XNUXS.svg)

图8　唯一约束

1\. 生成预订订单。客户填写房型、入住日期、退房日期等详细信息并点击“继续”后，预订服务生成一笔预订订单。

2\. 系统生成订单供客户确认。全局唯一标识符生成器生成唯一的 _reservation\_id_，并通过 API 响应返回。此步骤的界面可以如下所示：

![Booking.com 网站上 Alex 预订酒店的最后步骤。顶部有 Booking.com 标志和三步进度条：“你的选择”“你的信息”已勾选，“最后一步”未勾选。下方提示预订即将完成。预订详情为旧金山万豪侯爵联合广场酒店，标为四星并带点赞图标；2021年7月7日周三入住，7月10日周六退房，入住3晚，房型为无景观特大床房。费用明细包括房价508美元、14%税费71.12美元、旅游费2.19美元，以及2.25%城市税11.43美元。付款区要求填写带有信用卡图标的卡号、持卡人姓名和 MM/YY 格式的有效期，姓名栏已填 Alex。勾选框说明不会立即扣款，只会冻结信用卡额度以保留客房。蓝色“完成我的预订”按钮用于最终提交，还有“更改日期”链接。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fhotel-reservation-system%2Ffigure-9-confirmation-page-EUDNWWUU.png&w=3840&q=75)

图9　确认页面，来源：\[4\]

3a. 提交预订1。请求中包含 _reservation\_id_，它是 reservation 表的主键，见图6。注意，幂等键不一定要使用 _reservation\_id_；本设计选择它，是因为该字段已经存在，而且很适合这一用途。

3b. 如果用户再次点击“完成我的预订”，就会提交预订2。由于 _reservation\_id_ 是 reservation 表主键，可以利用其唯一约束保证不产生重复预订。

图10解释了为何能够避免重复预订。

![通过唯一约束阻止重复插入的系统图。用户第一次点击发起 INSERT INTO RESERVATION，写入121、2、3、2021-06-01、2021-06-04、pending_pay、guest1，其中121以橙色突出。插入成功，以蓝色勾号表示，数据库新增第1行，内容与插入值相同。用户第二次点击尝试插入完全相同的数据，但 reservation_id=121 已存在，触发唯一约束违反，以红色叉号表示。虚线将第二次查询与唯一约束错误说明连接。右侧展示 reservation 表字段：reservation_id、hotel_id、room_type_id、start_date、end_date、status、guest_id。两次查询都突出显示121，以说明它在冲突中的作用。](https://bytebytego.com/images/courses/system-design-interview/hotel-reservation-system/figure-10-unique-constraint-violation-DHYT3GIN.svg)

图10　违反唯一约束

第二种情况：某种房型只剩一间时，多个用户同时预订，会发生什么？看图11所示的场景。

![客房预订系统的竞态条件时序图。用户1和用户2同时尝试预订最后一间客房。最初双方都看到 total_inventory=100、total_reserved=99，表示只剩一间。两人依次在步骤2和3检查库存，均确认还有房。随后两人分别在步骤4和5预订并增加 total_reserved，但两个增量操作独立进行且没有加锁。步骤6和7双方均提交成功，双方看到的 total_reserved 都变成100，尽管只有一间剩余客房，却接受了两笔预订。图中用编号、库存初始和最终状态方框、动作箭头表示流程，提交步骤旁的勾号突出错误结果，垂直时间轴展示并发发生的过程。](https://bytebytego.com/images/courses/system-design-interview/hotel-reservation-system/figure-11-race-condition-7TTL5LW7.svg)

图11　竞态条件

1.  假设数据库隔离级别不是可串行化（serializable）\[5\]。用户1和用户2同时预订同一种房型，但只剩1间。将用户1的执行称为事务1，用户2的执行称为事务2。此时酒店共有100间客房，其中99间已被预订。
    
2.  事务2检查 _(total\_reserved + rooms\_to\_book) <= total\_inventory_，判断剩余客房是否足够。由于还剩1间，结果为 true。
    
3.  事务1也检查 (_total\_reserved + rooms\_to\_book) <= total\_inventory_。由于还剩1间，结果同样为 true。
    
4.  事务1预订客房并更新库存，_reserved\_room_ 变为100。
    
5.  随后事务2也预订该客房。ACID 中的**隔离性**意味着各事务独立于其他事务完成工作。事务1完成并提交之前，其数据变更对事务2不可见。因此，事务2看到的 _total\_reserved_ 仍为99，于是预订客房并更新库存，使 _reserved\_room_ 也变为100。结果是，只剩一间客房，系统却允许两个用户都预订成功。
    
6.  事务1成功提交。
    
7.  事务2成功提交。
    

通常需要某种锁机制来解决这一问题。下面讨论三种技术：

-   悲观锁。
    
-   乐观锁。
    
-   数据库约束。
    

在讨论解决方案之前，先看用于预订客房的 SQL 伪代码，它包含两部分：

-   检查客房库存。
    
-   预订客房。
    
    ```
    # step 1: check room inventory
    SELECT date, total_inventory, total_reserved
    FROM room_type_inventory
    WHERE room_type_id = ${roomTypeId} AND hotel_id = ${hotelId}
    AND date between ${startDate} and ${endDate}
    
    # For every entry returned from step 1
    if((total_reserved + ${numberOfRoomsToReserve}) > 110% * total_inventory) {
      Rollback
    }
    
    # step 2: reserve rooms
    UPDATE room_type_inventory
    SET total_reserved = total_reserved + ${numberOfRoomsToReserve}
    WHERE room_type_id = ${roomTypeId}
    AND date between ${startDate} and ${endDate}
    
    Commit
    ```
    

#### 方案1：悲观锁

悲观锁 \[6\]，也称悲观并发控制，在用户开始更新记录时立即加锁，以阻止同时更新。其他尝试更新该记录的用户必须等待第一个用户提交变更并释放锁。

在 MySQL 中，SELECT ... FOR UPDATE 会锁住查询返回的行。假设事务1已经启动，其他事务就需要等待事务1结束，才能继续执行另一个事务。图12展示了详细过程。

![数据库并发控制场景。用户1的事务1依次执行 BEGIN TRANSACTION、SELECT ... FOR UPDATE 检查库存、UPDATE 预订客房、COMMIT TRANSACTION。与此同时，用户2的事务2也开始，并尝试用 SELECT ... FOR UPDATE 检查库存，但事务1已经持有库存行的锁，因此事务2必须等待。事务1提交后，事务2才能继续执行预订步骤；此时 total_reserved 已为100，无法再预订，所以执行 ROLLBACK TRANSACTION，事务失败且修改不持久化。垂直箭头表示时间推进和各事务步骤，水平连接突出事务2等待事务1完成的同步点。](https://bytebytego.com/images/courses/system-design-interview/hotel-reservation-system/figure-12-pessimistic-locking-DLAKLD67.svg)

图12　悲观锁

图12中，由于事务1锁定了数据行，事务2的 SELECT ... FOR UPDATE 必须等待事务1结束。事务1完成后，_total\_reserved_ 变成100，用户2已经没有客房可订。

**优点：**

-   防止应用程序更新正在被修改，或已经发生修改的数据。
    
-   容易实现，通过串行化更新避免冲突。数据竞争激烈时，悲观锁很有用。
    

**缺点：**

-   锁定多个资源时可能发生死锁，编写无死锁的应用代码并不容易。
    
-   可扩展性较差。如果某个事务长时间持锁，其他事务就无法访问该资源，会严重影响数据库性能，尤其是事务持续时间很长或涉及很多实体时。
    

由于这些限制，本设计不建议为预订系统采用悲观锁。

#### 方案2：乐观锁

乐观锁 \[7\]，也称乐观并发控制，允许多个并发用户尝试更新同一资源。

乐观锁有两种常见实现方式：版本号和时间戳。通常认为版本号更合适，因为服务器时钟可能随时间变得不准确。下面以版本号说明乐观锁的工作方式。

图13展示了成功和失败两种情况。

![数据版本与冲突处理的两种场景。左侧无冲突：用户1读取版本v1，写入v2；随后用户2读取v2，写入v3，各操作顺序执行。有向箭头连接表示版本的圆形节点，人物图标旁标有读写动作，无冲突旁以勾号表示成功。右侧发生冲突：用户1与用户2都读取v1，并同时尝试写入v2，形成两个不同的v2版本。两个v2之间以标注“冲突”的弯曲虚线连接，红色叉号突出由此造成的数据不一致。两侧均用用户图标、版本节点和箭头展示动作与数据流。](https://bytebytego.com/images/courses/system-design-interview/hotel-reservation-system/figure-13-optimistic-locking-KQUA2477.svg)

图13　乐观锁

1.  在数据库表中新增 version 列。
    
2.  用户修改某行之前，应用先读取该行的版本号。
    
3.  用户更新该行时，应用将版本号加1，再写回数据库。
    
4.  数据库执行校验，下一版本号必须恰好比当前版本号大1。校验失败则中止事务，用户从第2步开始重试。
    

乐观锁通常比悲观锁更快，因为不需要锁定数据库。但并发度很高时，乐观锁性能会大幅下降。

例如，很多客户端同时预订客房。由于读取剩余房数的客户端数量不受限制，所有客户端都可能读到相同的余量和当前版本号。随后各客户端提交预订并写回数据库时，只有一个成功，其余都收到版本检查失败的消息，必须重试。下一轮重试依然只有一个成功，其他客户端继续重试。虽然最终结果正确，但反复重试会造成很差的用户体验。

优点：

-   防止应用修改过时数据。
    
-   无须锁定数据库资源。从数据库的角度看，没有加锁；应用完全通过版本号处理相关逻辑。
    
-   通常用于数据竞争较少的情况。冲突罕见时，事务不必承担锁管理的开销就能完成。
    

缺点：

-   数据竞争激烈时性能较差。

酒店预订的 QPS 通常不高，因此乐观锁是较好的选择。

#### 方案3：数据库约束

该方法与乐观锁非常相似。在 _room\_type\_inventory_ 表中加入下面的约束，看看它如何工作：

```
CONSTRAINT `check_room_count` CHECK((`total_inventory - total_reserved` >= 0))
```

以图14为例，用户2尝试预订时，_total\_reserved_ 变成101，违反 _total\_inventory (100) - total\_reserved (101) >= 0_ 约束，因此事务回滚。

![客房预订系统的竞态时序图。用户1和用户2都尝试预订最后一间客房，最初看到 total_inventory=100、total_reserved=99。双方在步骤2和3检查库存，确认尚余一间。用户1在步骤4增加 total_reserved，并在步骤5成功提交，旁边有蓝色勾号。用户2在步骤6也尝试增加 total_reserved，但这时 total_inventory - total_reserved 变为负数，违反约束，以红色叉号表示失败；原说明将此归因于缺乏适当锁机制的并发访问。图中编号显示并发动作顺序，最终状态明确违反 total_inventory - total_reserved >= 0；左侧为垂直时间轴。](https://bytebytego.com/images/courses/system-design-interview/hotel-reservation-system/figure-14-database-constraint-GWRAPFAG.svg)

图14　数据库约束

**优点**

-   易于实现。
    
-   数据竞争较少时效果很好。
    

**缺点**

-   与乐观锁相似，竞争激烈时可能有大量失败。用户看到还有客房，真正预订时却收到“没有可用客房”的响应，这种体验令人沮丧。
    
-   数据库约束不像应用代码那样容易进行版本管理。
    
-   并非所有数据库都支持约束，从一种数据库迁移到另一种时可能出现问题。
    

该方法容易实现，而且酒店预订的数据竞争通常不高，QPS较低，因此也是酒店预订系统的不错选择。

### 可扩展性

酒店预订系统的负载通常不高。但面试官可能追问：“如果预订系统不仅服务一家连锁酒店，而是服务 booking.com 或 expedia.com 这样的热门旅游网站呢？”这种情况下，QPS可能高出1000倍。

系统负载升高后，需要找出潜在瓶颈。所有服务都是无状态的，容易通过增加服务器扩展；但数据库保存了全部状态，不能简单增加数据库实例就完成扩展。下面讨论数据库如何扩展。

#### 数据库分片

扩展数据库的一种方法是分片，将数据拆分到多个数据库，使每个数据库只保存一部分数据。

分片时需要考虑数据如何分布。从数据模型可以看出，大多数查询都需要按 _hotel\_id_ 筛选，因此自然选择按 _hotel\_id_ 分片。图15将负载分散到16个分片。假设总 QPS 为30,000，分片后每个分片承担30,000 / 16 = 1875 QPS，在单台 MySQL 服务器可处理的范围内。

![数据库分片方案。中央预订服务将请求发送到编号0至15的十六个数据库分片，路由依据 hotel_id % 16。酒店标识作为取模输入，以16为除数，余数决定请求落在哪个分片。例如 hotel_id=17 时，余数为1，请求送至分片1。每个分片用圆柱数据库图标表示，保存酒店预订数据的一部分；箭头展示从预订服务到对应分片的请求流。](https://bytebytego.com/images/courses/system-design-interview/hotel-reservation-system/figure-15-database-sharding-GER3MC5O.svg)

图15　数据库分片

#### 缓存

酒店库存有一个特点：只有当前和未来的库存才有意义，因为客户只能预订近期未来的客房。

因此，理想的存储应有生存时间（TTL）机制，自动使旧数据过期；历史数据可以在另一个数据库中查询。Redis 是不错的选择，因为 TTL 和最近最少使用（LRU）淘汰策略有助于充分利用内存。

如果加载速度和数据库扩展成为问题，例如系统达到 booking.com 或 expedia.com 的规模，可以在数据库上方增加缓存层，将客房库存检查和预订逻辑移到缓存层，如图16所示。大多数不满足条件的请求会被库存缓存挡住，因此只有少部分请求访问库存数据库。需要强调的是，即使 Redis 显示库存充足，也仍要在数据库端重新检查，以防万一。库存数据以数据库为准。

![预订服务的简化架构。顶部预订服务连接“查询库存”和“更新库存”两个功能模块。查询库存连接红色缓存图标；更新库存同时连接“异步更新缓存”模块和标为分片15的数据库。异步更新缓存表示缓存更新以非阻塞方式进行，不妨碍预订服务的主要响应流程。整体上，服务从缓存查询库存，并在更新时写入持久数据库，同时异步更新缓存。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fhotel-reservation-system%2Ffigure-16-caching-LQA6WZX7.png&w=3840&q=75)

图16　缓存

先介绍各个组件。

**预订服务**：支持以下库存管理 API：

-   根据酒店标识、房型和日期范围查询可用客房数。
    
-   通过执行 _total\_reserved + 1_ 预订客房。
    
-   用户取消预订时更新库存。
    

**库存缓存**：所有库存管理的查询操作都移到 Redis 库存缓存中，需要预先填充库存数据。缓存是键值存储，结构如下：

```
key: hotelID_roomTypeID_{date}
value: the number of available rooms for the given hotel ID, room type ID and date.
```

对酒店预订系统而言，检查库存的读取量比写入量高一个数量级。大多数读取由缓存处理。

**库存数据库**：保存库存的权威数据。

**缓存引入的新挑战**

增加缓存层显著提高系统可扩展性与吞吐量，但也带来新问题：如何维持数据库与缓存的数据一致性。

用户预订客房时，正常流程执行两个操作：

1.  查询库存，确认是否有足够客房。该查询在库存缓存上执行。
    
2.  更新库存。先更新库存数据库，再将变更异步传播到缓存。可以由应用在数据写入数据库后调用缓存更新；也可以采用变更数据捕获（CDC）\[8\]。CDC 读取数据库变更，并将其应用到其他数据系统。常见方案是 Debezium \[9\]，它使用源连接器读取数据库变更，再将这些变更应用于 Redis 等缓存方案 \[10\]。
    

由于库存先在数据库中更新，缓存可能未反映最新数据。例如，缓存显示仍有空房，但数据库已无房；反过来也可能发生。

仔细分析会发现，只要数据库执行最终库存校验，库存缓存与数据库之间的不一致就不会影响这一最终判断。

例如，缓存显示还有空房，而数据库已经没有。用户查询库存时看到有房，于是尝试预订。请求到达库存数据库后，数据库校验发现余量为零，客户端收到错误，提示最后一间房刚刚被其他人订走。用户刷新网站时，很可能就会看到无房，因为在其点击刷新前，数据库已经把库存同步到缓存。

**优点**

-   降低数据库负载。读取查询由缓存层处理，显著减少数据库压力。
    
-   性能高。从内存直接读取结果，查询非常快。
    

**缺点**

-   很难维持数据库与缓存的一致性，需要仔细分析不一致会如何影响用户体验。

### 服务间的数据一致性

传统单体架构 \[11\] 通过共享关系型数据库保证数据一致性。本设计采用折中方案，让预订服务同时处理预订和库存 API，使库存表与预订表位于同一个关系型数据库。正如“并发问题”一节所述，这样可以利用关系型数据库的 ACID 特性，妥善处理预订流程中的许多并发问题。

但如果面试官是一位微服务纯粹主义者，可能会质疑这种折中方案。在其看来，微服务架构应该是每个服务拥有独立数据库，如图17右侧所示。

![酒店预订系统的单体架构与微服务架构对比。左侧三个服务——预订服务、库存服务、支付服务——都访问一个中央酒店数据库，箭头表示各服务到共享数据库的数据流。右侧每个服务拥有专用数据库：预订数据库、库存数据库、支付数据库。各服务只与对应数据库交互，以支持独立部署和扩展。主要区别在于左侧使用集中共享数据库，而右侧采用按服务分离的数据库。](https://bytebytego.com/images/courses/system-design-interview/hotel-reservation-system/figure-17-monolithic-vs-microservice-5V7CXVAI.svg)

图17　单体架构与微服务架构

这种纯粹的设计会引入很多数据一致性问题。本书首次讨论微服务，下面解释这些问题产生的方式和原因。为便于理解，只讨论两个服务；现实公司中可能有数百个微服务。在单体架构下，如图18所示，可以把不同操作放进同一个事务中，确保 ACID 特性。

![用户与数据库交互的客房预订时序图。左侧用户用人物图标表示，右侧数据库用圆柱表示，水平箭头展示请求。用户先发送“管理客房库存”，数据库处理后用虚线箭头响应；接着发送“预订客房”，数据库再次处理并响应。数据库两次处理过程由标注“单个事务”的垂直箭头连接，表示两个动作属于同一个数据库事务，从而保证原子性和一致性。处理过程以垂直激活条表示，数据库与其示意形状之间的虚线暗示松耦合关系，数据库也可能是独立服务。](https://bytebytego.com/images/courses/system-design-interview/hotel-reservation-system/figure-18-monolithic-architecture-4WZWZO7V.svg)

图18　单体架构

微服务架构中，每个服务都有独立数据库，一个逻辑上应当原子的操作可能跨越多个服务，因而无法用单个事务保证数据一致性。如图19所示，如果预订数据库更新失败，就需要回滚库存数据库中已预订的客房数。通常正常路径只有一条，但可能导致不一致的失败场景有很多。

![用户、库存服务和预订服务之间的时序图。用户首先通过实线箭头发送“管理客房库存”请求，库存服务以激活条表示处理过程，再用虚线箭头返回响应，这一交互标为“单个事务”。随后用户向预订服务发送“预订客房”，预订服务处理并返回响应，这一交互也标为“单个事务”。垂直虚线为服务生命线，展示各自活动时段。图中用户独立调用两个服务，每次调用属于单独、自包含的事务。](https://bytebytego.com/images/courses/system-design-interview/hotel-reservation-system/figure-19-microservice-architecture-7FGGR4AA.svg)

图19　微服务架构

下面概述业界已验证的解决数据不一致问题的技术。更多细节可阅读参考资料。

-   两阶段提交（2PC）\[12\]。2PC 是用于保证跨多个节点原子提交的数据库协议，即所有节点要么都成功，要么都失败。它是阻塞协议，单个节点失败可能使处理停滞，直到节点恢复，因此性能不理想。
    
-   Saga。Saga 由一系列本地事务组成，每个事务更新数据并发布消息，触发下一步事务。如果某一步失败，Saga 就执行补偿事务，撤销前序事务的变更 \[13\]。2PC 通过单次整体提交完成 ACID 事务，而 Saga 包含多个步骤，依赖最终一致性。
    

解决微服务间数据不一致需要比较复杂的机制，会显著增加整体设计的复杂度。作为架构师，需要判断这些额外复杂性是否值得。本题中，我们认为不值得，因此采用更务实的方案：将预订与库存数据保存在同一个关系型数据库。

## 第4步：总结

本章提出了酒店预订系统的设计。首先收集需求并进行粗略估算，以了解系统规模；总体设计中介绍 API、初步数据模型和系统架构。深入设计时，认识到预订应按房型而不是具体房间进行，因此探讨了其他数据库结构。还详细讨论竞态条件，并提出几种解决方案：

-   悲观锁。
    
-   乐观锁。
    
-   数据库约束。
    

随后讨论了数据库分片、Redis 缓存等系统扩展方式。最后分析微服务架构中的数据一致性问题，并简要介绍若干解决方案。

恭喜你读到这里！为自己的努力鼓掌吧，做得很好！

## 本章小结

![酒店预订系统设计的思维导图。中心节点为“酒店预订”，分为四个步骤。第1步收集需求：功能需求包括预订客房、管理后台、超额预订，非功能需求包括高并发和适中的延迟。第2步进行 API 设计，分为酒店、客房、预订三类，并建立数据模型，覆盖查看酒店详情、查找可用房型、创建和查询预订。第3步改进总体设计中的数据模型，使用 roomTypeID，并通过悲观锁、乐观锁、数据库约束解决并发。第4步总结，讨论数据库分片、缓存等扩展方法，以及服务间数据一致性。整个图为树状，各步骤继续分支为具体设计考虑和实现细节。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fhotel-reservation-system%2Fchapter-summary-HKFZ3FPJ.png&w=3840&q=75)

本章小结

## 参考资料

\[1\] 微服务：[<u>https://en.wikipedia.org/wiki/Microservices</u>](https://en.wikipedia.org/wiki/Microservices)

\[2\] 微服务架构有哪些优势：

[<u>https://www.appdynamics.com/topics/benefits-of-microservices</u>](https://www.appdynamics.com/topics/benefits-of-microservices)

\[3\] gRPC：[<u>https://www.grpc.io/docs/what-is-grpc/introduction/</u>](https://www.grpc.io/docs/what-is-grpc/introduction/)

\[4\] 来源：Booking.com iOS 应用。

\[5\] 可串行化：[<u>https://en.wikipedia.org/wiki/Serializability</u>](https://en.wikipedia.org/wiki/Serializability)

\[6\] 乐观与悲观记录锁：[<u>https://www.ibm.com/docs/en/rational-clearquest/10.0.7?topic=clearquest-optimistic-pessimistic-record-locking</u>](https://www.ibm.com/docs/en/rational-clearquest/10.0.7?topic=clearquest-optimistic-pessimistic-record-locking)

\[7\] 乐观并发控制：[<u>https://en.wikipedia.org/wiki/Optimistic_concurrency_control</u>](https://en.wikipedia.org/wiki/Optimistic_concurrency_control)

\[8\] 变更数据捕获：[<u>https://docs.oracle.com/cd/B10500_01/server.920/a96520/cdc.htm</u>](https://docs.oracle.com/cd/B10500_01/server.920/a96520/cdc.htm)

\[9\] Debezium：[<u>https://debezium.io/</u>](https://debezium.io/)

\[10\] Redis Sink 连接器：[<u>https://debezium.io/documentation/reference/stable/operations/debezium-server.html</u>](https://debezium.io/documentation/reference/stable/operations/debezium-server.html)

\[11\] 单体架构：[<u>https://microservices.io/patterns/monolithic.html</u>](https://microservices.io/patterns/monolithic.html)

\[12\] 两阶段提交协议：[<u>https://en.wikipedia.org/wiki/Two-phase_commit_protocol</u>](https://en.wikipedia.org/wiki/Two-phase_commit_protocol)

\[13\] Saga：[<u>https://microservices.io/patterns/data/saga.html</u>](https://microservices.io/patterns/data/saga.html)



