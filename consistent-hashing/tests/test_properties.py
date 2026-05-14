"""Property-based tests for consistent hashing ring using Hypothesis.

These tests verify correctness invariants across random inputs,
complementing the unit tests with broader coverage.
"""

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from consistent_hashing.ring import ConsistentHashRing


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
# We need at least 2 servers (N existing + 1 new) to test redistribution meaningfully
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

    For a stable ring (no topology changes), the same key shall always
    map to the same server (deterministic).
    """

    @given(servers=server_names_strategy, key=key_strategy)
    @settings(max_examples=200)
    def test_get_node_deterministic(self, servers: list[str], key: str) -> None:
        """For any stable ring and any key, get_node(key) always returns the same server."""
        ring = ConsistentHashRing(nodes=servers, num_virtual_nodes=10)

        # Call get_node multiple times with the same key
        result1 = ring.get_node(key)
        result2 = ring.get_node(key)
        result3 = ring.get_node(key)

        # All calls must return the same server
        assert result1 == result2, (
            f"get_node('{key}') returned '{result1}' first, then '{result2}'"
        )
        assert result2 == result3, (
            f"get_node('{key}') returned '{result2}' second, then '{result3}'"
        )

    @given(servers=server_names_strategy, keys=st.lists(key_strategy, min_size=1, max_size=50))
    @settings(max_examples=100)
    def test_get_node_deterministic_across_keys(self, servers: list[str], keys: list[str]) -> None:
        """For any stable ring, calling get_node on the same set of keys always yields the same mapping."""
        ring = ConsistentHashRing(nodes=servers, num_virtual_nodes=10)

        # Build mapping once
        mapping_first = {k: ring.get_node(k) for k in keys}

        # Build mapping again
        mapping_second = {k: ring.get_node(k) for k in keys}

        assert mapping_first == mapping_second, (
            "Key-to-server mapping changed between two passes over the same keys"
        )


class TestMinimalRedistribution:
    """**Validates: Requirements NFR-2.2**

    Adding or removing a server shall redistribute at most K/N of total
    keys on average, where K is the number of keys and N is the number
    of servers.
    """

    @given(servers=redistribution_servers_strategy, keys=large_key_set_strategy)
    @settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.data_too_large])
    def test_add_server_redistributes_bounded_keys(
        self, servers: list[str], keys: list[str]
    ) -> None:
        """Adding one server to a ring with N servers redistributes at most
        approximately 1/(N+1) of total keys (with 3x tolerance for variance).

        Algorithm:
        1. Create a ring with N-1 servers (all but the last)
        2. Assign all keys to servers (record before mapping)
        3. Add the last server
        4. Record after mapping
        5. Count moved keys
        6. Assert moved_keys <= total_keys * tolerance_factor / N
        """
        # Use all but the last server as the initial ring
        initial_servers = servers[:-1]
        new_server = servers[-1]
        n_initial = len(initial_servers)

        # Create ring with initial servers using moderate virtual nodes
        ring = ConsistentHashRing(nodes=initial_servers, num_virtual_nodes=150)

        # Record key assignments before adding the new server
        assignment_before = {key: ring.get_node(key) for key in keys}

        # Add the new server
        ring.add_node(new_server)

        # Record key assignments after adding the new server
        assignment_after = {key: ring.get_node(key) for key in keys}

        # Count how many keys moved to a different server
        moved_keys = sum(
            1
            for key in keys
            if assignment_before[key] != assignment_after[key]
        )

        # Theoretical bound: total_keys / (N+1) where N+1 is the new total servers
        # We use a generous 3x tolerance to account for statistical variance
        total_keys = len(keys)
        n_after = n_initial + 1
        tolerance_factor = 3.0
        max_allowed_moves = total_keys * tolerance_factor / n_after

        assert moved_keys <= max_allowed_moves, (
            f"Too many keys redistributed when adding server. "
            f"Moved {moved_keys} keys out of {total_keys} total "
            f"(ratio: {moved_keys / total_keys:.3f}). "
            f"Expected at most {max_allowed_moves:.0f} "
            f"(theoretical 1/{n_after} = {1/n_after:.3f}, "
            f"with {tolerance_factor}x tolerance = {tolerance_factor/n_after:.3f}). "
            f"Initial servers: {n_initial}, new total: {n_after}."
        )


class TestCompleteCoverage:
    """**Validates: Requirements FR-4.1**

    Every key maps to exactly one server (get_node always returns a valid
    server from the ring), and all servers with virtual nodes receive at
    least some keys from a large random key set (no server is starved).
    """

    @given(servers=server_names_strategy, key=key_strategy)
    @settings(max_examples=200)
    def test_every_key_maps_to_exactly_one_valid_server(
        self, servers: list[str], key: str
    ) -> None:
        """For any non-empty ring and any key, get_node returns exactly one
        server that is a member of the ring."""
        ring = ConsistentHashRing(nodes=servers, num_virtual_nodes=10)

        result = ring.get_node(key)

        # Result must be a single string (not None, not a list)
        assert isinstance(result, str), (
            f"get_node('{key}') returned {type(result).__name__}, expected str"
        )

        # Result must be one of the servers in the ring
        assert result in servers, (
            f"get_node('{key}') returned '{result}' which is not in the ring. "
            f"Ring servers: {servers}"
        )

    @given(
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
    @settings(
        max_examples=50,
        deadline=None,
        suppress_health_check=[HealthCheck.data_too_large, HealthCheck.too_slow],
    )
    def test_all_servers_receive_keys_from_large_key_set(
        self, servers: list[str], seed: int
    ) -> None:
        """With 2-8 servers (100+ virtual nodes each) and 1000+ unique keys,
        every server on the ring receives at least one key - no server is starved."""
        ring = ConsistentHashRing(nodes=servers, num_virtual_nodes=150)

        # Generate 1000+ unique keys deterministically from the seed
        keys = [f"key-{seed}-{i}" for i in range(1200)]

        # Assign all keys and track which servers received keys
        servers_with_keys: set[str] = set()
        for key in keys:
            server = ring.get_node(key)
            # Each key must map to a valid server
            assert server in servers, (
                f"get_node('{key}') returned '{server}' not in ring servers"
            )
            servers_with_keys.add(server)

        # All servers must have received at least one key
        missing_servers = set(servers) - servers_with_keys
        assert not missing_servers, (
            f"Servers {missing_servers} received no keys from a set of "
            f"{len(keys)} keys. This indicates starvation. "
            f"Total servers: {len(servers)}, "
            f"Servers with keys: {len(servers_with_keys)}"
        )


class TestBalancedDistribution:
    """**Validates: Requirements NFR-2.3**

    With sufficient virtual nodes (>=100 per server), key distribution
    standard deviation shall be within acceptable bounds. No server holds
    more than 2x the mean number of keys.
    """

    @given(
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
    @settings(
        max_examples=50,
        deadline=None,
        suppress_health_check=[HealthCheck.data_too_large, HealthCheck.too_slow],
    )
    def test_no_server_holds_more_than_2x_mean(
        self, servers: list[str], seed: int
    ) -> None:
        """With >=100 virtual nodes per server and >=1000 keys, no server
        holds more than 2x the mean number of keys."""
        ring = ConsistentHashRing(nodes=servers, num_virtual_nodes=150)

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

        # No server should hold more than 2x the mean
        for server, count in counts.items():
            assert count <= 2 * mean, (
                f"Server '{server}' holds {count} keys, which exceeds "
                f"2x the mean ({2 * mean:.0f}). "
                f"Mean: {mean:.1f}, Total keys: {total_keys}, "
                f"Servers: {num_servers}"
            )