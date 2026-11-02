"""Property-based tests for consistent hashing ring using Hypothesis.

These tests verify correctness invariants across random inputs, complementing
the unit tests with broader coverage.
"""

from statistics import pstdev

import consistent_hashing.ring
import hypothesis
from hypothesis import strategies as st
import pytest

from consistent_hashing import compute_distribution

# --- Strategies ---

# Strategy for generating server names: non-empty strings that are unique
server_names_strategy = st.lists(
    st.text(
        alphabet=st.characters(whitelist_categories=("L", "N", "P")),
        min_size=1,
        max_size=20,
    ),
    min_size=1,
    max_size=10,
    unique=True,
)

# Strategy for generating key strings
key_strategy = st.text(min_size=0, max_size=100)

# Strategy for generating a set of servers for redistribution tests
# We need at least 2 servers (N existing + 1 new) to test redistribution
# meaningfully
redistribution_servers_strategy = st.lists(
    st.text(
        alphabet=st.characters(whitelist_categories=("L", "N")),
        min_size=1,
        max_size=15,
    ),
    min_size=3,
    max_size=10,
    unique=True,
)

# Strategy for generating a large set of keys for statistical tests
large_key_set_strategy = st.lists(
    st.text(
        alphabet=st.characters(whitelist_categories=("L", "N", "P")),
        min_size=1,
        max_size=30,
    ),
    min_size=500,
    max_size=1000,
    unique=True,
)


# --- Property Tests ---


class TestDeterminism:
    """**Validates: Requirements NFR-2.1**

    For a stable ring (no topology changes), the same key shall always map to
    the same server (deterministic).
    """

    @hypothesis.given(servers=server_names_strategy, key=key_strategy)
    @hypothesis.settings(max_examples=200)
    def test_get_node_deterministic(self, servers: list[str], key: str) -> None:
        """For any stable ring and any key, get_node(key) always returns the
        same server.
        """
        ring = consistent_hashing.ring.ConsistentHashRing(nodes=servers, num_virtual_nodes=10)

        # Call get_node multiple times with the same key
        result1 = ring.get_node(key)
        result2 = ring.get_node(key)
        result3 = ring.get_node(key)

        # All calls must return the same server
        assert (
            result1 == result2
        ), f"get_node('{key}') returned '{result1}' first, then '{result2}'"
        assert (
            result2 == result3
        ), f"get_node('{key}') returned '{result2}' second, then '{result3}'"

    @hypothesis.given(
        servers=server_names_strategy,
        keys=st.lists(key_strategy, min_size=1, max_size=50),
    )
    @hypothesis.settings(max_examples=100)
    def test_get_node_deterministic_across_keys(
        self, servers: list[str], keys: list[str]
    ) -> None:
        """For any stable ring, calling get_node on the same set of keys always
        yields the same mapping.
        """
        ring = consistent_hashing.ring.ConsistentHashRing(nodes=servers, num_virtual_nodes=10)

        # Build mapping once
        mapping_first = {k: ring.get_node(k) for k in keys}

        # Build mapping again
        mapping_second = {k: ring.get_node(k) for k in keys}

        assert (
            mapping_first == mapping_second
        ), "Key-to-server mapping changed between two passes over the same keys"


class TestMinimalRedistribution:
    """**Validates: Requirements NFR-2.2**

    Addition only moves keys to the new server; removal only moves its keys.
    Average migration ratios are not bounds for arbitrary candidate keys.
    """

    @hypothesis.given(servers=redistribution_servers_strategy, keys=large_key_set_strategy)
    @hypothesis.settings(
        max_examples=50,
        deadline=None,
        suppress_health_check=[hypothesis.HealthCheck.data_too_large],
    )
    def test_add_remove_preserve_unaffected_owners(
        self, servers: list[str], keys: list[str]
    ) -> None:
        """Adding then removing a member preserves every other assignment."""
        # Use all but the last server as the initial ring
        initial_servers = servers[:-1]
        new_server = servers[-1]

        # Create ring with initial servers using moderate virtual nodes
        ring = consistent_hashing.ring.ConsistentHashRing(nodes=initial_servers, num_virtual_nodes=150)

        # Record key assignments before adding the new server
        assignment_before = {key: ring.get_node(key) for key in keys}

        # Add the new server
        ring.add_node(new_server)

        # Record key assignments after adding the new server
        assignment_after = {key: ring.get_node(key) for key in keys}

        for key in keys:
            if assignment_before[key] != assignment_after[key]:
                assert assignment_after[key] == new_server

        ring.remove_node(new_server)
        assert dict(ring.iter_assignments(keys)) == assignment_before


