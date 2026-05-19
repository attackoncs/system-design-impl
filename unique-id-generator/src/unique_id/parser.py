"""ID parsing and decomposition."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from unique_id.config import SnowflakeConfig


@dataclass(frozen=True)
class ParsedID:
    """A decomposed Snowflake ID with all extracted components."""

    id_value: int
    timestamp_ms: int  # Milliseconds since Unix epoch (absolute)
    datacenter_id: int
    machine_id: int
    sequence: int
    datetime_utc: datetime  # Converted to UTC datetime

    def __repr__(self) -> str:
        return (
            f"ParsedID(id={self.id_value}, "
            f"timestamp_ms={self.timestamp_ms}, "
            f"datacenter={self.datacenter_id}, "
            f"machine={self.machine_id}, "
            f"sequence={self.sequence}, "
            f"datetime={self.datetime_utc.isoformat()})"
        )


class IDParser:
    """Parses Snowflake IDs back into their constituent components.

    Uses the same configuration (bit layout, epoch) as the generator
    that created the ID to correctly extract fields.
    """

    def __init__(self, config: Optional[SnowflakeConfig] = None):
        """Initialize the parser with a configuration.

        Args:
            config: Configuration matching the generator. Uses defaults if None.
        """
        self._config = config or SnowflakeConfig()

    def parse(self, id_value: int) -> ParsedID:
        """Parse a Snowflake ID into its components.

        Extracts timestamp, datacenter ID, machine ID, and sequence number
        by applying bit masks and shifts based on the configuration.

        Args:
            id_value: The 64-bit Snowflake ID to parse.

        Returns:
            ParsedID with all extracted fields.

        Raises:
            ValueError: If id_value is negative or exceeds 64 bits.
        """
        if id_value < 0:
            raise ValueError(f"ID must be non-negative, got {id_value}")
        if id_value.bit_length() > 63:
            raise ValueError(f"ID exceeds 63 bits: {id_value}")

        config = self._config

        # Extract sequence (lowest bits)
        sequence_mask = (1 << config.sequence_bits) - 1
        sequence = id_value & sequence_mask

        # Extract machine ID
        machine_mask = (1 << config.machine_bits) - 1
        machine_id = (id_value >> config.machine_shift) & machine_mask

        # Extract datacenter ID
        datacenter_mask = (1 << config.datacenter_bits) - 1
        datacenter_id = (id_value >> config.datacenter_shift) & datacenter_mask

        # Extract timestamp offset
        timestamp_mask = (1 << config.timestamp_bits) - 1
        ts_offset = (id_value >> config.timestamp_shift) & timestamp_mask

        # Convert to absolute timestamp
        timestamp_ms = ts_offset + config.epoch_ms

        # Convert to datetime
        datetime_utc = datetime.fromtimestamp(
            timestamp_ms / 1000.0, tz=timezone.utc
        )

        return ParsedID(
            id_value=id_value,
            timestamp_ms=timestamp_ms,
            datacenter_id=datacenter_id,
            machine_id=machine_id,
            sequence=sequence,
            datetime_utc=datetime_utc,
        )
