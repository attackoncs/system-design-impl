"""Unique ID Generator - Twitter Snowflake algorithm implementation.

Generates 64-bit unique, time-sortable IDs suitable for distributed systems.
"""

__version__ = "0.1.0"

from unique_id.config import SnowflakeConfig
from unique_id.clock import Clock, SystemClock, MonotonicClock
from unique_id.snowflake import SnowflakeGenerator
from unique_id.parser import IDParser, ParsedID
from unique_id.strategies import IDGenerator, UUIDGenerator, TimestampRandomGenerator
from unique_id.exceptions import (
    UniqueIDError,
    ClockMovedBackwardsError,
    SequenceOverflowError,
    InvalidConfigError,
)

__all__ = [
    "SnowflakeGenerator",
    "SnowflakeConfig",
    "IDParser",
    "ParsedID",
    "Clock",
    "SystemClock",
    "MonotonicClock",
    "IDGenerator",
    "UUIDGenerator",
    "TimestampRandomGenerator",
    "UniqueIDError",
    "ClockMovedBackwardsError",
    "SequenceOverflowError",
    "InvalidConfigError",
]
