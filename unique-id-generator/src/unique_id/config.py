"""Configuration for the Snowflake ID generator."""

from dataclasses import dataclass

from unique_id.exceptions import InvalidConfigError

# Default epoch: 2024-01-01T00:00:00Z in milliseconds
DEFAULT_EPOCH_MS = 1704067200000


@dataclass(frozen=True)
class SnowflakeConfig:
    """Configuration for the Snowflake ID generator.

    Defines the bit layout, custom epoch, and machine identity.
    The total bits (timestamp + datacenter + machine + sequence) must equal 63
    (sign bit is always 0).
    """

    # Bit allocation
    timestamp_bits: int = 41  # ~69 years from epoch
    datacenter_bits: int = 5  # 32 datacenters
    machine_bits: int = 5  # 32 machines per datacenter
    sequence_bits: int = 12  # 4096 IDs per millisecond per machine

    # Identity
    datacenter_id: int = 0
    machine_id: int = 0

    # Epoch
    epoch_ms: int = DEFAULT_EPOCH_MS  # 2024-01-01T00:00:00Z

    def __post_init__(self) -> None:
        """Validate configuration on creation.

        Raises:
            InvalidConfigError: If bit allocation is invalid or IDs exceed capacity.
        """
        # Validate all bit fields are positive
        if self.timestamp_bits <= 0:
            raise InvalidConfigError(
                f"timestamp_bits must be positive, got {self.timestamp_bits}"
            )
        if self.datacenter_bits <= 0:
            raise InvalidConfigError(
                f"datacenter_bits must be positive, got {self.datacenter_bits}"
            )
        if self.machine_bits <= 0:
            raise InvalidConfigError(
                f"machine_bits must be positive, got {self.machine_bits}"
            )
        if self.sequence_bits <= 0:
            raise InvalidConfigError(
                f"sequence_bits must be positive, got {self.sequence_bits}"
            )

        # Validate total bits == 63
        total = (
            self.timestamp_bits
            + self.datacenter_bits
            + self.machine_bits
            + self.sequence_bits
        )
        if total != 63:
            raise InvalidConfigError(
                f"Bit allocation must sum to 63, got {total} "
                f"({self.timestamp_bits}+{self.datacenter_bits}+"
                f"{self.machine_bits}+{self.sequence_bits})"
            )

        # Validate epoch
        if self.epoch_ms <= 0:
            raise InvalidConfigError(
                f"epoch_ms must be positive, got {self.epoch_ms}"
            )

        # Validate datacenter_id fits in allocated bits
        if self.datacenter_id < 0 or self.datacenter_id > self.max_datacenter_id:
            raise InvalidConfigError(
                f"datacenter_id must be in [0, {self.max_datacenter_id}], "
                f"got {self.datacenter_id}"
            )

        # Validate machine_id fits in allocated bits
        if self.machine_id < 0 or self.machine_id > self.max_machine_id:
            raise InvalidConfigError(
                f"machine_id must be in [0, {self.max_machine_id}], "
                f"got {self.machine_id}"
            )

    @property
    def max_datacenter_id(self) -> int:
        """Maximum datacenter ID for configured bits (2^datacenter_bits - 1)."""
        return (1 << self.datacenter_bits) - 1

    @property
    def max_machine_id(self) -> int:
        """Maximum machine ID for configured bits (2^machine_bits - 1)."""
        return (1 << self.machine_bits) - 1

    @property
    def max_sequence(self) -> int:
        """Maximum sequence number for configured bits (2^sequence_bits - 1)."""
        return (1 << self.sequence_bits) - 1

    @property
    def max_timestamp(self) -> int:
        """Maximum timestamp value for configured bits (2^timestamp_bits - 1)."""
        return (1 << self.timestamp_bits) - 1

    @property
    def timestamp_shift(self) -> int:
        """Number of bits to left-shift the timestamp."""
        return self.datacenter_bits + self.machine_bits + self.sequence_bits

    @property
    def datacenter_shift(self) -> int:
        """Number of bits to left-shift the datacenter ID."""
        return self.machine_bits + self.sequence_bits

    @property
    def machine_shift(self) -> int:
        """Number of bits to left-shift the machine ID."""
        return self.sequence_bits