class TestCompleteCoverage:
    """**Validates: Requirements FR-4.1**

    Every key has one valid owner. Statistics retain zero-count members too;
    a finite sample need not cover every server.
    """

    @hypothesis.given(servers=server_names_strategy, key=key_strategy)
    @hypothesis.settings(max_examples=200)
    def test_every_key_maps_to_exactly_one_valid_server(
        self, servers: list[str], key: str
    ) -> None:
        """For any non-empty ring and any key, get_node returns exactly one
        server that is a member of the ring.
        """
        ring = consistent_hashing.ring.ConsistentHashRing(nodes=servers, num_virtual_nodes=10)

        result = ring.get_node(key)

        # Result must be a single string (not None, not a list)
        assert isinstance(
            result, str
        ), f"get_node('{key}') returned {type(result).__name__}, expected str"

        # Result must be one of the servers in the ring
        assert result in servers, (
            f"get_node('{key}') returned '{result}' which is not in the ring. "
            f"Ring servers: {servers}"
        )

    @hypothesis.given(
        servers=st.lists(
            st.text(
                alphabet=st.characters(whitelist_categories=("L", "N", "P")),
                min_size=1,
                max_size=20,
            ),
            min_size=2,
            max_size=8,
            unique=True,
        ),
        seed=st.integers(min_value=0, max_value=2**32 - 1),
    )
    @hypothesis.settings(
        max_examples=50,
        deadline=None,
        suppress_health_check=[
            hypothesis.HealthCheck.data_too_large,
            hypothesis.HealthCheck.too_slow,
        ],
    )
    def test_distribution_retains_zero_count_members(
        self, servers: list[str], seed: int
    ) -> None:
        """A concentrated sample is accounted for without dropping members."""
        ring = consistent_hashing.ring.ConsistentHashRing(nodes=servers, num_virtual_nodes=150)

        key = f"key-{seed}"
        owner = ring.get_node(key)
        result = compute_distribution(ring, iter([key] * 1200))
        assert result.total_keys == 1200
        assert result.keys_per_server == {
            server: 1200 if server == owner else 0 for server in servers
        }
        assert result.min_keys == 0
        assert result.max_keys == 1200


class TestBalancedDistribution:
    """**Validates: Requirements NFR-2.3**

    Sampled balance metrics must match observed counts. More virtual nodes do
    not impose a universal upper bound on the load of any server.
    """

    @hypothesis.given(
        servers=st.lists(
            st.text(
                alphabet=st.characters(whitelist_categories=("L", "N")),
                min_size=1,
                max_size=15,
            ),
            min_size=2,
            max_size=8,
            unique=True,
        ),
        seed=st.integers(min_value=0, max_value=2**32 - 1),
    )
    @hypothesis.settings(
        max_examples=50,
        deadline=None,
        suppress_health_check=[
            hypothesis.HealthCheck.data_too_large,
            hypothesis.HealthCheck.too_slow,
        ],
    )
    def test_balance_metrics_match_observed_assignments(
        self, servers: list[str], seed: int
    ) -> None:
        """Compare reported population statistics with independent counts."""
        ring = consistent_hashing.ring.ConsistentHashRing(nodes=servers, num_virtual_nodes=150)

        # Generate 1500 unique keys
        keys = [f"balance-{seed}-{i}" for i in range(1500)]

        # Count keys per server
        counts: dict[str, int] = {s: 0 for s in servers}
        for key in keys:
            server = ring.get_node(key)
            counts[server] += 1

        total_keys = len(keys)
        num_servers = len(servers)
        mean = total_keys / num_servers

        result = compute_distribution(ring, iter(keys))
        expected_std_dev = pstdev(counts.values())
        assert result.keys_per_server == counts
        assert result.total_keys == total_keys
        assert result.num_servers == num_servers
        assert result.mean == mean
        assert result.std_dev == pytest.approx(expected_std_dev)
        assert result.balance_ratio == pytest.approx(expected_std_dev / mean)
        assert result.min_keys == min(counts.values())
        assert result.max_keys == max(counts.values())
