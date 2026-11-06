# 26 实时游戏排行榜

本章将带你逐步应对为一款在线手机游戏设计排行榜的挑战。

什么是排行榜？排行榜在游戏以及其他场景中很常见，用于展示某项锦标赛或竞赛中谁处于领先位置。用户完成任务或挑战后会获得积分，积分最高的人排在榜首。图 1 展示了一个手机游戏排行榜示例。排行榜不仅展示领先参赛者的名次，也显示当前用户在其中的位置。

![图中展示了某项比赛中四支队伍的排名。排行榜采用表格形式，包含“排名”“玩家”和“积分”三列，浅粉色表头标明列名。表头下的四行分别列出各队数据，每行开头都有一颗星，随着名次下降，星星颜色从红色逐渐变为浅粉色。“排名”列显示数字名次 1、2、3、4；“玩家”列依次列出队伍名称 Aquaboys、B team、Berlin's Angels 和 GrendelTeam；“积分”列显示对应分数，分别为 976、956、890 和 878。数据按积分降序排列，积分最高的队伍位于顶部。各行之间没有连接或信息流，每一行代表排行榜中的一条独立记录。](https://bytebytego.com/images/courses/system-design-interview/real-time-gaming-leaderboard/figure-1-leaderboard-NWJTV4ZV.svg)

图 1 排行榜

## 第 1 步：理解问题并确定设计范围

排行榜可以相当简单，但也有许多因素会增加复杂度。我们应该先明确需求。

**候选人：** 排行榜中的积分如何计算？

**面试官：** 用户每赢一局就得一分。可以采用简单的积分体系，每位用户都有一个对应的积分。用户每赢一局，就在总积分上加一分。

**候选人：** 排行榜是否包含所有玩家？

**面试官：** 是的。

**候选人：** 排行榜是否有对应的时间周期？

**面试官：** 每个月都会开启一轮新的比赛，同时建立新的排行榜。

**候选人：** 是否可以假设我们只关心前 10 名用户？

**面试官：** 我们希望展示前 10 名用户，以及某个特定用户在排行榜中的位置。如果时间允许，也讨论一下如何返回某个用户前后各四个名次的用户。

**候选人：** 一轮比赛有多少用户参与？

**面试官：** 平均有 500 万日活跃用户（DAU）和 2500 万月活跃用户（MAU）。

**候选人：** 一轮比赛期间，用户平均会玩多少局？

**面试官：** 每位玩家平均每天玩 10 局。

**候选人：** 两个玩家积分相同时，如何确定名次？

**面试官：** 这种情况下，他们的名次相同。如果时间允许，可以讨论打破平局的方法。

**候选人：** 排行榜需要实时更新吗？

**面试官：** 是的，我们希望展示实时结果，或者尽可能接近实时。不能只提供批量处理后的历史结果。

现在已经收集了所有需求，下面列出功能需求。

-   展示排行榜上的前 10 名玩家。
    
-   显示某个用户的具体名次。
    
-   展示目标用户前后各四个名次的玩家（加分项）。
    

除了明确功能需求，也需要理解非功能需求。

**非功能需求**

-   实时更新积分。
    
-   积分更新实时反映到排行榜上。
    
-   满足一般性的可扩展性、可用性和可靠性要求。
    

### 粗略估算

下面做一些粗略计算，确定方案可能需要应对的规模和挑战。

系统有 500 万 DAU，如果玩家在 24 小时内均匀分布，平均每秒约有 50 位用户（5,000,000 DAU / 10^5 秒 = ~50）。不过，实际使用量很可能分布不均；不同时区的大量用户在晚间有时间玩游戏，因此可能出现高峰。考虑到这一点，可以假设峰值负载是平均值的 5 倍。因此，系统应能够支持每秒 250 位用户的峰值负载。

用户得分的 QPS：如果每位用户平均每天玩 10 局，用户得分的 QPS 为 50 \* 10 = ~500。峰值 QPS 是平均值的 5 倍：500 \* 5 = 2,500。

获取前 10 名排行榜的 QPS：假设每位用户每天打开游戏一次，且仅在首次打开游戏时加载前 10 名排行榜，则这一 QPS 约为 50。

## 第 2 步：提出总体设计并达成共识

本节将讨论 API 设计、总体架构和数据模型。

### API 设计

总体而言，需要以下三个 API：

**POST /v1/scores**

用户赢得一局游戏后，更新其在排行榜中的位置。请求参数如下。这应当是一个仅供游戏服务器调用的内部 API，客户端不能直接更新排行榜积分。

| **字段** | **说明**                                       |
| --------- | ----------------------------------------------------- |
| user\_id  | 赢得游戏的用户。                             |
| points    | 用户赢得一局游戏所获得的积分。 |

表 1

响应：

| **名称**        | **说明**                      |
| --------------- | ------------------------------------ |
| 200 OK          | 成功更新用户积分。 |
| 400 Bad Request | 更新用户积分失败。     |

表 2

**GET /v1/scores**

从排行榜中获取前 10 名玩家。

响应示例：

```
{
  "data": [
    {
      "user_id": "user_id1",
      "user_name": "alice",
      "rank": 1,
      "score": 12543
    },
    {
      "user_id": "user_id2",
      "user_name": "bob",
      "rank": 2,
      "score": 11500
    }
  ],
  ...
  "total": 10
}
```

**GET /v1/scores/{:user\_id}**

获取特定用户的名次。

| **字段** | **说明**                                       |
| --------- | ----------------------------------------------------- |
| user\_id  | 要查询名次的用户 ID。 |

表 3

响应示例：

```
{
    "user_info": {
        "user_id": "user5",
        "score": 1000,
        "rank": 6,
    }
}
```

### 总体架构

图 2 展示了总体设计。设计中有两个服务：游戏服务让用户玩游戏，排行榜服务则创建并展示排行榜。

![图中以系统架构图展示了游戏排行榜系统中的信息流。顶部方框中，用户以人物图标表示，与手机图标表示的移动设备交互。用户会触发两个主要操作：“1）赢得游戏”向“游戏服务”方框发起请求；游戏服务随后向“排行榜服务”发送“2）更新积分”消息。同时，用户也可以向排行榜服务请求“4 a. 获取排行榜”和“4 b. 获取 player_rank”。排行榜服务接收并处理积分更新，随后通过“3）更新积分”操作更新圆柱形数据库图标表示的“排行榜存储”。带编号的圆圈表示操作顺序，编号 4 下的字母项代表一次用户请求中的子操作。箭头表示组件间的数据流方向。](https://bytebytego.com/images/courses/system-design-interview/real-time-gaming-leaderboard/figure-2-high-level-design-XQWDCF72.svg)

图 2 总体设计

1.  玩家赢得一局游戏后，客户端向游戏服务发送请求。
    
2.  游戏服务确认获胜结果有效，并调用排行榜服务更新积分。
    
3.  排行榜服务更新排行榜存储中的用户积分。
    
4.  玩家直接调用排行榜服务，获取排行榜数据，包括：
    
    a. 前 10 名排行榜。
    
    b. 该玩家在排行榜中的名次。
    

在确定这一设计之前，我们考虑并排除了几个其他方案。回顾这一思考过程并比较不同选择，可能很有帮助。

**客户端是否应该直接与排行榜服务通信？**

![图中展示了游戏获胜后更新排行榜的两种系统设计。“当前方案”采用顺序执行的流程：人物图标和手机图标表示的用户赢得游戏（步骤 1），触发对“游戏服务”的调用；游戏服务随后调用“排行榜服务”更新积分（步骤 2）。“备选方案”采用不同方式：用户（使用相同的人物和手机图标）直接设置积分（步骤 1），并直接调用排行榜服务更新积分（步骤 2），绕过中间的游戏服务。两图都用编号步骤表示信息和操作的流向，外围虚线将两个独立的设计方案区分开。](https://bytebytego.com/images/courses/system-design-interview/real-time-gaming-leaderboard/figure-3-who-sets-the-leaderboard-score-O6AIP5UI.svg)

图 3 谁来设置排行榜积分

在备选设计中，积分由客户端设置。这种方案不安全，容易遭受中间人攻击 \[x\]：玩家可以设置代理，随意修改积分。因此，积分需要在服务器端设置。

注意，对于在线扑克这类以服务器为权威的游戏，客户端可能无须显式调用游戏服务器来设置积分。游戏服务器处理所有游戏逻辑，知道游戏何时结束，因此可以在没有客户端参与的情况下设置积分。

**游戏服务与排行榜服务之间是否需要消息队列？**

这主要取决于游戏积分的用途。如果数据还会用于其他地方，或需要支持多种功能，那么将数据放入 Kafka 可能是合理的，如图 4 所示。这样，同一份数据就能被排行榜服务、分析服务、推送通知服务等多个消费者消费。对于回合制或多人游戏，这一点尤其适用，因为需要将积分更新通知其他玩家。根据与面试官的讨论，这并不是明确需求，因此本设计不使用消息队列。

![图中以系统架构图展示了使用 Apache Kafka 作为消息代理的数据流。“游戏服务”组件生成数据，并将其作为消息发送到中央的“Kafka”组件。Kafka 作为分布式流处理平台，接收并存储这些消息。随后，数据由三个独立服务从 Kafka 中消费：“排行榜服务”“分析服务”和“推送通知服务”。每个消费服务都从 Kafka 接收消息流，根据游戏数据并行处理，分别更新排行榜、分析结果和用户通知。箭头表示数据从生产者（游戏服务）单向流入 Kafka，再从 Kafka 流向三个消费服务的方向。](https://bytebytego.com/images/courses/system-design-interview/real-time-gaming-leaderboard/figure-4-game-scores-are-used-by-multiple-services-UWBWILEE.svg)

图 4 多个服务使用游戏积分

### 数据模型

排行榜存储是系统的关键组件之一。我们将讨论三种可能的方案：关系型数据库、Redis 和 NoSQL（NoSQL 方案将在深入设计部分介绍）。

#### 关系型数据库方案

先退一步，从最简单的方案开始。如果不考虑规模，且只有少量用户，会怎样？

我们很可能会选择使用关系型数据库系统（RDS）实现简单的排行榜。每个月的排行榜可以用一张数据库表表示，其中包含用户 ID 和积分两列。用户获胜时，如果是新用户，就给予 1 分；否则，将现有积分增加 1 分。为了确定用户在排行榜中的名次，将表按积分降序排序即可。详细说明如下。

排行榜数据库表：

![图中展示了排行榜的简单数据库表结构。表以包含表头和表体的矩形框表示，顶部表头用粗体标明表名“leaderboard”。表体通过横线与表头分隔，其中两行定义了表的列。第一行定义名为“user_id”、数据类型为“varchar”的列，表示存储用户标识符的可变长度字符串。第二行定义名为“score”、数据类型为“int”的列，表示存储用户积分的整数值。图中仅展示表的内部结构，没有其他连接或信息流。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Freal-time-gaming-leaderboard%2Ffigure-5-leaderboard-table-WGJD5HTB.png&w=3840&q=75)

图 5 leaderboard 表

实际中，leaderboard 表还会包含 game\_id、时间戳等额外信息，但查询和更新排行榜的基本逻辑不变。为简单起见，假设 leaderboard 表中仅存储当月的排行榜数据。

**用户获得一分：**

![图中以简化的系统架构图展示了排行榜服务与数据库之间的数据流。左侧标为“排行榜服务”的矩形框代表负责管理排行榜数据的软件组件，一条有向箭头从该框出发。箭头标为“向数据库插入或更新”，表示排行榜服务将数据发送给数据库，执行插入或更新操作。箭头连接到右侧代表数据库（DB）的圆柱体，后者接收并存储排行榜数据。整体展示了从排行榜服务到数据库的单向数据流，表示服务使用排行榜信息更新持久存储的过程。](https://bytebytego.com/images/courses/system-design-interview/real-time-gaming-leaderboard/figure-6-a-user-wins-a-point-FN3VKT66.svg)

图 6 用户获得一分

假设每次积分更新都增加 1 分。如果用户尚未出现在当月排行榜中，第一次插入如下：

```
INSERT INTO leaderboard (user_id, score) VALUES ('mary1934', 1);
```

更新用户积分的语句如下：

```
UPDATE leaderboard set score=score + 1 where user_id='mary1934';
```

**查找用户在排行榜中的位置：**

![图中以简化的系统架构图展示了获取排行榜数据的过程。左侧标为“排行榜服务”的矩形框代表负责管理和展示排行榜信息的服务。一条有向箭头从该框向右延伸，标为“从数据库获取，按排名排序”，表示服务从数据库获取数据。箭头连接右侧代表数据库（DB）的圆柱体。箭头和标注清楚表明，从数据库获取的数据已按排名排序，然后推测会由排行榜服务使用。整体展示了从数据库到排行榜展示服务的简单数据流。](https://bytebytego.com/images/courses/system-design-interview/real-time-gaming-leaderboard/figure-7-find-a-users-leaderboard-position-6WVQDZK3.svg)

图 7 查找用户在排行榜中的位置

要获取用户排名，需要对 leaderboard 表进行排序，并按积分确定名次：

```
SELECT (@rownum := @rownum + 1) AS rank, user_id, score
FROM leaderboard
ORDER BY score DESC;
```

SQL 查询的结果如下：

| **rank** | **user\_id**  | **score** |
| -------- | ------------- | --------- |
| 1        | happy\_tomato | 987       |
| 2        | mallow        | 902       |
| 3        | smith         | 870       |
| 4        | mary1934      | 850       |

表 4 按积分排序的结果

数据集较小时，这一方案可以工作；但有数百万行数据时，查询就会非常缓慢。下面分析原因。

为了确定用户的排名，需要将每位玩家都放到排行榜中的正确位置上，才能准确得出其名次。还要记住，积分可能相同，因此名次并不只是用户在列表中的位置。

当需要处理大量持续变化的信息时，SQL 数据库的性能并不好。对数百万行数据进行排名计算，可能需要数十秒，这无法满足实时要求。由于数据不断变化，采用缓存也不可行。

关系型数据库并非为承受这种实现所需的大量读取查询而设计。如果采用批量操作，RDS 可以很好地完成任务，但这不符合实时返回用户排行榜位置的要求。

一种可行的优化是添加索引，并通过 LIMIT 子句限制需要扫描的页数。查询如下：

```
SELECT (@rownum := @rownum + 1) AS rank, user_id, score
FROM leaderboard
ORDER BY score DESC
LIMIT 10
```

不过，这种方案的扩展性仍然不佳。首先，查询用户排名的性能不好，因为本质上仍需扫描表来确定名次。其次，对于不在榜首的用户，这种方案没有提供一种直接确定其排名的方法。

#### Redis 方案

我们希望找到一种方案，即使面对数百万用户也能提供可预测的性能，并且方便地执行常见排行榜操作，无须依赖复杂的数据库查询。

Redis 为这个问题提供了一种可能的解决方案。Redis 是支持键值对的内存数据存储。由于在内存中运行，它能实现快速读写。Redis 有一种称为**有序集合（sorted set）**的专用数据类型，非常适合解决排行榜系统设计问题。

##### 什么是有序集合？

有序集合是一种类似集合的数据类型，其中每个成员都关联一个分数。集合成员必须唯一，但分数可以重复。分数用于按升序排列有序集合中的成员。

我们的排行榜场景与有序集合完全契合。有序集合在内部由两种数据结构实现：哈希表和跳表 \[1\]。哈希表将用户映射到积分，跳表将积分映射到用户。在有序集合中，用户按积分排序。理解有序集合的一种直观方式，是将其想象成一张包含 score 和 member 两列的表，如图 8 所示。表中的数据按积分降序排列。

![图中以简单的数据流图展示了 2021 年 2 月的排行榜。标为“leaderboard_feb_2021”的矩形框作为数据源，一条有向箭头从该框指向表格。表格包含“score”（数值积分）和“member”（用户名，例如“user10”“user20”）两列，共九行，每行展示一位用户的积分和对应用户名，按积分从高到低排列。箭头表示表格 score 和 member 两列所展示的信息来自 leaderboard_feb_2021，体现了排行榜的数据结构以及可能的访问或展示方式。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Freal-time-gaming-leaderboard%2Ffigure-8-february-leaderboard-is-represented-by-the-sorted-set-G3APTESM.png&w=3840&q=75)

图 8 用有序集合表示 2 月的排行榜

本章不展开有序集合实现的全部细节，而是介绍总体思路。

跳表是一种支持快速查找的链表结构，由基础有序链表和多级索引组成。下面来看一个例子。图 9 中，基础链表是有序的单向链表，插入、删除和查找操作的时间复杂度为 _O(n)_。

如何加快这些操作？一种思路是像二分查找算法一样，快速到达中间位置。为此，我们添加第 1 级索引，每隔一个节点建立一个索引；再添加第 2 级索引，每隔一个第 1 级索引节点建立一个索引。不断引入新层级，每个新层级都在上一层的基础上隔一个节点取一个。当索引节点间的距离达到 n/2 - 1 时，停止增加层级，其中 n 是节点总数。如图 9 所示，使用多级索引后，查找数字 45 要快得多。

![图中展示了有序链表的多级索引结构。顶部“基础链表”中依次排列着有序数字 1、4、8、10、15、26、36、45、60 和 68，箭头表示顺序。下方的“第 1 级索引”更为稀疏，仅包含基础链表中的部分数字 1、8、15、36、60，并通过箭头相连。其下方的第二条平行链表展示基础链表中的其余数字 1、4、8、10、15、26、36、45、60、68。底部“第 2 级索引”进一步简化，仅包含相连的 1、15、60。其下还有一层与前面结构相同的第 1 级索引，最底部再次展示完整的基础链表。箭头表示各层之间的连接，说明索引如何指向基础链表的子集。数字 45 在各层都以粉色突出显示，强调其在各层中的一致存在。整体结构展示了分层索引方式，可能用于在大型数据集中高效查找或获取数据。](https://bytebytego.com/images/courses/system-design-interview/real-time-gaming-leaderboard/figure-9-skip-list-WNOCIURA.svg)

图 9 跳表

数据集较小时，跳表带来的速度提升并不明显。图 10 展示了一个包含 5 级索引的跳表示例。在基础链表中，需要经过 62 个节点才能到达目标节点；在跳表中，只需要遍历 11 个节点 \[2\]。

![图中展示了一个有向无环图（DAG），表示数据流或处理流水线。图中有许多圆形节点，大致呈三角形排列，底部有 40 个节点，逐渐收拢至顶点处的单个节点。节点之间的箭头表示数据或信息从左向右单向流动。最上方左侧节点作为源头，启动整个流程。数据经过多层节点，可能代表不同的处理或转换阶段。最右侧的最终节点为红色，代表流水线终点，处理后的数据可能在此汇集或输出。节点和边上没有标签或具体数据值，仅以视觉形式展示数据流的结构和方向。](https://bytebytego.com/images/courses/system-design-interview/real-time-gaming-leaderboard/figure-10-skip-list-with-5-levels-of-indexes-K5QMZDBC.svg)

图 10 包含 5 级索引的跳表

有序集合的性能优于关系型数据库，一方面是因为每个元素在插入或更新时都会自动放到正确的位置；另一方面是因为有序集合中的添加和查找操作具有对数时间复杂度，即 _O(logn)_。

相比之下，要在关系型数据库中计算特定用户的排名，需要执行嵌套查询：

```
SELECT *,(SELECT COUNT(*) FROM leaderboard lb2
WHERE lb2.score >= lb1.score) RANK
FROM leaderboard lb1
WHERE lb1.user_id = {:user_id};
```

##### 使用 Redis 有序集合实现

现在已经知道有序集合很快，下面看看构建排行榜将使用哪些 Redis 操作 \[3\] \[4\] \[5\] \[6\]：

-   ZADD：如果用户尚未存在，则将其插入集合；否则，更新该用户的积分。执行时间复杂度为 _O(log(n))_。
    
-   ZINCRBY：按指定增量增加用户积分。如果用户尚未存在于集合中，则假定其初始积分为 0。执行时间复杂度为 _O(log(n))_。
    
-   ZRANGE/ZREVRANGE：获取按积分排序的一段用户。可以指定顺序（range 或 revrange）、记录数量和起始位置。执行时间复杂度为 _O(log(n)+m)_，其中 _m_ 是要获取的记录数（在本场景中通常很小），_n_ 是有序集合中的记录总数。
    
-   ZRANK/ZREVRANK：以对数时间，获取任意用户按升序或降序排列时的位置。
    

**使用有序集合的工作流程**

1\. 用户获得一分

![图中展示了排行榜服务的简化系统架构，三个主要组件水平排列。左侧标为“排行榜服务”的方框表示负责管理和展示排行榜数据的应用。中间标为“ZINCRBY”的方框表示使用 Redis 命令增加有序集合成员的分数；有序集合是一种非常适合排行榜的数据结构。右侧的 Redis 标志表示排行榜数据存储在 Redis 数据库中。该排列暗示排行榜服务通过 ZINCRBY 命令与 Redis 交互，可能用于更新用户积分。图中没有明确的箭头或连线，但隐含的交互是排行榜服务向 Redis 发送 ZINCRBY 等命令来管理数据，并从 Redis 获取数据以展示排行榜。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Freal-time-gaming-leaderboard%2Ffigure-11-a-user-scores-a-point-XGFZPTLE.png&w=3840&q=75)

图 11 用户获得一分

每个月都会创建一个新的排行榜有序集合，并将之前的排行榜移至历史数据存储。用户每赢一局就得 1 分，因此调用 ZINCRBY，在当月排行榜中将该用户积分增加 1 分；如果用户尚未出现在排行榜集合中，就将其加入。ZINCRBY 的语法如下：

```
ZINCRBY <key> <increment> <user>
```

下面的命令在用户“mary1934”获胜后，为其增加一分。

```
ZINCRBY leaderboard_feb_2021 1 'mary1934'
```

2\. 用户获取全球前 10 名排行榜

![图中展示了排行榜服务与 Redis 数据库之间交互的简化系统架构。左侧标为“排行榜服务”的矩形框表示负责管理和展示排行榜数据的服务。右侧矩形框中显示“ZREVRANGE”，这是 Redis 中用于获取指定范围内、按分数降序排列的有序集合成员的命令。图中虽未明确画出向右箭头，但暗示了请求从排行榜服务流向 Redis 数据库。最右侧的 Redis 标志清楚标明所使用的数据库技术。整体说明，排行榜服务可能使用 `ZREVRANGE` 命令查询 Redis 中保存用户积分的有序集合，以获取和展示排行榜信息。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Freal-time-gaming-leaderboard%2Ffigure-12-fetch-top-10-global-leaderboard-KOUPGTUC.png&w=3840&q=75)

图 12 获取全球前 10 名排行榜

由于需要最高积分，我们调用 ZREVRANGE，按降序获取成员，并传入“WITHSCORES”选项，确保返回最高积分用户集合的同时，也返回每位用户的总积分。下面的命令获取 2021 年 2 月排行榜中的前 10 名玩家。

```
ZREVRANGE leaderboard_feb_2021 0 9 WITHSCORES
```

返回的列表如下：

```
[(user2,score2),(user1,score1),(user5,score5)...]
```

3\. 用户希望获取自己在排行榜中的位置

![图中以简化的系统架构图展示了排行榜服务的数据流。左侧标为“排行榜服务”的方框代表负责管理和展示排行榜数据的应用。该服务与中央标为“ZREVRANK”的组件交互，暗示这是用于获取排名数据的 Redis 命令。右侧 Redis 标志表明 ZREVRANK 对 Redis 数据库执行。整体流程是排行榜服务使用 ZREVRANK 命令查询 Redis，获取并展示排名信息，说明排行榜数据通过 Redis 的有序集合数据结构存储和管理。图中没有可见的具体 URL 或参数。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Freal-time-gaming-leaderboard%2Ffigure-13-fetch-a-users-leaderboard-position-YUJM3G3J.png&w=3840&q=75)

图 13 获取用户在排行榜中的位置

要获取用户在排行榜中的位置，调用 ZREVRANK 即可。这里同样使用命令的 rev 版本，因为我们希望按积分从高到低排序。

```
ZREVRANK leaderboard_feb_2021 'mary1934'
```

4\. 获取用户在排行榜中的相对位置。图 14 展示了一个例子。

![图中展示了游戏或比赛中玩家的排行榜，采用包含“排名”“玩家”和“积分”三列的表格。每行代表一名玩家，展示其名次（表示所在位置的数字标识）、玩家名称（可包含字母、数字、空格和特殊符号的字符串）及积分（数值分数）。各行按积分降序排列，最高分玩家位于顶部。“Mallow007”所在行以浅绿色背景突出显示，可能表示当前关注或选中的玩家。图中没有 URL 或参数。数据全部为文字，除浅粉色表头和浅绿色高亮行外，没有其他视觉提示。](https://bytebytego.com/images/courses/system-design-interview/real-time-gaming-leaderboard/figure-14-fetch-4-players-above-and-below-DNN7J5ML.svg)

图 14 获取前后各 4 名玩家

虽然这不是明确要求，但只需对 ZREVRANGE 指定目标玩家前后需要返回的结果数量，就能轻松获取用户的相对位置。例如，用户 Mallow007 的排名为 361，若希望获取其前后各 4 名玩家，则执行以下命令。

```
ZREVRANGE leaderboard_feb_2021 357 365
```

##### 存储需求

至少需要存储用户 ID 和积分。最坏情况下，2500 万月活跃用户都至少赢过一局，都在当月排行榜中有记录。假设 ID 是一个 24 字符的字符串，积分是 16 位整数（即 2 字节），则每条排行榜记录需要 26 字节。按每位 MAU 对应一条排行榜记录的最坏情况计算，Redis 缓存中的排行榜存储需要 26 字节 \* 2500 万 = 6.5 亿字节，约为 650 MB。即使将有序集合的跳表和哈希表开销考虑在内，把内存用量翻倍，一台现代 Redis 服务器也足以容纳这些数据。

另一个相关因素是 CPU 和 I/O 用量。粗略估算得到的峰值 QPS 为每秒 2500 次更新，完全在单台 Redis 服务器的性能范围内。

Redis 缓存的一个问题是持久化，因为 Redis 节点可能故障。幸运的是，Redis 支持持久化，但从磁盘重启大型 Redis 实例较慢。通常会为 Redis 配置一个只读副本，主实例故障时，将只读副本提升为主实例，并挂接一个新的只读副本。

此外，还需要在 MySQL 这样的关系型数据库中建立两张辅助表：user 和 point。user 表存储用户 ID 和展示名称（实际应用中还会包含更多数据）；point 表存储用户 ID、积分和获胜时的时间戳。这些数据可以用于对局历史等其他游戏功能，也可以在基础设施故障时重建 Redis 排行榜。

还可以做一个小的性能优化：额外缓存用户详情，尤其是访问最频繁的前 10 名玩家。不过，这部分数据量并不大。

### 第 3 步：深入设计

讨论完总体设计后，接下来深入研究以下内容：

-   是否使用云服务提供商
    
    -   自行管理服务
        
    -   使用 Amazon Web Services（AWS）等云服务提供商
    
-   扩展 Redis
    
-   备选方案：NoSQL
    
-   其他考虑因素
    

#### 是否使用云服务提供商

根据现有基础设施，部署方案通常有两种选择。下面分别讨论。

##### 自行管理服务

在这种方式下，每个月创建一个排行榜有序集合，存储该时段的排行榜数据。有序集合保存成员和积分信息。用户的其他详情，例如姓名和头像，则存储在 MySQL 数据库中。获取排行榜时，API 服务器除了获取排行榜数据，还会查询数据库，取得相应用户的姓名和头像，以便展示。如果长期来看效率不够高，可以引入用户资料缓存，保存前 10 名玩家的详细信息。设计如图 15 所示。

![图中展示了排行榜应用的系统架构。用户以人物和智能手机图标表示，通过一组经过负载均衡的 Web 服务器与系统交互；这组服务器用一个带有三个下级方框的层次结构表示。Web 服务器连接 MySQL 数据库，后者存储用户资料及其对应的积分。此外，一个 Redis 实例用于缓存前 10 名用户的资料，以加快获取速度、提高性能。另一个独立的 Redis 实例以有序集合形式存储排行榜数据，从而高效地进行排名和获取排行榜信息。数据从用户设备流向 Web 服务器，服务器再与保存用户资料的 MySQL 数据库，以及分别负责缓存和排行榜数据的 Redis 实例交互。排行榜数据专门以 Redis 有序集合存储，针对高效排名操作进行了优化。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Freal-time-gaming-leaderboard%2Ffigure-15-manage-our-own-services-T3VHGXIJ.png&w=3840&q=75)

图 15 自行管理服务

##### 基于云构建

第二种方式是使用云基础设施。本节假设现有基础设施构建在 AWS 上，因此在云端构建排行榜是自然的选择。本设计主要使用两项 AWS 技术：Amazon API Gateway 和 AWS Lambda 函数 \[7\]。Amazon API Gateway 可以定义 RESTful API 的 HTTP 端点，并将其连接到任意后端服务。我们用它连接 AWS Lambda 函数。RESTful API 与 Lambda 函数的映射关系如表 5 所示。

| **API**                   | **Lambda 函数**        |
| -------------------------- | -------------------------- |
| GET /v1/scores             | LeaderboardFetchTop10      |
| GET /v1/scores/{:user\_id} | LeaderboardFetchPlayerRank |
| POST /v1/scores            | LeaderboardUpdateScore     |

表 5 Lambda 函数

AWS Lambda 是最流行的无服务器计算平台之一。它使我们无须自行预置或管理服务器就能运行代码。它仅在需要时运行，并根据流量自动扩展。无服务器计算是云服务领域的热门话题，各大云服务提供商均予以支持。例如，Google Cloud 提供 Google Cloud Functions \[8\]，Microsoft 则将其产品命名为 Microsoft Azure Functions \[9\]。

总体而言，游戏调用 Amazon API Gateway，再由后者调用适当的 Lambda 函数。我们使用 AWS Lambda 函数在存储层（Redis 和 MySQL）执行相应命令，将结果返回 API Gateway，再返回应用。

借助 Lambda 函数，可以执行所需查询，无须启动服务器实例。AWS 支持可以从 Lambda 函数中调用的 Redis 客户端。这种方式还能随着 DAU 增长按需自动扩展。下面分别展示用户得分和获取排行榜的设计图：

**用例 1：获得一分**

![图中以系统架构图展示了游戏获胜后更新排行榜的过程。流程起点是“排行榜服务”方框，表示负责维护排行榜数据的系统。“赢得游戏”方框表示触发更新的事件。该事件流向用橙色 Lambda 标志表示的 AWS Lambda 函数，后者作为处理单元。Lambda 函数随后向 Redis 标志表示的实例发出“ZINCRBY”命令，原子地增加玩家积分。“插入积分”标注说明了积分更新应用于 Redis 数据库的位置。最后，更新后的积分可能会持久化到 MySQL 标志表示的数据库中，供长期保存，但图中没有用箭头明确展示这一连接。整体流程表示，游戏获胜触发 Redis 中玩家积分的更新，随后可能在 MySQL 中持久化，整个过程由 AWS Lambda 函数管理。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Freal-time-gaming-leaderboard%2Ffigure-16-score-a-point-TJAYZUVE.png&w=3840&q=75)

图 16 获得一分

**用例 2：获取排行榜**

![图中展示了排行榜服务的系统架构。“排行榜服务”方框发起“获取排行榜”请求，并发送给橙色 Lambda 标志表示的 AWS Lambda 函数。Lambda 函数随后发起两次调用：①向 Redis 标志表示的数据库发送“调用 ZREVRANGE”，获取排行榜数据；②向 MySQL 标志表示的数据库发送“获取用户详情”，读取额外的用户信息。数据从 Redis 和 MySQL 返回 Lambda 函数，后者可能会处理并汇总数据，再将完整排行榜信息返回排行榜服务。整体展示了典型的排行榜系统架构，使用 Redis 快速获取数据，并用 MySQL 持久保存用户详情。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Freal-time-gaming-leaderboard%2Ffigure-17-retrieve-leaderboard-4UMUHLST.png&w=3840&q=75)

图 17 获取排行榜

Lambda 的优势在于采用无服务器方式，基础设施会按需自动扩展函数。这意味着我们无须管理扩缩容、环境配置和维护。因此，如果从零开始构建游戏，建议采用无服务器方式。

#### 扩展 Redis

在 500 万 DAU 的规模下，无论存储容量还是 QPS，一套 Redis 缓存都足以应对。不过，假设 DAU 达到 5 亿，是原来规模的 100 倍。最坏情况下，排行榜大小将增至 65 GB（650 MB \*100），QPS 将增至每秒 250,000 次查询（2,500 \* 100）。这时就需要分片方案。

##### 数据分片

我们考虑采用以下两种方式之一进行分片：固定分区或哈希分区。

**固定分区**

理解固定分区的一种方法，是考察排行榜的整体积分范围。假设一个月内获得的积分范围是 1 到 1000，可以按范围拆分数据。例如，使用 10 个分片，每个分片包含 100 个积分值（例如 1-100、101-200、201-300 等），如图 18 所示。

![图中有四个水平相连的矩形框，每个框代表一个有序的数字集合。第一个框包含有序集合“[1, 100]”，第二个为“[101, 200]”，第三个为“[201, 300]”，最后一个为“[901, 1000]”。每个框都在数值范围下方标为“有序集合”。方框之间的直线表示顺序数据流。第三个与第四个方框之间的省略号“...”表示，为简洁起见省略了其他类似方框，暗示每个范围包含 100 个数字的模式继续延伸。整体展示了一种分区或分片策略，将大型有序数据集拆分为较小、易于管理的有序子集。](https://bytebytego.com/images/courses/system-design-interview/real-time-gaming-leaderboard/figure-18-fixed-partition-2DM4XVL6.svg)

图 18 固定分区

要使这种方式有效，需要确保积分在排行榜中均匀分布。否则，就需要调整各分片的积分范围，以保持相对均匀的分布。这种方式由应用代码自行对数据进行分片。

插入或更新用户积分时，需要知道用户位于哪个分片。可以从 MySQL 数据库计算用户的当前积分。这样可行，但性能更好的选择是再建立一个缓存，保存用户 ID 到积分的映射。用户积分增加、需要在分片间移动时，必须谨慎处理：将用户从当前分片移除，再移动到新的分片。

要获取排行榜前 10 名玩家，可以从积分最高的分片（有序集合）中获取前 10 名。图 18 中，积分范围为 \[901, 1000\] 的最后一个分片包含前 10 名玩家。

要获取用户排名，需要计算其在当前分片中的排名（局部排名），再加上其他分片中积分更高的玩家总数。注意，执行“_info keyspace_”命令，可以在 _O(1)_ 时间内取得分片中的玩家总数 \[10\]。

##### 哈希分区

第二种方式是使用 Redis Cluster。当积分分布高度集中时，这种方式很有吸引力。Redis Cluster 可以将数据自动分片到多个 Redis 节点。它不使用一致性哈希，而是采用另一种分片方式：每个键都属于一个**哈希槽（hash slot）**。一共有 16384 个哈希槽 \[11\]，可以通过 _CRC16(_key_) % 16384_ 计算给定键所在的哈希槽 \[12\]。这样，在集群中增加或移除节点时，无须重新分配所有键。图 19 中有 3 个节点，其中：

-   第一个节点包含哈希槽 \[0, 5500\]。
    
-   第二个节点包含哈希槽 \[5501, 11000\]。
    
-   第三个节点包含哈希槽 \[11001, 16383\]。
    

![图中展示了 Redis Cluster 架构。顶部“客户端”连接“代理”，代理再根据槽范围将请求转发到三个 Redis 分片之一。每个分片都用虚线框表示，内部包含一个主节点和两个副本节点。主节点以带星标的红色三层立方体表示，副本节点采用类似的红色立方体。分片 1 的槽范围为 [0, 5500]，分片 2 为 [5501, 11000]，分片 3 为 [11001, 16383]。各分片的主节点负责处理写入，副本提供读取可用性和冗余。箭头表示数据从客户端流向代理，再由代理流向相应分片的主节点。三个分片下方标有“Redis Cluster”，代表整个系统。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Freal-time-gaming-leaderboard%2Ffigure-19-hash-partition-BNBRIQB7.png&w=3840&q=75)

图 19 哈希分区

更新操作只需修改相应分片中的用户积分，分片由 _CRC16(key)%16384_ 确定。获取排行榜前 10 名则更复杂：需要从每个分片中取出前 10 名，再由应用对数据排序。图 20 展示了一个具体例子。可以并行执行这些查询，以降低延迟。

![图中展示了采用分散—汇集（scatter-gather）方式的数据处理流程。三张表分别标为“分片 0（前 10 名）”“分片 1（前 10 名）”和“分片 2（前 10 名）”，各自包含一部分数据，分为“score”（数值）和“member”（字母数字字符串，推测为用户 ID）两列。每个分片都展示按积分计算的前 10 条记录。标为“scatter-gather”的箭头表示，三个分片表中的数据被发送到中央的“前 10 名”表。该表按积分汇总所有分片的前 10 条记录，生成包含前 10 名成员及对应积分的合并列表。“前 10 名”表同样包含 score 和 member 两列，与分片表结构一致。这个过程体现了分布式系统先将数据划分到多个分片，再合并产生整体前 10 名排行榜的方式。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Freal-time-gaming-leaderboard%2Ffigure-20-scatter-gather-KXBJYDNB.png&w=3840&q=75)

图 20 分散—汇集

这种方式有几个局限：

-   当需要返回排行榜前 K 名、且 K 非常大时，延迟会很高，因为各分片会返回大量记录，随后还需要排序。
    
-   分区数量很多时，延迟会升高，因为查询必须等待最慢的分区。
    
-   另一个问题是，这种方式没有提供一种直接确定特定用户排名的方法。
    

因此，我们倾向于第一种方案：固定分区。

##### 确定 Redis 节点规格

确定 Redis 节点规格时，需要考虑多方面因素 \[13\]。写密集型应用需要更多可用内存，因为在创建用于故障恢复的快照时，还需要能够容纳所有写入。为稳妥起见，应为写密集型应用分配两倍内存。

Redis 提供名为 Redis-benchmark 的工具，可以通过模拟多个客户端执行多个查询，对 Redis 部署的性能进行基准测试，并返回给定硬件条件下每秒能够处理的请求数。有关 Redis-benchmark 的更多信息，请参阅 \[14\]。

#### 备选方案：NoSQL

另一个可以考虑的方案是 NoSQL 数据库。应该使用哪类 NoSQL 数据库？理想情况下，希望它具备以下特性：

-   针对写入进行优化。
    
-   能够在同一分区内按积分高效排序条目。
    

Amazon DynamoDB \[15\]、Cassandra、MongoDB 等 NoSQL 数据库都可能适合。本章以 DynamoDB 为例。DynamoDB 是全托管的 NoSQL 数据库，提供稳定的性能和良好的可扩展性。为了通过主键以外的属性高效访问数据，可以使用 DynamoDB 的全局二级索引 \[16\]。全局二级索引包含原表中的部分属性，但使用不同的主键来组织。下面来看一个例子。

图 21 展示了更新后的系统架构图。Redis 和 MySQL 被 DynamoDB 替代。

![图中展示了排行榜服务的简化架构，四个组件水平排列。最左侧标为“排行榜服务”的白色方框表示负责管理和展示排行榜数据的服务。接下来是一个金橙色风格化图标，代表 Amazon Elastic Container Service（ECS），暗示排行榜服务部署在这一容器编排平台上。后面是代表 AWS Lambda 的橙色图标，表示排行榜服务中的某些任务使用无服务器函数，可能用于处理更新或请求。最右侧的蓝色圆柱图标代表数据库，可能保存积分、用户 ID、时间戳等排行榜数据。组件间没有明确画出连线，但隐含流程是：运行在 ECS 上并使用 Lambda 函数的排行榜服务与数据库交互，读写排行榜信息。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Freal-time-gaming-leaderboard%2Ffigure-21-dynamodb-solution-QSI6N2FT.png&w=3840&q=75)

图 21 DynamoDB 方案

假设我们为国际象棋游戏设计排行榜，初始表如图 22 所示。它是 leaderboard 表和 user 表的反规范化视图，包含展示排行榜所需的全部信息。

![图中展示了一张数据库结构表，或某张表中的部分数据。表分为“主键”和“属性”两大部分。主键列列出唯一标识符，具体为 `user_id` 值，例如 `lovelove`、`i_love_tofu`、`golden_gate`、`pizza_or_bread` 和 `ocean`，省略号（...）表示还有更多记录。属性部分包含 `score`（用户积分数值）、`email`（用户电子邮箱）、`profile_pic`（指向 CDN 上头像的 URL，例如 `https://cdn.example/3.png`）和 `leaderboard_name`（可能表示用户所属的排行榜，例如 `chess#2020-02`）。每行代表一个用户记录，`user_id` 作为主键，将所有属性关联到特定用户。数据中的邮箱统一采用 `@test.com` 域名，排行榜名称均以“chess”开头，后接日期。底部省略号（...）表示表中还有未展示的行。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Freal-time-gaming-leaderboard%2Ffigure-22-denormalized-view-of-the-leaderboard-and-user-tables-PGST2M7Z.png&w=3840&q=75)

图 22 leaderboard 表和 user 表的反规范化视图

这种表结构可以工作，但扩展性不佳。随着行数增加，需要扫描整张表才能找到最高积分。

为避免线性扫描，需要添加索引。第一次尝试是使用“_game\_name#{year-month}_”作为分区键，使用积分作为排序键，如图 23 所示。

![图中展示了一张包含五列的全局二级索引表。第一列标为“分区键（PK）”，其中的分区键字符串都以“chess#”开头，后接 YYYY-MM 格式的日期，例如“chess#2020-02”。第二列“排序键（score）”包含整数值。第三列“user_id”是字符串形式的唯一用户标识符，例如“lovelove”“i_love_tofu”。第四列“email”包含对应用户的电子邮箱。第五列“profile_pic”列出指向内容分发网络（CDN）上头像的 URL，均以“https://cdn.example/”开头，后接“3.png”“p.png”等字符串。每行代表索引中的一条记录，将分区键、积分、用户 ID、电子邮箱和头像 URL 关联起来。表格末尾有省略号（...），表示还有未展示的行。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Freal-time-gaming-leaderboard%2Ffigure-23-partition-key-and-sort-key-NMM4PK4Z.png&w=3840&q=75)

图 23 分区键与排序键

这种方式可以工作，但在高负载下会出现问题。DynamoDB 使用一致性哈希将数据分布到多个节点上，每个条目根据其分区键存放到对应节点。我们希望组织数据时，使数据均匀分布在各个分区中。而在图 23 的表设计中，最近一个月的所有数据都会存储在同一个分区，这就形成了热点分区。如何解决？

可以将数据拆分到 _N_ 个分区，并在分区键后面追加一个分区编号（user\_id % number\_of\_partitions）。这种模式称为写分片。写分片会增加读写操作的复杂度，因此需要仔细权衡。

第二个需要回答的问题是，应当使用多少个分区？可以根据写入量或 DAU 确定。需要记住，分区负载与读取复杂度之间存在权衡。同一个月的数据均匀分散到多个分区后，单个分区的负载大幅降低；但是，要读取某个月的数据，就必须查询所有分区并合并结果，这会增加读取复杂度。

分区键的形式类似于“_game\_name#{year-month}#p{partition\_number}_”。图 24 展示了更新后的表结构。

![图中展示了一张全局二级索引表，共五列。第一列标为“分区键（PK）”，包含格式为“chess#YYYY-MM#pX”的唯一标识符，其中 YYYY-MM 表示年月，pX 表示分区编号。第二列“排序键（score）”包含数值积分，看起来与相应分区键关联。其余三列统称“属性”，列出用户信息：“user_id”（唯一用户名）、“email”（用户电子邮箱）和“profile_pic”（指向 CDN“cdn.example”上用户头像的 URL）。表中的每行代表一条数据记录，将特定分区键和积分关联到用户 ID、电子邮箱及头像 URL。表中展示了若干示例记录，并通过“...”表示还有更多未显示的数据。这些数据体现了一种按积分对用户数据进行分区和排序的系统，便于同时按分区键和积分高效获取数据。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Freal-time-gaming-leaderboard%2Ffigure-24-updated-partition-key-WDZGBZGP.png&w=3840&q=75)

图 24 更新后的分区键

全局二级索引使用“_game\_name#{year-month}#p{partition\_number}_”作为分区键，以积分作为排序键。最终得到 _N_ 个分区，每个分区内部有序，即局部有序。假设有 3 个分区，要获取前 10 名排行榜，可以使用前面提到的“分散—汇集”方式。从每个分区中取出前 10 条结果，这是“分散”部分；随后由应用对所有分区的结果排序，这是“汇集”部分。图 25 展示了一个例子。

![图中展示了从三个数据分区生成前 10 名排行榜的系统。三张表分别标为“分区 0 的前 10 名（分散）”“分区 1 的前 10 名（分散）”和“分区 2 的前 10 名（分散）”，各自包含对应分区的前 10 条记录。每张表都有“分区键（PK）”“排序键（score）”和“user_id”三列。分区键列包含各分区的唯一标识符，例如“chess#2020-02#p0”；排序键列包含数值积分，user_id 列包含用户标识符。标为“汇集”的箭头从这三张表连接到最终的“前 10 名排行榜”，后者包含 score 和 user_id 两列。汇集操作表示将三个源表的数据合并，再按 score 列排序，生成最终的前 10 名排行榜。最终排行榜展示从三个分区汇总而来的、按积分计算的前 10 名用户。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Freal-time-gaming-leaderboard%2Ffigure-25-scatter-gather-TTZSXIYA.png&w=3840&q=75)

图 25 分散—汇集

如何确定分区数量？这可能需要仔细进行基准测试。更多分区可以减轻每个分区的负载，但也会增加复杂度，因为构建最终排行榜时，需要向更多分区分散查询。通过基准测试，可以更清楚地看到其中的权衡。

不过，与前面提到的 Redis 分区方案类似，这种方式无法直接确定用户的相对名次。但可以得到用户位置的百分位，而这可能已经足够。实际中，告诉玩家自己位于前 10-20%，可能比显示 1,200,001 这样的精确名次更好。因此，如果规模大到需要分片，可以假设所有分片的积分分布大致相同。如果这一假设成立，就可以创建定时任务，分析各分片的积分分布，并缓存结果。

结果可能如下所示：

```
10th percentile = score < 100
20th percentile = score < 500
...
90th percentile = score < 6500
```

这样，就可以快速返回用户的相对排名，例如第 90 百分位。

### 第 4 步：总结

本章设计了一种支持数百万 DAU 的实时游戏排行榜方案。我们先研究了使用 MySQL 数据库的直接方案，但由于它无法扩展到数百万用户，因此将其排除。随后，使用 Redis 有序集合设计排行榜，并进一步研究了如何将数据分片到不同的 Redis 缓存，把方案扩展到 5 亿 DAU。我们还提出了一种基于 NoSQL 的备选方案。

如果面试结束前还有时间，可以再讨论以下几个话题：

**加快获取速度与打破平局**

Redis Hash 提供字符串字段与值之间的映射。可以在以下两种场景中使用哈希：

1.  存储用户 ID 到用户对象的映射，以便在排行榜中展示。这样可以更快地获取用户对象，无须访问数据库。
    
2.  两位玩家积分相同时，可以根据谁先获得该积分来排名。增加用户积分时，同时保存用户 ID 到最近一次获胜时间戳的映射。平分情况下，时间戳较早的用户排名更高。
    

**系统故障恢复**

Redis 集群可能发生大规模故障。根据前面的设计，MySQL 数据库会在用户每次获胜时记录一条带时间戳的记录，可以利用这一点编写脚本。遍历每位用户的全部记录，对每位用户的每条记录调用一次 ZINCRBY。这样，在发生大规模故障时，就能按需离线重建排行榜。

恭喜你读到这里！现在给自己一点鼓励吧，做得很好！

## 本章小结

![图中以思维导图概述了游戏排行榜系统的设计过程。中心节点为“游戏排行榜”，向外分为第 1、2、3、4 步。第 1 步聚焦需求，分为功能需求（展示前 10 名、显示用户排名、显示目标用户前后各四个名次）和非功能需求（实时更新、可扩展）。第 1 步还包含估算，列明预期日活跃用户为 500 万 DAU、更新积分的每秒查询数为 2.5k。第 2 步涉及 API 设计（得分、获取排行榜、获取玩家排名）和总体设计，其中考虑关系型数据库方案和 Redis 方案两种数据模型。第 3 步研究部署方式（是否基于云构建）和扩展策略（扩展 Redis 或采用 NoSQL 备选方案）。最后，第 4 步涉及性能优化（更快获取数据及打破平局）和系统可靠性（系统故障恢复）。整幅图体现了设计过程从初始需求和估算，到总体设计选择、数据建模、扩展策略以及系统稳健性考虑的层次化拆分。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Freal-time-gaming-leaderboard%2Fchapter-summary-DXV7IGUO.png&w=3840&q=75)

## 参考资料

\[1\] Redis 有序集合源代码：

[<u>https://github.com/redis/redis/blob/unstable/src/t_zset.c</u>](https://github.com/redis/redis/blob/unstable/src/t_zset.c)

\[2\] 极客时间：

[<u>https://static001.geekbang.org/resource/image/46/a9/46d283cd82c987153b3fe0c76dfba8a9.jpg</u>](https://static001.geekbang.org/resource/image/46/a9/46d283cd82c987153b3fe0c76dfba8a9.jpg)

\[3\] 使用 Redis 构建实时排行榜：

[<u>https://medium.com/@sandeep4.verma/building-real-time-leaderboard-with-redis-82c98aa47b9f</u>](https://medium.com/@sandeep4.verma/building-real-time-leaderboard-with-redis-82c98aa47b9f)

\[4\] 使用 Amazon ElastiCache for Redis 构建实时游戏排行榜：

[<u>https://aws.amazon.com/blogs/database/building-a-real-time-gaming-leaderboard-with-amazon-elasticache-for-redis</u>](https://aws.amazon.com/blogs/database/building-a-real-time-gaming-leaderboard-with-amazon-elasticache-for-redis)

\[5\] 我们如何为百万用户创建实时排行榜：

[<u>https://levelup.gitconnected.com/how-we-created-a-real-time-leaderboard-for-a-million-users-555aaa3ccf7b</u>](https://levelup.gitconnected.com/how-we-created-a-real-time-leaderboard-for-a-million-users-555aaa3ccf7b)

\[6\] 排行榜：[<u>https://redislabs.com/solutions/use-cases/leaderboards</u>](https://redislabs.com/solutions/use-cases/leaderboards)/

\[7\] Lambda: [<u>https://aws.amazon.com/lambda/</u>](https://aws.amazon.com/lambda/)

\[8\] Google Cloud Functions: [<u>https://cloud.google.com/functions</u>](https://cloud.google.com/functions)

\[9\] Azure Functions: [<u>https://azure.microsoft.com/en-us/services/functions/</u>](https://azure.microsoft.com/en-us/services/functions/)

\[10\] Info 命令：[<u>https://redis.io/commands/INFO</u>](https://redis.io/commands/INFO)

\[11\] 为什么 Redis Cluster 只有 16384 个槽：

[<u>https://stackoverflow.com/questions/36203532/why-redis-cluster-only-have-16384-slots</u>](https://stackoverflow.com/questions/36203532/why-redis-cluster-only-have-16384-slots)

\[12\] 循环冗余校验：[<u>https://en.wikipedia.org/wiki/Cyclic_redundancy_check</u>](https://en.wikipedia.org/wiki/Cyclic_redundancy_check)

\[13\] 选择节点规格：

[<u>https://docs.aws.amazon.com/AmazonElastiCache/latest/red-ug/nodes-select-size.html</u>](https://docs.aws.amazon.com/AmazonElastiCache/latest/red-ug/nodes-select-size.html)

\[14\] Redis 有多快？[<u>https://redis.io/topics/benchmarks</u>](https://redis.io/topics/benchmarks)

\[15\] 在 DynamoDB 中使用全局二级索引：

[<u>https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/GSI.html</u>](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/GSI.html)~.~

\[16\] 排行榜与写分片：[<u>https://www.dynamodbguide.com/leaderboard-write-sharding/</u>](https://www.dynamodbguide.com/leaderboard-write-sharding/)

****

