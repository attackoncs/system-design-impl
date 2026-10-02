# News Feed System

A Python news feed system library implementing the design from *System Design Interview* Chapter 12 "Design A News Feed System".

## Features

- Feed Publishing and News Feed Retrieval APIs
- Hybrid fanout model (push for normal users, pull for celebrities)
- Async message queue with configurable fanout workers
- 5-layer cache architecture (News Feed, Content, Social Graph, Action, Counters)
- Social graph abstraction with pluggable backends
- Zero runtime dependencies (stdlib only)

## Installation

```bash
pip install -e ".[dev]"
```

## Quick Start

```python
import asyncio
from news_feed_system import NewsFeedAPI, NewsFeedConfig, FeedRequest

async def main():
    api = NewsFeedAPI(NewsFeedConfig())
    # See examples/demo.py for full usage
    pass

asyncio.run(main())
```

## Development

```bash
pip install -e ".[dev]"
pytest
```
