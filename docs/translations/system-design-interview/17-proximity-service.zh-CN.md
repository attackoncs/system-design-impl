# 17 邻近服务

本章将设计一个邻近服务。邻近服务用于发现附近的餐厅、酒店、剧院、博物馆等地点，是 Yelp 上查找附近最佳餐厅、Google Maps 上查找最近的 k 个加油站等功能的核心组件。图 1 展示了在 Yelp 上搜索附近餐厅的用户界面 \[1\]。请注意，本章使用的地图瓦片来自 Stamen Design \[2\]，地图数据来自 OpenStreetMap \[3\]。

![图中展示了一个搜索结果页面，可能来自 Google Maps 或 Yelp 这样的基于位置的服务。顶部搜索栏包含查询词“tacos, cheap dinner, Max's”，以及位置“1355 Market St, San Francisco, CA”。下方横向导航栏提供“餐厅”“家居服务”“汽车服务”和“更多”选项。左侧是距离筛选器，单选项包括“鸟瞰视图”“驾车（5 mi.）”“骑车（2 mi.）”“步行（1 mi.）”和“4 个街区以内”。中央区域展示三家餐厅 Manila Bowl、WingSlut 和 Matko，每项都包含图片、星级评分、菜系、价格区间、所在街区 SoMa、距离 0.01 英里，以及一段用户评价摘要，并提供“更多”链接查看完整评价。每家餐厅还配有标示位置的小地图图标。右侧地图以旧金山市 1355 Market St 附近为中心，带编号的标记对应若干地点，可能就是列出的餐厅，街道名称清晰可见。地图还显示交通信号灯和公共交通符号等视觉提示。整体采用典型的搜索结果布局，将文字信息与图片、地图等视觉辅助结合起来，向用户展示相关信息。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fproximity-service%2Ffigure-1-neary-search-on-yelp-PIX55ZBV.png&w=3840&q=75)

图 1 Yelp 上的附近搜索（来源：\[1\]）

## 第 1 步：理解问题并确定设计范围

Yelp 支持许多功能，不可能在一次面试中全部设计出来，因此需要通过提问缩小范围。面试官与候选人的交流可能如下：

**候选人**：用户能否指定搜索半径？如果搜索半径内的商家数量不足，系统是否扩大搜索范围？

**面试官**：这个问题很好。假设只关心指定半径内的商家。如果时间允许，再讨论半径内商家不足时如何扩大搜索范围。

**候选人**：允许的最大半径是多少？可以假设为 20 km（12.5 英里）吗？

**面试官**：这个假设很合理。

**候选人**：用户可以在界面上修改搜索半径吗？

**面试官**：可以，有以下选项：0.5km（0.31 英里）、1km（0.62 英里）、2km（1.24 英里）、5km（3.1 英里）和 20km（12.42 英里）。

**候选人**：如何添加、删除或更新商家信息？这些操作需要实时生效吗？

**面试官**：商家可以添加、删除或更新自己的商家信息。假设事先已经约定，新增或更新的信息将在第二天生效。

**候选人**：用户使用应用或网站时可能在移动，因此过一段时间后搜索结果可能略有不同。是否需要刷新页面，以保持结果最新？

**面试官**：假设用户移动速度较慢，无须不断刷新页面。

### 功能需求

根据上述对话，我们聚焦以下 3 项核心功能：

-   根据用户位置（纬度和经度）及半径，返回范围内的所有商家。
    
-   商家可以添加、删除或更新商家信息，但这些变化无须实时体现。
    
-   用户可以查看商家的详细信息。
    

### 非功能需求

根据业务需求，可以推导出一组非功能需求。这些需求也应与面试官确认。

-   低延迟。用户应该能够快速看到附近的商家。
    
-   数据隐私。位置信息属于敏感数据。设计基于位置的服务（location-based service，LBS）时，应始终考虑用户隐私。我们需要遵守《通用数据保护条例》（GDPR）\[4\]、《加利福尼亚州消费者隐私法案》（CCPA）\[5\] 等数据隐私法律。
    
-   高可用性和可扩展性。应确保系统能够应对人口密集地区高峰时段的流量激增。
    

### 粗略估算

下面做一些粗略计算，确定方案可能面临的规模和挑战。假设有 1 亿日活跃用户和 2 亿家商家。

| 计算 QPS                                                |
| ------------------------------------------------------------ |
| -   > 一天的秒数 = 24 \* 60 \* 60 = 86,400。为方便计算，可以向上取整为 10^5。**本书始终使用 10^5** 表示一天的秒数。 |

-   > 假设用户每天进行 5 次搜索。
    
-   > Search QPS = 100 million \* 5 / 10^5 = 5,000|

## 第 2 步：提出总体设计并达成共识

本节将讨论以下内容：

-   API 设计
    
-   总体设计
    
-   查找附近商家的算法
    
-   数据模型
    

### API 设计

我们遵循 RESTful API 的惯例，设计一组简化的 API。

**GET /v1/search/nearby**

该端点根据指定搜索条件返回商家。在实际应用中，搜索结果通常需要分页。分页 \[6\] 不是本章重点，但在面试时值得提及。

请求参数：

| **字段** | **说明**                                  | **类型** |
| --------- | ------------------------------------------------ | -------- |
| latitude  | 给定位置的纬度                     | decimal  |
| longitude | 给定位置的经度                    | decimal  |
| radius    | 可选。默认为 5000 米（约 3 英里） | int      |

表 1 请求参数

响应体

```
{
  "total": 10,
  "businesses":[{business object}]
}
```

business 对象包含渲染搜索结果页所需的全部信息，但渲染商家详情页时，可能还需要图片、评价、星级评分等额外属性。因此，用户点击进入商家详情页时，通常需要调用另一个端点，获取该商家的详细信息。

**商家相关 API**

与 business 对象相关的 API 如下表所示。

| **API**                     | **说明**                                   |
| --------------------------- | -------------------------------------------- |
| GET /v1/businesses/{:id}    | 返回商家的详细信息 |
| POST /v1/businesses         | 添加商家                               |
| PUT /v1/businesses/{:id}    | 更新商家的详细信息                 |
| DELETE /v1/businesses/{:id} | 删除商家                            |

表 2 商家相关 API

如果对实际应用中的地点或商家搜索 API 感兴趣，可以参考 Google Places API \[7\] 和 Yelp Reservation API \[8\] 这两个例子。

### 数据模型

本节讨论读写比例和模式设计，数据库的可扩展性将在深入设计部分介绍。

#### 读写比例

读取量较高，因为以下两项功能使用得非常频繁：

-   搜索附近商家。
    
-   查看商家详细信息。
    

相比之下，写入量较低，因为添加、删除和编辑商家信息并不频繁。

对于读密集型系统，MySQL 这样的关系数据库可能是不错的选择。下面进一步看看模式设计。

#### 数据模式

关键数据库表包括 business 表和地理空间索引（geo index）表。

**business 表**

business 表包含商家的详细信息，如表 3 所示，主键为 _business\_id_。

![图中展示了名为“business”的关系数据库表结构，以矩形网格表示，灰色表头标为“business”。第一列列出属性名：`business_id`、`address`、`city`、`state`、`country`、`latitude` 和 `longitude`。`business_id` 对应的第二列写有“PK”，表明 `business_id` 是表的主键，用于唯一标识每条商家记录。其余列为空，代表各属性数据的存储字段。图中没有展示与其他表的关联，只说明 business 表本身的结构。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fproximity-service%2Ftable-3-business-table-72LHQZZP.png&w=3840&q=75)

表 3 business 表

**地理空间索引表**

地理空间索引表用于高效处理空间操作。由于理解该表需要一些地理哈希知识，我们将在深入设计中的“扩展数据库”一节讨论。

### 总体设计

图 2 展示了总体设计。系统分为基于位置的服务（LBS）和商家相关服务两部分。下面逐一介绍各个组件。

![图中展示了商家服务的系统架构。用户通过负载均衡器发起请求，负载均衡器根据端点将请求路由到基于位置的服务（LBS），或商家服务：前者使用 `/search/nearby`，后者使用 `/businesses/{:id}`。LBS 处理只读请求，将请求分发到三个标为“Replica”的数据库副本节点。这些副本通过复制从粉色的“Primary”主数据库节点接收数据。商家服务处理写操作，仅将其发送给主数据库节点。主数据库再把写入的数据复制到三个副本节点，确保数据库集群内的数据一致性。箭头表示数据流方向，“读取”“写入”和“复制”等标注说明了操作类型。](https://bytebytego.com/images/courses/system-design-interview/proximity-service/figure-2-high-level-design-OLWE4F2G.svg)

图 2 总体设计

**负载均衡器**

负载均衡器自动将传入流量分发到多个服务。公司通常提供一个统一的 DNS 入口，再在内部根据 URL 路径，将 API 调用路由到相应服务。

**基于位置的服务（LBS）**

LBS 是系统的核心，负责根据给定的半径和位置查找附近商家。LBS 具有以下特点：

-   读取密集，没有写请求。
    
-   QPS 较高，在人口密集地区的高峰时段尤其如此。
    
-   服务无状态，因此易于水平扩展。
    

**商家服务**

商家服务主要处理两类请求：

-   商家创建、更新或删除商家信息。这类请求主要是写操作，QPS 不高。
    
-   用户查看商家详细信息。高峰时段的 QPS 较高。
    

**数据库集群**

数据库集群可以采用主从配置。在这种配置中，主数据库处理所有写操作，多个副本负责读操作。数据先保存到主数据库，再复制到各副本。由于复制存在延迟，LBS 读取的数据与主数据库上写入的数据可能略有差异。由于商家信息无须实时更新，这种不一致通常不会造成问题。

**商家服务和 LBS 的可扩展性**

商家服务和 LBS 都是无状态服务，因此可以自动增加服务器来应对高峰流量（例如用餐时段），并在低峰时段（例如睡眠时间）移除服务器。如果系统运行在云上，还可以部署到不同区域和可用区，进一步提高可用性 \[9\]。深入设计部分将进一步讨论。

### 查找附近商家的算法

实际中，公司可能使用现成的地理空间数据库，例如 Redis 中的地理哈希（Geohash）\[10\]，或带有 PostGIS 扩展的 Postgres \[11\]。面试时并不要求你了解这些地理空间数据库的内部实现。与其只罗列数据库名称，不如解释地理空间索引的工作方式，展示解决问题的能力和技术知识。

接下来探索获取附近商家的不同方案。我们将列出几种选择，梳理思考过程，并讨论其中的权衡。

#### 方案 1：二维搜索

获取附近商家最直观、但比较朴素的方法，是以预设半径画一个圆，找出圆内的所有商家，如图 3 所示。

![图中展示了一块以“我”为中心的地图区域，包含城市网格中的部分街道和建筑。地图大致上北下南，街道名称包括“East 57th Street”“East 58th Street”“Madison Avenue”和“Park Avenue”。建筑用浅灰色矩形表示。“我”的位置周围有一个虚线圆，半径约为一个街区。圆内稍偏离中心的位置，有一个咖啡杯图标和两个刀叉图标，表示附近的咖啡馆和餐厅。地图底部注明：“地图瓦片由 Stamen Design 提供，采用 CC BY 3.0 许可。数据 © OpenStreetMap 贡献者。”整体直观展示了一个人的位置，以及步行距离内的附近兴趣点。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fproximity-service%2Ffigure-3-two-dimensional-search-UCK2KWKU.png&w=3840&q=75)

图 3 二维搜索

这一过程可以表示为以下伪 SQL 查询：

```
SELECT business_id, latitude, longitude,
FROM business
WHERE (latitude BETWEEN {:my_lat} - radius AND {:my_lat} + radius) AND
      (longitude BETWEEN {:my_long} - radius AND {:my_long} + radius)
```

这一查询效率不高，因为需要扫描整张表。如果在 longitude 和 latitude 列上建立索引，能提高效率吗？提升不会很大。问题在于，我们处理的是二维数据，每个维度返回的数据集仍可能非常庞大。例如，图 4 中，借助经度和纬度列上的索引，可以快速获取数据集 1 和数据集 2。但要找到指定半径内的商家，还需要对两个数据集求交集。由于每个数据集都包含大量数据，这样做效率不高。

![图中展示了一张世界地图，两个数据集叠加在地理底图上。底图显示北美洲、南美洲、欧洲、亚洲、非洲、澳大利亚和南极洲，并用不同地形颜色表示海拔。一条浅蓝色水平带横跨地图，位置约为北纬 30 至 40 度，右侧标为“数据集 1”，表示该数据集覆盖的地理范围。一条海绿色竖直带从北极延伸到南极，大致以西经 90 度为中心，顶部标为“数据集 2”，表示其经度覆盖范围。地图使用经纬网，纵轴纬度标注为 -90 至 90 度，横轴经度标注为 -180 至 180 度。左下角注明：“地图瓦片由 Stamen Design 提供，采用 CC BY 3.0 许可。数据 © OpenStreetMap 贡献者。”](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fproximity-service%2Ffigure-4-intersect-two-datasets-XGXW3FF3.png&w=3840&q=75)

图 4 两个数据集求交集

上一方案的问题在于，数据库索引只能提高一个维度上的搜索速度。因此，自然会进一步想到：能否把二维数据映射到一维？答案是可以。

在探讨具体方案之前，先看看不同的索引方法。从广义上说，地理空间索引可以分为两类，如图 5 所示。高亮显示的算法在业界应用广泛，我们将详细讨论。

-   哈希：均匀网格、地理哈希、笛卡尔分层（cartesian tiers）\[12\] 等。
    
-   树：四叉树、Google S2、R 树 \[13\] 等。
    

![图中以层次树结构展示了空间数据的不同索引技术。根节点为“索引”，向下分为“哈希”和“树”两大类。“哈希”节点进一步分为三个叶子节点，表示具体的哈希索引方法：“均匀网格”“地理哈希”和“笛卡尔分层”，均采用浅粉色矩形框。“树”节点同样分为三个叶子节点，表示基于树的索引方法：“四叉树”“Google S2”（同样为浅粉色框）和“R 树”（白色矩形框）。节点间的连线表示层次关系，说明各索引方法所属的类别。图中没有明确的数据流，仅展示各种空间索引技术的组织结构。](https://bytebytego.com/images/courses/system-design-interview/proximity-service/figure-5-different-types-of-geospatial-indexes-FFADHPSV.svg)

图 5 不同类型的地理空间索引

这些方案的底层实现虽然不同，但总体思路相同，即**将地图划分成更小的区域，并建立索引以加快搜索**。其中，地理哈希、四叉树和 Google S2 在实际应用中最为常见。下面逐一介绍。

| 提示                                                     |
| ------------------------------------------------------------ |
| 实际面试中，通常不必解释各类索引方案的实现细节，但需要基本理解为什么要使用地理空间索引、它的大致工作原理及其局限。 |

#### 方案 2：均匀划分的网格

一种简单的方法是将整个世界均匀划分为小网格（图 6）。这样，一个网格内可以有多家商家，而地图上的每家商家都属于某个网格。

![图中展示了一张采用基于 WGS84 基准的地理坐标系绘制的全球地图。地图呈矩形网格，纵轴标注纬度，范围为 -75 至 75 度；横轴标注经度，范围为 -150 至 150 度。陆地为浅灰色，海洋留白。虚线网格覆盖地图，为经纬度坐标提供视觉参考。标题明确写明“全球地图——地理坐标系——WGS84 基准”，并注明单位为“度——纬度/经度”。图中没有 URL 或参数。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fproximity-service%2Ffigure-6-global-map-NWAMEPOX.png&w=3840&q=75)

图 6 全球地图（来源：\[14\]）

这种方案在一定程度上可行，但有一个主要问题：商家分布并不均匀。纽约市中心可能有大量商家，而沙漠或海洋中的网格内可能一家商家都没有。将世界划分为均匀网格，会产生很不均匀的数据分布。理想情况下，密集地区应使用更细的网格，稀疏地区应使用较大的网格。另一个潜在难题，是如何找到给定网格的相邻网格。

#### 方案 3：地理哈希

地理哈希优于均匀网格方案。它将二维经纬度数据转换为一维的字母数字字符串。地理哈希算法每增加一位，就递归地将世界划分为更小的网格。下面从总体上看看其工作方式。

首先，沿本初子午线和赤道，将地球划分为四个象限。

![图中展示了一张世界地图，两条在中心垂直相交的直线将其分为四个象限。每个象限都有两位数字标记：左上为 01，右上为 11，左下为 00，右下为 10。左上象限 01 展示欧洲、亚洲和非洲；右上象限 11 展示北美洲及南美洲的一部分；左下象限 00 展示南极洲和南印度洋的一部分；右下象限 10 展示南美洲其余部分，以及包括南极洲在内的大部分南大洋。陆地采用写实地形风格，以深浅不同的绿色和棕色表示海拔，海洋为浅蓝色。图中没有 URL 或参数。数字似乎是象限位置的二进制表示，可能用于索引或组织数据。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fproximity-service%2Ffigure-7-geohash-XY4L6XHV.png&w=3840&q=75)

图 7 地理哈希

-   纬度范围 \[-90, 0\] 用 0 表示
    
-   纬度范围 \[0, 90\] 用 1 表示
    
-   经度范围 \[-180, 0\] 用 0 表示
    
-   经度范围 \[0, 180\] 用 1 表示
    

其次，将每个网格划分为四个更小的网格。每个网格可以通过交替排列经度位和纬度位来表示。

![图中展示了一种类似四叉树的层次结构，将世界地图递归分区。顶层由 2x2 个世界地图区域组成，分别标有两位二进制码 01、11、00、10。这些编码似乎代表地理分区：01 可能表示欧亚大陆，11 表示北美洲，00 表示南极洲和非洲南部，10 表示南美洲。一条斜线分隔左上和右下象限。下半部分将每个顶层象限进一步展开成 2x2 网格，共得到 16 个较小的地图区域。每个小区域也都有四位二进制码，例如 0101、0111 等，表示进一步细化的地理划分。部分地图区域能看到欧洲、非洲、亚洲、澳大利亚、北美洲、南美洲和南极洲，为这些二进制码提供地理参照。整体结构体现了一种基于递归空间分区、使用二进制编码对地理位置进行索引或寻址的方法。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fproximity-service%2Ffigure-8-divide-grid-Z5AJ3TFS.png&w=3840&q=75)

图 8 划分网格

重复这一细分过程，直到网格大小满足所需精度。地理哈希通常采用 base32 表示 \[15\]。下面看两个例子。

-   Google 总部的地理哈希（长度 = 6）：
    
    ```
    1001 10110 01001 10000 11011 11010 (base32 in binary) → 9q9hvu (base32)
    ```
    
-   Facebook 总部的地理哈希（长度 = 6）：
    
    ```
    1001 10110 01001 10001 10000 10111 (base32 in binary) → 9q9jhr (base32)
    ```
    

如表 4 所示，地理哈希有 12 种精度，也称层级。精度决定网格大小。我们只关注长度在 4 到 6 之间的地理哈希：长度大于 6 时，网格太小；长度小于 4 时，网格又太大（见表 4）。

| **地理哈希长度** | **网格宽度 x 高度**                        |
| ------------------ | ---------------------------------------------- |
| 1                  | 5,009.4km x 4,992.6km（地球的大小） |
| 2                  | 1,252.3km x 624.1km                            |
| 3                  | 156.5km x 156km                                |
| **4**              | **39.1km x 19.5km**                            |
| **5**              | **4.9km x 4.9km**                              |
| **6**              | **1.2km x 609.4m**                             |
| 7                  | 152.9m x 152.4m                                |
| 8                  | 38.2m x 19m                                    |
| 9                  | 4.8m x 4.8m                                    |
| 10                 | 1.2m x 59.5cm                                  |
| 11                 | 14.9cm x 14.9cm                                |
| 12                 | 3.7cm x 1.9cm                                  |

表 4 地理哈希长度与网格大小的对应关系（来源：\[16\]）

如何选择合适的精度？我们希望找到能够覆盖用户指定半径所画整个圆的最小地理哈希长度。半径与地理哈希长度的对应关系如下表所示。

| 半径（千米） | 地理哈希长度 |
| -------------------- | -------------- |
| 0.5 km（0.31 英里）   | 6              |
| 1 km（0.62 英里）     | 5              |
| 2 km（1.24 英里）     | 5              |
| 5 km（3.1 英里）      | 4              |
| 20 km（12.42 英里）   | 4              |

表 5 半径与地理哈希的对应关系

这种方式在大多数情况下效果很好，但地理哈希的边界处理存在一些特殊情况，需要与面试官讨论。

**边界问题**

地理哈希保证，两个编码的公共前缀越长，对应位置就越近。如图 9 所示，所有网格都有公共前缀 9q8zn。

![图中展示了一块覆盖网格的地理区域，可能是城市的一部分。底图包含 34th、35th、32nd、33rd Avenue，以及 Judah、Kirkham、Lawton、Moraga Street 等道路名称，表明这是一个真实地点。深蓝色网格线将底图划分为九个大小相同的矩形区域。每个区域都填充浅绿色，并标有唯一的字母数字标识符，以“9q8zn”开头，后接数字 2、3、6、8、9 或字母 b、c、d、f。这些标识符以深蓝色文字显示在对应网格内。图中没有明确的信息流，只展示这些带标签的网格区域在街道地图上的空间布局。底部注明：“地图瓦片由 Stamen Design 提供，采用 CC BY 3.0 许可。数据 © OpenStreetMap 贡献者。”](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fproximity-service%2Ffigure-9-shared-prefix-K37LAGTS.png&w=3840&q=75)

图 9 公共前缀

**边界问题 1**

但反过来并不成立：两个位置可能很近，却完全没有公共前缀。原因是，赤道或本初子午线两侧的两个相近位置，属于世界的不同“一半”。例如，法国的 La Roche-Chalais（地理哈希：u000）距离 Pomerol（地理哈希：ezzz）仅 30km，但两者的地理哈希完全没有公共前缀 \[17\]。

![图中是一张地理地图，展示了法国的 La Roche-Chalais（标为 A）和 Pomerol（标为 B）两个地点。La Roche-Chalais 旁注明地理哈希“u000”及位置详情“he-Chalais, Aquitaine, France”；Pomerol 旁注明地理哈希“ezzz”和位置“Pomerol, Nouvelle-A”。一条亮黄色箭头连接两地，黄色方框注明距离约为 30km。一条浅蓝色线沿河流蜿蜒延伸，可能表示两点之间的路线。尽管距离很近，黄色方框特别强调两地没有公共地理哈希前缀，用来说明地理空间索引和数据结构中的相关概念。地图背景提供了周围地形的直观参照。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fproximity-service%2Ffigure-10-no-shared-prefix-MFLTKKDZ.png&w=3840&q=75)

图 10 没有公共前缀

由于这一边界问题，下面这种简单的 SQL 前缀查询无法获取所有附近商家。

```
SELECT * FROM geohash_index WHERE geohash LIKE `9q8zn%`
```

**边界问题 2**

另一个边界问题是，两个位置可能拥有很长的公共前缀，但属于不同的地理哈希网格，如图 11 所示。

![图中展示了一块覆盖网格的地图，突出显示特定区域。底图包含城市街区布局，道路标注有“Judah Street”“Kirkham Street”“Lawton Street”“34th Avenue”“29th Avenue”“27th”“26th”“25th”“24”“33rd”和“35th”。浅绿色阴影覆盖了包含多个街区的大矩形区域，再由深蓝色线条细分为 3x3 网格。九个子区域各自标有唯一的字母数字标识符，以“9q8zn”开头，后接数字或字母，例如“9q8zn6”“9q8zn2”“9q8znc”。网格上叠加了两个彩色圆：深棕色圆位于中央 3x3 网格的右上部分，绿色圆稍偏其左下方。底部注明：“地图瓦片由 Stamen Design 提供，采用 CC BY 3.0 许可。数据 © OpenStreetMap 贡献者。”](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fproximity-service%2Ffigure-11-boundary-issue-XJWRE2EX.png&w=3840&q=75)

图 11 边界问题

常见的解决方法是，不仅获取当前网格内的所有商家，还获取相邻网格中的商家。相邻网格的地理哈希可以在常数时间内算出，更多细节见 \[17\]。

**商家数量不足**

现在来处理加分问题：如果当前网格和所有相邻网格中的商家加起来仍然不够，应该怎么办？

方案 1：只返回半径范围内的商家。这种方式容易实现，但缺点也很明显：返回的结果不足以满足用户需求。

方案 2：扩大搜索半径。可以删除地理哈希的最后一位，使用新的地理哈希获取附近商家。如果数量仍然不足，就再删除一位，继续扩大范围。这样，网格会逐步扩大，直到结果数量超过期望值。图 12 展示了扩大搜索范围过程的概念图。

![图中按顺序展示了扩大搜索区域的过程，由三个大小相同的网格组成，每个网格都包含按行列排列、大小相等的许多小方格。第一个网格中央附近有一个绿色小方格。一条标为“扩大搜索区域”的黑色箭头从第一个网格指向第二个网格。第二个网格中的绿色方格扩大了，占据更多区域，但仍位于中心附近。另一条相同的黑色箭头同样标为“扩大搜索区域”，从第二个网格指向第三个网格。第三个网格中的绿色方格进一步扩大，占据明显更大的范围。整体说明了搜索范围如何从局部小区域开始，逐步向外扩大。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fproximity-service%2Ffigure-12-expand-the-search-process-ZN5TJYNX.png&w=3840&q=75)

图 12 扩大搜索范围的过程

#### 方案 4：四叉树

另一种常用方案是四叉树。四叉树 \[18\] 是一种常见的数据结构，通过递归地将二维空间细分为四个象限（网格）来进行分区，直到网格内容满足指定条件。例如，可以不断细分，直到网格中的商家数量不超过 100。这个数值只是示例，实际取值可以根据业务需求确定。采用四叉树时，我们在内存中构建树结构来响应查询。请注意，**四叉树是内存数据结构，并不是数据库方案**。它运行在每台 LBS 服务器上，服务器启动时构建该数据结构。

下图直观展示了将整个世界细分为四叉树的概念过程。假设全球共有 2 亿家商家（200m，m 表示百万）。

![图中展示了数据经过不同阶段的系统，三个矩形框水平排列。最左侧是一个标为“200m”的大正方形，表示数据源或大型数据集。一条双向箭头将其与中间方框连接。中间方框分为四个小正方形，分别标有方位 NW、NE、SW、SE 及以米为单位的数值 40m、30m、70m、60m，可能表示各区域的大小或容量。另一条双向箭头将中间方框与最右侧方框连接。最右侧方框进一步分为四个小矩形，上方两个标为“10m”“11m”，下方两个标为“4m”“5m”，可能代表具有不同容量的后续处理或筛选阶段。箭头表示数据从源头 200m 经 NW、NE、SW、SE 处理阶段，最后到达 10m、11m、4m、5m 输出或最终阶段的流动。整体结构暗示一条包含多个处理步骤的数据流水线，初始数据规模较大，随后被拆分或处理为更小的单元。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fproximity-service%2Ffigure-13-quadtree-MWKA5XCK.png&w=3840&q=75)

图 13 四叉树

图 14 更详细地说明了四叉树的构建过程。根节点代表整个世界地图。不断递归地将节点分为 4 个象限，直到没有任何节点包含超过 100 家商家。

![图中展示了一种树状结构，可能是四叉树，说明层次化的数据分区方式。根节点标为“200m”，向下分出四个子节点 40m、30m、70m、60m，分别带有 nw、ne、sw、se 方位标记，表示西北、东北、西南和东南。这样的方位标记和四路分支模式在树中继续向下延伸。圆形代表内部节点，正方形代表叶子节点，虚线表示为简洁起见省略的多层结构。节点旁的数值可能表示相应区域或分区中的数据。叶子节点内的数值为 50、90、60、140、60、80，可能代表层次结构最低层的最终数据点或聚合值。图例解释了圆形与正方形分别表示内部节点和叶子节点，虚线表示图中省略了多层。](https://bytebytego.com/images/courses/system-design-interview/proximity-service/figure-14-build-quadtree-ZV3JS35E.svg)

图 14 构建四叉树

构建四叉树的伪代码如下：

```
public void buildQuadtree(TreeNode node) {
    if (countNumberOfBusinessesInCurrentGrid(node) > 100) {
        node.subdivide();
        for (TreeNode child : node.getChildren()) {
            buildQuadtree(child);
        }
    }
}
```

要回答这个问题，需要先知道存储哪些数据。

**叶子节点中的数据**

| **名称**                                                     | **大小**                                                     |
| ------------------------------------------------------------ | ------------------------------------------------------------ |
| 用于标识网格的左上角坐标和右下角坐标 | 32 字节（8 字节 \* 4）                                      |
| 网格中的商家 ID 列表                             | 每个 ID 8 字节 \* 100（单个网格允许的最大商家数） |
| 总计                                                        | 832 字节                                                    |

表 6 叶子节点

**内部节点中的数据**

| **名称**                                                     | **大小**                |
| ------------------------------------------------------------ | ----------------------- |
| 用于标识网格的左上角坐标和右下角坐标 | 32 字节（8 字节 \* 4） |
| 指向 4 个子节点的指针                                       | 32 字节（8 字节 \* 4） |
| 总计                                                        | 64 字节                |

表 7 内部节点

虽然构建树的过程取决于网格内的商家数量，但无须在四叉树节点中保存这一数量，因为可以从数据库记录中推导出来。

现在已经知道每个节点的数据结构，接下来看看内存用量。

-   每个网格最多可以存储 100 家商家
    
-   叶子节点数量 = ~200 million / 100 = ~2 million
    
-   内部节点数量 = 2 million \* 1/3 = ~0.67 million。如果不清楚为什么内部节点数量是叶子节点的三分之一，请参阅参考资料 \[19\]。
    
-   内存总需求 = 2 million \* 832 bytes + 0.67 million \* 64 bytes = ~1.71 GB。即使再加上构建树的一些额外开销，内存需求仍然很小。
    

实际面试中，不需要进行如此详细的计算。这里的关键在于，四叉树索引占用内存不多，可以轻松容纳在一台服务器中。这是否意味着，只需一台服务器存储四叉树索引？答案是否定的。根据读取量，单台四叉树服务器的 CPU 或网络带宽可能不足以处理全部读请求。此时，需要将读取负载分散到多台四叉树服务器上。

**构建整棵四叉树需要多长时间？**

每个叶子节点包含约 100 个商家 ID。构建树的时间复杂度为 _(N/100) lg(N/100)_，其中 _N_ 为商家总数。包含 2 亿家商家的整棵四叉树，可能需要几分钟才能构建完成。

**如何使用四叉树获取附近商家？**

1.  在内存中构建四叉树。
    
2.  四叉树构建完成后，从根节点开始搜索并遍历，直到找到搜索起点所在的叶子节点。如果该叶子节点有 100 家商家，就返回该节点。否则，从相邻网格中补充商家，直到返回足够数量的商家。
    

**四叉树的运维考虑**

如前所述，服务器启动时，构建包含 2 亿家商家的四叉树可能需要几分钟。如此长的启动时间会带来运维影响，需要认真考虑。四叉树构建期间，服务器无法处理流量。因此，应逐步发布服务器新版本，每次只更新一小部分服务器，避免让集群中大量服务器同时下线，导致服务性能下降。也可以采用蓝绿部署 \[20\]，但如果全新的服务器集群同时从数据库服务获取 2 亿家商家的数据，就会给系统带来很大压力。这样做并非不可行，但可能增加设计复杂度，面试中应当提及。

另一个运维问题是，商家不断增加和删除时，如何更新四叉树。最简单的方式是分批重建，每次重建一小部分服务器上的四叉树，逐步覆盖整个集群。但这意味着，短时间内部分服务器会返回过期数据。根据需求，这通常是可接受的折中。还可以事先约定，新增或更新的商家信息第二天才生效，进一步减轻影响。这样，就可以通过夜间任务更新缓存。这种方式的一个潜在问题是，大量键会同时失效，给缓存服务器带来很高负载。

也可以在商家增加和删除时即时更新四叉树。这会增加设计复杂度，尤其是在多个线程都能访问四叉树数据结构时，需要某种加锁机制，使实现大为复杂。

**实际应用中的四叉树示例**

Yext 提供了一张图片（图 15），展示丹佛附近构建完成的四叉树。密集区域需要更小、更精细的网格，稀疏区域则需要更大的网格。

![图中展示了一张可能位于美国科罗拉多州丹佛地区的地理地图，上方覆盖着由亮绿色大矩形单元组成的网格。网格对地图进行粗粒度分区，覆盖 Boulder、Arvada、Denver、Aurora、Centennial、Parker、Watkins、Strasburg、Byers 等城市所在的广阔区域。网格内可见标有 70、25、270 等编号的主要公路和较小道路，以及浅蓝色水域。城市名称提供了地理参照。这一网格似乎用于空间分区，在系统设计中可能用于数据分片、负载均衡或地理数据组织。单元大小和排列说明，这可能是经过规划的层次化区域划分；Denver 和 Aurora 一带的网格明显更细，可能表示该地区的数据密度或处理需求更高。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fproximity-service%2Ffigure-15-real-world-example-of-a-quadtree-WENVCDJ2.png&w=3840&q=75)

图 15 实际应用中的四叉树示例

#### 方案 5：Google S2

Google S2 几何库 \[22\] 也是这一领域的重要方案。与四叉树类似，它是一种内存方案，基于希尔伯特曲线（一种空间填充曲线）\[23\]，将球面映射到一维索引。希尔伯特曲线具有一个重要性质：曲线上相互接近的两个点，在一维空间中也相互接近（图 16）。一维空间中的搜索比二维空间高效得多。感兴趣的读者可以使用在线工具 \[24\] 探索希尔伯特曲线。

![图中可能展示了一种受限环境中的路径查找或网络路由概念。背景是覆盖网格的重复 L 形路径，形成类似迷宫的结构。图中有两组不同的节点及连线。左侧三个红粉色节点通过直线连接到左下角一个较大的节点，位置约为坐标（0.18，0），表示潜在路径。右侧四个蓝灰色节点同样连接到右下角一个较大的节点，坐标约为（0.875，0）。横轴似乎表示归一化距离或其他连续变量，范围为 0 到 1，并标有刻度间隔。纵轴没有明确标签，隐含表示网格内的垂直位置。整体布局可能是在比较从两个不同起点到同一目的地的不同路径或路线，用来说明迷宫式网格约束下的效率或最优性。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fproximity-service%2Ffigure-16-hilbert-curve-A42ZBOV2.png&w=3840&q=75)

图 16 希尔伯特曲线（来源：\[24\]）

S2 是一个复杂的库，面试中并不要求解释其内部实现。但由于 Google、Tinder 等公司广泛使用它，这里简要介绍其优势。

-   S2 很适合地理围栏，因为它可以使用不同层级覆盖任意形状的区域（图 17）。根据维基百科的定义，“地理围栏是现实地理区域的虚拟边界。它可以动态生成，例如以某个位置为中心、指定半径的区域；也可以是一组预先定义的边界，例如学区或社区边界”\[25\]。
    
    ![图中展示了一张叠加地理围栏的街道地图。底图为城市的一部分，包含标为“Judah Street”“Kirkham Street”“Lawton Street”的道路，以及部分可见的“Juda”和“Law”街道名称。浅灰色矩形建筑按街区排列。地图上方覆盖一个浅青色不规则多边形，标为“地理围栏”，包含多个街区。围栏边界并不严格沿街道延伸，而是横穿街区，表明这是自定义区域。底部注明：“地图瓦片由 Stamen Design 提供，采用 CC BY 3.0 许可。数据 © OpenStreetMap 贡献者。”围栏底部附近还能看到部分数字“29”，可能是区域内的街道编号或其他位置标识符。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fproximity-service%2Ffigure-17-geofence-PIWNFXYE.png&w=3840&q=75)
    
    图 17 地理围栏
    
    地理围栏允许我们为关注的区域定义边界，并向离开这些区域的用户发送通知。与仅返回附近商家相比，它可以提供更丰富的功能。
    
-   S2 的另一个优点是区域覆盖（Region Cover）算法 \[26\]。地理哈希采用固定层级（精度），而 S2 可以指定最小层级、最大层级和最大单元数。由于单元大小灵活，S2 返回的结果更加精细。更多内容可以参考 S2 工具 \[26\]。
    

#### 建议

为高效查找附近商家，我们讨论了地理哈希、四叉树和 S2 等方案。如表 8 所示，不同公司或技术采用不同方案。

| **地理空间索引**             | **公司**                                              |
| ------------------------- | ---------------------------------------------------------- |
| 地理哈希                   | Bing 地图 \[27\]、Redis \[10\]、MongoDB \[28\]、Lyft \[29\] |
| 四叉树                  | Yext \[21\]                                                |
| 地理哈希和四叉树 | Elasticsearch \[30\]                                       |
| S2                        | Google Maps, Tinder \[31\]                                 |

表 8 不同类型的地理空间索引

面试时，建议选择**地理哈希或四叉树**，因为 S2 更复杂，难以在面试中解释清楚。

#### 地理哈希与四叉树对比

结束本节之前，先简单比较地理哈希与四叉树。

**地理哈希**

-   易于使用和实现，无须构建树。
    
-   支持返回指定半径内的商家。
    
-   地理哈希精度（层级）固定时，网格大小也固定，不能根据人口密度动态调整。要支持这一点，需要更复杂的逻辑。
    
-   更新索引很简单。例如，要从索引中移除某家商家，只需删除具有相同 _geohash_ 和 _business\_id_ 的对应行。图 18 展示了一个具体例子。
    
    ![图中是一张包含“geohash”和“business_id”两列的简单表格。geohash 列中的三条记录均为“9q8zn”，business_id 列中的对应数值为 3、8、4。一条红色横线突出显示第二行，强调地理哈希“9q8zn”及其关联的 business_id 8。该表可能是一种简化数据结构，将表示地理位置的地理哈希关联到商家标识符。高亮行表示特别关注位于“9q8zn”区域内、ID 为 8 的商家。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fproximity-service%2Ffigure-18-remove-a-business-ELAJOMXR.png&w=3840&q=75)
    
    图 18 删除商家
    

**四叉树**

-   实现略难，因为需要构建树。
    
-   支持获取最近的 k 家商家。有时只想返回最近的 k 家商家，并不关心它们是否位于指定半径内。例如，旅行途中汽车快没油了，只想找到最近的 k 个加油站。这些加油站可能离你并不近，但应用仍需返回最近的 k 个结果。四叉树很适合这类查询，因为其细分过程基于数量 k，可以自动调整查询范围，直到返回 k 个结果。
    
-   可以根据人口密度动态调整网格大小，参见图 15 中的丹佛示例。
    
-   更新索引比地理哈希复杂。四叉树是一种树结构，删除商家时，需要从根节点遍历到叶子节点才能完成。例如，要删除 ID = 2 的商家，必须从根节点一路找到叶子节点，如图 19 所示。更新索引的时间复杂度为 _O(logn)_，但多线程程序访问该数据结构时需要加锁，实现会更复杂。此外，树的再平衡也可能比较复杂。例如，叶子节点无法容纳新条目时，就需要再平衡。一种可能的解决方法是为范围预留更多容量。
    

![图中展示了一种树状结构，可能是四叉树或类似的空间分区数据结构。根节点标为“200m”，以浅粉色圆形表示内部节点。它分出四个子节点 40m、30m、70m、60m，同样都是内部节点。连线分别标为“nw”“ne”“sw”“se”，表示方位。这一模式递归重复，直到四层深度。内部节点用浅粉色圆形表示，叶子节点用正方形表示，虚线表示为简洁起见省略的多层结构。叶子节点包含 50、90、60 等数值。最后一个叶子节点为浅粉色，标有“80”和额外文字“business_ids: [2, 6, 7]”，表示它保存着关联数据。图例说明圆形和正方形分别代表内部节点与叶子节点，虚线代表省略多层。节点上的数字可能表示大小或数据量等数量指标。整体结构体现了用于空间索引或类似应用的层次化数据组织。](https://bytebytego.com/images/courses/system-design-interview/proximity-service/figure-19-update-quadtree-OCBKLCXE.svg)

图 19 更新四叉树

## 第 3 步：深入设计

现在应该已经对系统的整体面貌有了清晰认识。接下来深入讨论几个方面。

-   扩展数据库
    
-   缓存
    
-   区域和可用区
    
-   按时间或商家类型筛选结果
    
-   最终架构图
    

### 扩展数据库

下面讨论如何扩展两张最重要的表：business 表和地理空间索引表。

#### business 表

business 表的数据可能无法全部容纳在一台服务器中，因此适合进行分片。最简单的方法是按商家 ID 对全部数据分片。这种方案能将负载均匀分布到各分片，运维也比较容易。

#### 地理空间索引表

地理哈希和四叉树都应用广泛。由于地理哈希比较简单，这里以它为例。表结构可以采用两种方式。

方案 1：每个 geohash 键对应一行，行内用一个 JSON 数组保存商家 ID。这意味着，同一地理哈希范围内的所有商家 ID 都保存在同一行中。

![图中展示了一张地理空间索引表，边框带灰色阴影。顶部浅灰色表头以黑色粗体标为“geospatial_index”。表头下方有两行。第一行用黑色普通字体标出“geohash”，表示一种将地理坐标编码为短字符串的空间索引值。第二行同样以黑色普通字体标为“list_of_business_ids”，表示上一行地理哈希所对应区域内的商家标识符列表。该结构体现了一对多关系：一个地理哈希可以对应多个商家 ID。各组成部分间没有直观的信息流，表格仅用于存储数据。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fproximity-service%2Ftable-9-list_of_business_ids-is-a-json-array-HUAEOYZJ.png&w=3840&q=75)

表 9 list\_of\_business\_ids 是一个 JSON 数组

方案 2：同一个地理哈希范围内有多家商家时，使用多行，每家商家各占一行。这意味着，同一地理哈希内的不同商家 ID 分别保存在不同行中。

![图中展示了一张标为“geospatial_index”的表结构，包含两列。最上方一行是表头，标明表名。表头下方第一列标为“geohash”，可能存储表示地理位置的地理哈希值；第二列标为“business_id”，可能存储商家的唯一标识符。该表用于创建地理空间索引，以地理哈希为空间键，高效获取相应地理位置上的商家。图中没有明确的信息流，仅展示表结构。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fproximity-service%2Ftable-10-business_id-is-a-single-id-3ZNAELVH.png&w=3840&q=75)

表 10 business\_id 是一个 ID

下面是方案 2 的一些示例记录。

| **geohash** | **business\_id** |
| ----------- | ---------------- |
| 32feac      | 343              |
| 32feac      | 347              |
| f3lcad      | 112              |
| f3lcad      | 113              |

表 11 地理空间索引表的示例记录

**建议**：我们推荐方案 2，理由如下：

采用方案 1 时，要更新商家信息，需要先获取 _business\_ids_ 数组，再扫描整个数组，找到需要更新的商家。插入新商家时，也必须扫描整个数组，确保没有重复。此外，还需要锁定该行以防并发更新，要处理的特殊情况很多。

采用方案 2 时，如果两列构成复合键（_geohash, business\_id_），添加和删除商家就非常简单，无须锁定任何内容。

**扩展地理空间索引**

扩展地理空间索引时，一个常见错误是不考虑表的实际大小，就急于采用分片方案。在本例中，地理空间索引表的完整数据集并不大：四叉树索引仅占用 1.71G 内存，地理哈希索引的存储需求也相近。整个地理空间索引可以轻松容纳在现代数据库服务器的工作集中。不过，根据读取量，单台数据库服务器的 CPU 或网络带宽可能不足以处理全部读请求。此时，需要将读取负载分散到多台数据库服务器上。

分散关系数据库服务器负载，通常有两种方法：增加只读副本，或对数据库分片。

许多工程师喜欢在面试中讨论分片。但分片比较复杂，未必适合 geohash 表。例如，应用层需要加入分片逻辑。有时，分片确实是唯一选择；但在本场景中，所有数据都能放入单台数据库服务器的工作集，因此没有充分的技术理由将数据分片到多台服务器。

在本场景下，更好的方法是部署多个只读副本，分担读取负载。这种方式的开发和维护简单得多。因此，建议通过副本扩展地理空间索引表。

### 缓存

在引入缓存层之前，首先要问：真的需要缓存层吗？

缓存是否一定带来明显收益，并不显而易见：

-   工作负载以读取为主，数据集又相对较小，可以容纳在现代数据库服务器的工作集中。因此，查询不受 I/O 限制，速度应当几乎与内存缓存一样快。
    
-   如果读取性能成为瓶颈，可以增加数据库只读副本，提高读取吞吐量。
    

与面试官讨论缓存时需要谨慎，因为这需要仔细进行基准测试和成本分析。如果确认缓存符合业务需求，再继续讨论缓存策略。

#### 缓存键

最直接的缓存键选择是用户的位置坐标，也就是纬度和经度。不过，这种选择存在几个问题：

-   手机返回的位置坐标并不精确，只是尽可能准确的估计 \[32\]。即使没有移动，每次在手机上获取的坐标也可能略有不同。
    
-   用户从一个位置移动到另一个位置时，坐标会稍有变化。对大多数应用而言，这种变化没有实际意义。
    

因此，位置坐标不适合作为缓存键。理想情况下，位置的小幅变化仍应映射到同一个缓存键。前面介绍的地理哈希和四叉树方案可以很好地处理这个问题，因为同一网格内的所有商家都映射到相同地理哈希。

#### 缓存的数据类型

如表 12 所示，可以缓存两类数据，提高系统的整体性能：

| **键**      | **值**                        |
| ------------ | -------------------------------- |
| geohash      | 网格内的商家 ID 列表 |
| business\_id | business 对象                  |

表 12 缓存中的键值对

**网格内的商家 ID 列表**

商家数据相对稳定，因此可以预先计算给定地理哈希内的商家 ID 列表，存入 Redis 这样的键值存储。下面看一个启用缓存后获取附近商家的具体例子。

1.  获取给定地理哈希内的商家 ID 列表。
    
    ```
    SELECT business_id FROM geohash_index WHERE geohash LIKE `{:geohash}%`
    ```
    
2.  如果缓存未命中，将结果保存到 Redis 缓存。
    
    ```
    public List<String> getNearbyBusinessIds(String geohash) {
        String cacheKey = hash(geohash);
        List<string> listOfBusinessIds = Redis.get(cacheKey);
        if (listOfBusinessIDs  == null) {
            listOfBusinessIds = Run the select SQL query above;
            Cache.set(cacheKey, listOfBusinessIds, "1d");
        }
        return listOfBusinessIds;
    }
    ```
    

新增、编辑或删除商家时，更新数据库并使缓存失效。这类操作数量相对较少，而且地理哈希方案无须加锁，因此更新操作比较容易处理。

根据需求，用户可以在客户端选择以下 4 个半径：500m、1km、2km 和 5km，分别对应地理哈希长度 4、5、5 和 6。为快速获取不同半径内的附近商家，我们在 Redis 中缓存这三种精度（geohash\_4、geohash\_5、geohash\_6）的数据。

如前所述，我们有 2 亿家商家，在给定精度下，每家商家属于 1 个网格。因此，内存总需求为：

-   Redis 值的存储空间：8 bytes \* 200 million \* 3 precisions = ~5 GB
    
-   Redis 键的存储空间：可以忽略不计
    
-   内存总需求：~5 GB
    

从内存用量看，一台现代 Redis 服务器就足够了。但为确保高可用性并减少跨洲延迟，我们会在全球部署 Redis 集群。根据估算的数据量，可以在全球部署相同的缓存数据副本。最终架构图（图 21）中，这套 Redis 缓存称为“Geohash”。

**客户端渲染页面所需的商家数据**

这类数据的缓存方式很直接：键为 business\_id，值为 business 对象，其中包含商家名称、地址、图片 URL 等。最终架构图（图 21）中，这套 Redis 缓存称为“Business info”。

### 区域和可用区

我们将基于位置的服务部署到多个区域和可用区，如图 21 所示。这有以下几个优点：

-   让用户在物理上更“接近”系统。美国西部用户连接该地区的数据中心，欧洲用户连接欧洲的数据中心。
    
-   可以灵活地根据人口分布均衡流量。日本、韩国等地区人口密度较高，可以考虑将它们放在独立区域，甚至将基于位置的服务部署到多个可用区，以分散负载。
    
-   隐私法律。某些国家可能要求用户数据在本地使用和存储。此时，可以在该国设立一个区域，并通过 DNS 路由，将来自该国的所有请求限制在该区域内。
    
    ![图中是一张世界地图，展示服务器区域与用户位置的地理分布，并说明网络延迟。多个标为“区域”的蓝色圆形分散在各大洲，代表数据中心位置。两个小人图标代表用户群，一组标为“美国西部用户”，另一组标为“欧洲用户”。虚线将美国西部用户连接到美国西部区域，标注的“10ms”表示延迟。另一条虚线从美国西部区域连接到欧洲区域，延迟为“140ms”。一条实线将欧洲用户连接到欧洲区域，延迟为“15ms”。地图直观展示了用户与服务器区域的距离如何影响网络响应时间，强调地理分布式服务器对降低延迟的重要性。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fproximity-service%2Ffigure-20-deploy-lbs-closer-to-the-user-OVZHVKZ6.png&w=3840&q=75)
    
    图 20 将 LBS 部署在更接近用户的位置
    

### 追问：按时间或商家类型筛选结果

面试官可能进一步追问：如何只返回当前营业的商家，或者只返回餐厅？

**候选人**：通过地理哈希或四叉树将世界划分为小网格后，搜索结果中的商家数量相对较少。因此，可以先返回商家 ID，再补全 business 对象，并根据营业时间或商家类型筛选。该方案假设营业时间和商家类型保存在 business 表中。

### 最终设计图

将各部分组合起来，得到以下设计图。

![图中展示了商家搜索服务的系统架构。顶部负载均衡器（1）分发传入请求。图中有两类请求：`/search/nearby`（2）路由到 LBS（负载均衡服务）（3、4），`/businesses/{id}` 路由到商家服务。LBS（3、4）读取 Redis 集群中的数据（5、6），集群包含“Business Info”和“Geohash”两个节点。商家服务从数据库集群读取数据，该集群是一个三节点副本集，包括一个粉色主数据库和两个白色副本数据库。主数据库接收商家服务的写操作，再将数据同步复制到两个副本，箭头上标有“复制”。“Sync”标记表示 Redis 集群与数据库之间的同步。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fproximity-service%2Ffigure-21-design-diagram-ORKTOGMW.png&w=3840&q=75)

图 21 设计图

#### 获取附近商家

1.  用户在 Yelp 上查找 500 米内的餐厅。客户端将用户位置（latitude = 37.776720，longitude = -122.416730）及半径 500m 发送给负载均衡器。
    
2.  负载均衡器将请求转发给 LBS。
    
3.  LBS 根据用户位置和半径，确定本次搜索对应的地理哈希长度。查阅表 5 可知，500m 对应的地理哈希长度为 6。
    
4.  LBS 计算相邻网格的地理哈希，并加入列表。结果类似于：list\_of\_geohashes = \[my\_geohash, neighbor1\_geohash, neighbor2\_geohash, …, neighbor8\_geohash\]。
    
5.  对 _list\_of\_geohashes_ 中的每个地理哈希，LBS 调用“Geohash”Redis 服务器，获取对应的商家 ID。各地理哈希的商家 ID 获取调用可以并行执行，以降低延迟。
    
6.  根据返回的商家 ID 列表，LBS 从“Business info”Redis 服务器获取完整商家信息，再计算用户与商家的距离、进行排序，并将结果返回客户端。
    

#### 查看、更新、添加或删除商家

所有商家相关 API 都与 LBS 分离。查看商家详情时，商家服务先检查“Business info”Redis 缓存中是否已有数据。如果有，就将缓存数据返回客户端；否则，从数据库集群获取数据，再存入 Redis 缓存，使后续请求能够直接从缓存获得结果。

由于事先已经约定，新增或更新的商家信息第二天生效，因此缓存中的商家数据通过夜间任务更新。

## 第 4 步：总结

本章介绍了邻近服务的设计。这是一个使用地理空间索引的典型 LBS 系统。我们讨论了几种索引方案：

-   二维搜索
    
-   均匀划分的网格
    
-   地理哈希
    
-   四叉树
    
-   Google S2
    

地理哈希、四叉树和 S2 被不同科技公司广泛采用。我们选择地理哈希为例，说明地理空间索引如何工作。

在深入设计中，我们讨论了缓存为何能降低延迟、应该缓存什么，以及如何借助缓存快速获取附近商家。还讨论了如何通过复制和分片扩展数据库。

随后，介绍了如何将 LBS 部署到不同区域和可用区，以提高可用性、让用户在物理上更靠近服务器，并更好地遵守当地隐私法律。

恭喜你读到这里！现在给自己一点鼓励吧，做得很好！

## 本章小结

![图中以思维导图概述了邻近服务的设计过程。中心节点为“邻近服务”，向外分出四个主要步骤。第 1 步详细列出需求，包括功能需求（返回附近商家、添加/删除/更新商家、查看商家）和非功能需求（低延迟、数据隐私、搜索 QPS 为 5,000）。第 2 步聚焦设计，包括 API 设计（搜索 API、商家 API、分页）、数据模型（读写比例、数据模式）和总体设计图。第 3 步讨论实现，包括算法（均匀网格、地理哈希、四叉树、Google S2、地理哈希与四叉树对比）、扩展数据库（business 表、地理空间索引表）以及缓存（缓存键、数据类型）。最后，第 4 步包含区域和可用区、筛选结果、最终设计图及总结。整幅图体现了设计过程从初始需求到最终实现和总结的层次化拆分。](https://bytebytego.com/_next/image?url=%2Fimages%2Fcourses%2Fsystem-design-interview%2Fproximity-service%2Fchapter-summary-CNKPFTKZ.png&w=3840&q=75)

## 参考资料

\[1\] Yelp: [<u>https://www.yelp.com/</u>](https://www.yelp.com/)

\[2\] Stamen Design 提供的地图瓦片：

[<u>http://maps.stamen.com/</u>](http://maps.stamen.com/)

\[3\] OpenStreetMap: [<u>https://www.openstreetmap.org</u>](https://www.openstreetmap.org/)

\[4\] 《通用数据保护条例》（GDPR）：[<u>https://en.wikipedia.org/wiki/General_Data_Protection_Regulation</u>](https://en.wikipedia.org/wiki/General_Data_Protection_Regulation)

\[5\] 《加利福尼亚州消费者隐私法案》（CCPA）：[<u>https://en.wikipedia.org/wiki/California_Consumer_Privacy_Act</u>](https://en.wikipedia.org/wiki/California_Consumer_Privacy_Act)

\[6\] REST API 中的分页：

[<u>https://developer.atlassian.com/server/confluence/pagination-in-the-rest-api/</u>](https://developer.atlassian.com/server/confluence/pagination-in-the-rest-api/)

\[7\] Google Places API：[<u>https://developers.google.com/maps/documentation/places/web-service/search</u>](https://developers.google.com/maps/documentation/places/web-service/search)

\[8\] Yelp 预订 API：

[<u>https://docs.developer.yelp.com/docs/reservation</u>](https://docs.developer.yelp.com/docs/reservation)

\[9\] 区域和可用区：

[<u>https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/using-regions-availability-zones.html</u>](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/using-regions-availability-zones.html)

\[10\] Redis GEOHASH: [<u>https://redis.io/commands/GEOHASH</u>](https://redis.io/commands/GEOHASH)

\[11\] POSTGIS: [<u>https://postgis.net/</u>](https://postgis.net/)

\[12\] 笛卡尔分层：[<u>http://www.nsshutdown.com/projects/lucene/whitepaper/locallucene_v2.html</u>](http://www.nsshutdown.com/projects/lucene/whitepaper/locallucene_v2.html)

\[13\] R 树：[<u>https://en.wikipedia.org/wiki/R-tree</u>](https://en.wikipedia.org/wiki/R-tree)

\[14\] 地理坐标参考系中的全球地图：

[<u>https://bit.ly/3DsjAwg</u>](https://bit.ly/3DsjAwg)

\[15\] Base32: [<u>https://en.wikipedia.org/wiki/Base32</u>](https://en.wikipedia.org/wiki/Base32)

\[16\] 地理哈希网格聚合：[<u>https://bit.ly/3kKl4e6</u>](https://bit.ly/3kKl4e6)

\[17\] 地理哈希：[<u>https://www.movable-type.co.uk/scripts/geohash.html</u>](https://www.movable-type.co.uk/scripts/geohash.html)

\[18\] 四叉树：[<u>https://en.wikipedia.org/wiki/Quadtree</u>](https://en.wikipedia.org/wiki/Quadtree)

\[19\] 四叉树有多少叶子节点：

[<u>https://stackoverflow.com/questions/35976444/how-many-leaves-has-a-quadtree</u>](https://stackoverflow.com/questions/35976444/how-many-leaves-has-a-quadtree)

\[20\] 蓝绿部署：[<u>https://martinfowler.com/bliki/BlueGreenDeployment.html</u>](https://martinfowler.com/bliki/BlueGreenDeployment.html)

\[21\] Yext：地图与位置支持：

[<u>https://www.yext.com/platform/features/maps-and-location-support</u>](https://www.yext.com/platform/features/maps-and-location-support)

\[22\] S2: [<u>http://s2geometry.io/</u>](http://s2geometry.io/)

\[23\] 希尔伯特曲线：[<u>https://en.wikipedia.org/wiki/Hilbert_curve</u>](https://en.wikipedia.org/wiki/Hilbert_curve)

\[24\] 希尔伯特映射：[<u>http://bit-player.org/extras/hilbert/hilbert-mapping.html</u>](http://bit-player.org/extras/hilbert/hilbert-mapping.html)

\[25\] 地理围栏：[<u>https://en.wikipedia.org/wiki/Geo-fence</u>](https://en.wikipedia.org/wiki/Geo-fence)

\[26\] 区域覆盖：[<u>http://s2geometry.io/devguide/s2cell_hierarchy</u>](http://s2geometry.io/devguide/s2cell_hierarchy)

\[27\] Bing 地图：[<u>https://bit.ly/30ytSfG</u>](https://bit.ly/30ytSfG)

\[28\] MongoDB: [<u>https://docs.mongodb.com/manual/tutorial/build-a-2d-index/</u>](https://docs.mongodb.com/manual/tutorial/build-a-2d-index/)

\[29\] 地理空间索引：支撑 Lyft 千万 QPS 的 Redis 架构：

[<u>https://www.youtube.com/watch?v=cSFWlF96Sds&amp;t=2155s</u>](https://www.youtube.com/watch?v=cSFWlF96Sds&t=2155s)

\[30\] 地理形状类型：

[<u>https://www.elastic.co/guide/en/elasticsearch/reference/1.6/mapping-geo-shape-type.html</u>](https://www.elastic.co/guide/en/elasticsearch/reference/1.6/mapping-geo-shape-type.html)

\[31\] 地理分片推荐，第 1 部分：分片方法：

[<u>https://medium.com/tinder-engineering/geosharded-recommendations-part-1-sharding-approach-d5d54e0ec77a</u>](https://medium.com/tinder-engineering/geosharded-recommendations-part-1-sharding-approach-d5d54e0ec77a)

\[32\] 获取最后一次已知位置：

[<u>https://developer.android.com/training/location/retrieve-current#Challenges</u>](https://developer.android.com/training/location/retrieve-current#Challenges)



