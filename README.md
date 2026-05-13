# System Design Interview - Python Implementations

Production-quality Python implementations of system design concepts from the book *System Design Interview* by Alex Xu. Each chapter is a self-contained sub-project with full documentation, source code, tests, and usage examples.

> 中文版本请参阅 [README_CN.md](./README_CN.md)

## Completed

| Chapter | Topic | Status | Description |
|---------|-------|--------|-------------|
| 05 | [Rate Limiter](./rate-limiter/) | ✅ Done | 5 algorithms, Redis/memory backends, FastAPI/Flask integration |

## Planned

| Chapter | Topic | Status |
|---------|-------|--------|
| 06 | Consistent Hashing | 📋 Planned |
| 07 | Key-Value Store | 📋 Planned |
| 08 | Unique ID Generator | 📋 Planned |
| 09 | URL Shortener | 📋 Planned |
| 10 | Web Crawler | 📋 Planned |
| 11 | Notification System | 📋 Planned |
| 12 | News Feed System | 📋 Planned |
| 13 | Chat System | 📋 Planned |
| 14 | Search Autocomplete | 📋 Planned |

## Project Structure

```
sdi-implement/
├── README.md                    # This file
├── README_CN.md                 # Chinese version
├── System Design Interview.md   # Book content reference
├── rate-limiter/                # Ch.05: Rate Limiter
│   ├── README.md               # Usage documentation
│   ├── ARCHITECTURE.md         # Architecture design (Chinese)
│   ├── pyproject.toml           # Package configuration
│   ├── src/rate_limiter/       # Source code
│   ├── tests/                  # Tests (180 tests)
│   ├── examples/               # Usage examples
│   └── docs/                   # Design spec documents
├── <next-project>/             # Next implementation...
└── .gitignore
```

## Design Principles

Each sub-project follows these principles:

1. **Self-contained** — Each has its own `pyproject.toml` and can be installed/tested independently
2. **Fully tested** — pytest with unit and integration tests
3. **Well documented** — README, architecture docs, design specs
4. **Production quality** — Type hints, error handling, performance considerations
5. **Faithful to the book** — Implements the core concepts and algorithms described in each chapter

## Quick Start

```bash
# Enter a sub-project
cd rate-limiter

# Install dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run examples
python examples/basic_usage.py
```

## Requirements

- Python >= 3.9
- Additional dependencies vary by sub-project (see each `pyproject.toml`)

## License

MIT
