# 30 持续学习

设计优秀的系统，需要多年的知识积累。一条捷径是深入研究真实系统的架构。下面整理了一些有帮助的阅读资料。强烈建议同时关注这些系统共有的设计原则和底层技术。研究每项技术并理解它解决的问题，是夯实知识基础、完善设计过程的好方法。

## 真实系统

以下资料有助于理解不同公司真实系统架构背后的总体设计思路。

Facebook 时间线：反规范化的力量：[<u>https://goo.gl/FCNrbm</u>](https://goo.gl/FCNrbm)

Facebook 的规模化：[<u>https://goo.gl/NGTdCs</u>](https://goo.gl/NGTdCs)

构建时间线：扩展系统以承载你的人生故事：[<u>https://goo.gl/8p5wDV</u>](https://goo.gl/8p5wDV)

Facebook 中的 Erlang：Facebook 聊天系统：[<u>https://goo.gl/zSLHrj</u>](https://goo.gl/zSLHrj)

Facebook 聊天系统：[<u>https://goo.gl/qzSiWC</u>](https://goo.gl/qzSiWC)

在 Haystack 中大海捞针：Facebook 的照片存储：[<u>https://goo.gl/edj4FL</u>](https://goo.gl/edj4FL)

支撑 Facebook Multifeed：通过重新设计提高效率和性能：[<u>https://goo.gl/adFVMQ</u>](https://goo.gl/adFVMQ)

Facebook 如何扩展 Memcache：[<u>https://goo.gl/rZiAhX</u>](https://goo.gl/rZiAhX)

TAO：Facebook 面向社交图谱的分布式数据存储：[<u>https://goo.gl/Tk1DyH</u>](https://goo.gl/Tk1DyH)

Amazon 架构：[<u>https://goo.gl/k4feoW</u>](https://goo.gl/k4feoW)

Dynamo：Amazon 的高可用键值存储：[<u>https://goo.gl/C7zxDL</u>](https://goo.gl/C7zxDL)

全方位了解 Netflix 的完整技术栈：[<u>https://goo.gl/rYSDTz</u>](https://goo.gl/rYSDTz)

一切为了 A/B 测试：Netflix 实验平台：[<u>https://goo.gl/agbA4K</u>](https://goo.gl/agbA4K)

Netflix 推荐系统：超越五星评分，第1部分：[<u>https://goo.gl/A4FkYi</u>](https://goo.gl/A4FkYi)

Netflix 推荐系统：超越五星评分，第2部分：[<u>https://goo.gl/XNPMXm</u>](https://goo.gl/XNPMXm)

Google 架构：[<u>https://goo.gl/dvkDiY</u>](https://goo.gl/dvkDiY)

Google 文件系统：Google Docs：[<u>https://goo.gl/xj5n9R</u>](https://goo.gl/xj5n9R)

差分同步：Google Docs：[<u>https://goo.gl/9zqG7x</u>](https://goo.gl/9zqG7x)

YouTube 架构：[<u>https://goo.gl/mCPRUF</u>](https://goo.gl/mCPRUF)

西雅图可扩展性会议：YouTube 的可扩展性：[<u>https://goo.gl/dH3zYq</u>](https://goo.gl/dH3zYq)

Bigtable：面向结构化数据的分布式存储系统：[<u>https://goo.gl/6NaZca</u>](https://goo.gl/6NaZca)

Instagram 架构：1400万用户、TB级照片、数百个实例、数十种技术：[<u>https://goo.gl/s1VcW5</u>](https://goo.gl/s1VcW5)

Twitter 如何设计架构以支持1.5亿活跃用户：[<u>https://goo.gl/EwvfRd</u>](https://goo.gl/EwvfRd)

扩展 Twitter：让 Twitter 提速10000%：[<u>https://goo.gl/nYGC1k</u>](https://goo.gl/nYGC1k)

介绍 Snowflake：一种用于大规模生成唯一标识符、并提供若干简单保证的网络服务：[<u>https://goo.gl/GzVWYm</u>](https://goo.gl/GzVWYm)

大规模时间线系统：[<u>https://goo.gl/8KbqTy</u>](https://goo.gl/8KbqTy)

Uber 如何扩展实时市场平台：[<u>https://goo.gl/kGZuVy</u>](https://goo.gl/kGZuVy)

扩展 Pinterest：[<u>https://goo.gl/KtmjW3</u>](https://goo.gl/KtmjW3)

Pinterest 架构更新：[<u>https://goo.gl/w6rRsf</u>](https://goo.gl/w6rRsf)

LinkedIn 扩展简史：[<u>https://goo.gl/8A1Pi8</u>](https://goo.gl/8A1Pi8)

Flickr 架构：[<u>https://goo.gl/dWtgYa</u>](https://goo.gl/dWtgYa)

我们如何扩展 Dropbox：[<u>https://goo.gl/NjBDtC</u>](https://goo.gl/NjBDtC)

Facebook 以190亿美元收购的 WhatsApp 架构：[<u>https://bit.ly/2AHJnFn</u>](https://bit.ly/2AHJnFn)

## 公司工程博客

如果准备参加某家公司的面试，阅读该公司的工程博客，熟悉其采用的技术和实现的系统，是个好主意。此外，工程博客还会提供对特定领域极有价值的洞见。经常阅读，有助于我们成为更好的工程师。

下面列出一些知名大型公司和初创企业的工程博客。

Airbnb: [<u>https://medium.com/airbnb-engineering</u>](https://medium.com/airbnb-engineering)

Amazon: [<u>https://developer.amazon.com/blogs</u>](https://developer.amazon.com/blogs)

Asana: [<u>https://blog.asana.com/category/eng</u>](https://blog.asana.com/category/eng)

Atlassian: [<u>https://developer.atlassian.com/blog</u>](https://developer.atlassian.com/blog)

Bittorrent: [<u>http://engineering.bittorrent.com</u>](http://engineering.bittorrent.com/)

Cloudera: [<u>https://blog.cloudera.com</u>](https://blog.cloudera.com/)

Docker: [<u>https://blog.docker.com</u>](https://blog.docker.com/)

Dropbox: [<u>https://blogs.dropbox.com/tech</u>](https://blogs.dropbox.com/tech)

eBay: [<u>http://www.ebaytechblog.com</u>](http://www.ebaytechblog.com/)

Facebook: [<u>https://code.facebook.com/posts</u>](https://code.facebook.com/posts)

GitHub: [<u>https://githubengineering.com</u>](https://githubengineering.com/)

Google: [<u>https://developers.googleblog.com</u>](https://developers.googleblog.com/)

Groupon: [<u>https://engineering.groupon.com</u>](https://engineering.groupon.com/)

Highscalability: [<u>http://highscalability.com</u>](http://highscalability.com/)

Instacart: [<u>https://tech.instacart.com</u>](https://tech.instacart.com/)

Instagram: [<u>https://engineering.instagram.com</u>](https://engineering.instagram.com/)

Linkedin: [<u>https://engineering.linkedin.com/blog</u>](https://engineering.linkedin.com/blog)

Mixpanel: [<u>https://mixpanel.com/blog</u>](https://mixpanel.com/blog)

Netflix: [<u>https://medium.com/netflix-techblog</u>](https://medium.com/netflix-techblog)

Nextdoor: [<u>https://engblog.nextdoor.com</u>](https://engblog.nextdoor.com/)

PayPal: [<u>https://www.paypal-engineering.com</u>](https://www.paypal-engineering.com/)

Pinterest: [<u>https://engineering.pinterest.com</u>](https://engineering.pinterest.com/)

Quora: [<u>https://engineering.quora.com</u>](https://engineering.quora.com/)

Reddit: [<u>https://redditblog.com</u>](https://redditblog.com/)

Salesforce: [<u>https://engineering.salesforce.com/blog/</u>](https://engineering.salesforce.com/blog/)

Shopify: [<u>https://engineering.shopify.com</u>](https://engineering.shopify.com/)

Slack: [<u>https://slack.engineering</u>](https://slack.engineering/)

Soundcloud: [<u>https://developers.soundcloud.com/blog</u>](https://developers.soundcloud.com/blog)

Spotify: [<u>https://labs.spotify.com</u>](https://labs.spotify.com/)

Stripe: [<u>https://stripe.com/blog/engineering</u>](https://stripe.com/blog/engineering)

系统设计入门指南（System Design Primer）：[<u>https://github.com/donnemartin/system-design-primer</u>](https://github.com/donnemartin/system-design-primer)

Twitter: [<u>https://blog.twitter.com/engineering/en_us.html</u>](https://blog.twitter.com/engineering/en_us.html)

Thumbtack: [<u>https://www.thumbtack.com/engineering</u>](https://www.thumbtack.com/engineering)

Uber: [<u>http://eng.uber.com</u>](http://eng.uber.com/)

Yahoo: [<u>https://developer.yahoo.com/blogs/</u>](https://developer.yahoo.com/blogs/)

Yelp: [<u>https://engineeringblog.yelp.com</u>](https://engineeringblog.yelp.com/)

Zoom: [<u>https://medium.com/zoom-developer-blog</u>](https://medium.com/zoom-developer-blog)

恭喜！你已经读完本面试指南，并积累了设计系统所需的技能和知识。并非每个人都有足够的自律，去掌握你已经学到的内容。停下来，为自己的努力鼓掌吧，你的付出终会有所回报。

找到理想工作是一段漫长的旅程，需要投入大量时间与精力。熟能生巧，祝你好运！

感谢你购买并阅读本课程。没有像你这样的读者，就不会有我们的工作成果。希望你享受这段学习过程！