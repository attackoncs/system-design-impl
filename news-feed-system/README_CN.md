# 新闻推送系统

一个 Python 新闻推送系统库，实现了《系统设计面试》第12章"设计新闻推送系统"中的设计方案。

## 功能特性

- 动态发布和新闻推送检索 API
- 混合推送模型（普通用户推送，名人拉取）
- 异步消息队列与可配置的推送工作线程
- 5层缓存架构（新闻推送、内容、社交图谱、动作、计数器）
- 社交图谱抽象与可插拔后端
- 零运行时依赖（仅使用标准库）

## 安装

```bash
pip install -e ".[dev]"
```

## 快速开始

```python
import asyncio
from news_feed_system import NewsFeedAPI, NewsFeedConfig, FeedRequest

async def main():
    api = NewsFeedAPI(NewsFeedConfig())
    # 完整用法请参见 examples/demo.py
    pass

asyncio.run(main())
```

## 开发

```bash
pip install -e ".[dev]"
pytest
```
