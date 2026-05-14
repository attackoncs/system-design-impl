# System Design Interview - Python Implementations

本项目是《System Design Interview》书中各章节系统设计的 Python 实现。每个章节作为独立的子项目，包含完整的设计文档、源代码、测试和使用示例。

## 已实现

| 章节 | 主题 | 状态 | 说明 |
|------|------|------|------|
| 05 | [Rate Limiter](./rate-limiter/) | ✅ 完成 | 支持 5 种算法、Redis/内存后端、FastAPI/Flask 集成 |
| 06 | [Consistent Hashing](./consistent-hashing/) | ✅ 完成 | 虚拟节点、O(log N) 查找、分布统计 |

## 计划实现

| 章节 | 主题 | 状态 |
|------|------|------|
| 07 | Key-Value Store | 📋 待实现 |
| 08 | Unique ID Generator | 📋 待实现 |
| 09 | URL Shortener | 📋 待实现 |
| 10 | Web Crawler | 📋 待实现 |
| 11 | Notification System | 📋 待实现 |
| 12 | News Feed System | 📋 待实现 |
| 13 | Chat System | 📋 待实现 |
| 14 | Search Autocomplete | 📋 待实现 |

## 项目结构

```
sdi-implement/
├── README.md                    # 本文件
├── System Design Interview.md   # 原书内容参考
├── rate-limiter/                # 第05章：限流器
│   ├── README.md               # 英文使用文档
│   ├── README_CN.md            # 中文使用文档
│   ├── pyproject.toml          # 项目配置
│   ├── src/rate_limiter/       # 源代码
│   ├── tests/                  # 测试
│   ├── examples/               # 使用示例
│   └── docs/                   # 设计规格文档
├── consistent-hashing/          # 第06章：一致性哈希
│   ├── README.md               # 英文使用文档
│   ├── README_CN.md            # 中文使用文档
│   ├── pyproject.toml          # 项目配置
│   ├── src/consistent_hashing/ # 源代码
│   ├── tests/                  # 测试
│   ├── examples/               # 使用示例
│   └── docs/                   # 设计规格文档
└── .gitignore
```

## 设计原则

每个子项目遵循以下原则：

1. **独立可运行** — 每个子项目有自己的 `pyproject.toml`，可独立安装和测试
2. **完整测试覆盖** — 使用 pytest，包含单元测试和集成测试
3. **文档齐全** — README、架构文档、设计规格
4. **生产级质量** — 类型注解、错误处理、性能考虑
5. **忠于原书** — 实现书中描述的核心概念和算法

## 快速开始

```bash
# 进入某个子项目
cd rate-limiter

# 安装依赖
pip install -e ".[dev]"

# 运行测试
pytest

# 运行示例
python examples/basic_usage.py
```

## 环境要求

- Python >= 3.9
- 各子项目可能有额外依赖（见各自的 pyproject.toml）

## License

MIT
