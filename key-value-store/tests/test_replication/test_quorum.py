"""Tests for the Quorum Logic implementation."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from kv_store.config import ReplicationConfig
from kv_store.replication.quorum import (
    ConsistencyLevel,
    QuorumConfig,
    QuorumManager,
    QuorumResult,
)
from kv_store.replication.vector_clock import VectorClock


class TestQuorumConfigStrongConsistency:
    """Tests for QuorumConfig.is_strongly_consistent()."""

    def test_strong_consistency_w2_r2_n3(self):
        """W=2, R=2, N=3 is strongly consistent (2+2 > 3)."""
        config = QuorumConfig(n=3, w=2, r=2)
        assert config.is_strongly_consistent() is True

    def test_not_strong_consistency_w1_r1_n3(self):
        """W=1, R=1, N=3 is NOT strongly consistent (1+1 < 3)."""
        config = QuorumConfig(n=3, w=1, r=1)
        assert config.is_strongly_consistent() is False

    def test_strong_consistency_all(self):
        """W=N, R=N is strongly consistent."""
        config = QuorumConfig(n=3, w=3, r=3)
        assert config.is_strongly_consistent() is True

    def test_boundary_w2_r1_n3(self):
        """W=2, R=1, N=3 is NOT strongly consistent (2+1 = 3, not > 3)."""
        config = QuorumConfig(n=3, w=2, r=1)
        assert config.is_strongly_consistent() is False

    def test_strong_consistency_w3_r1_n3(self):
        """W=3, R=1, N=3 is strongly consistent (3+1 > 3)."""
        config = QuorumConfig(n=3, w=3, r=1)
        assert config.is_strongly_consistent() is True


class TestQuorumConfigFromConsistencyLevel:
    """Tests for QuorumConfig.from_consistency_level()."""

    def test_one_level_produces_w1_r1(self):
        """ConsistencyLevel.ONE produces W=1, R=1."""
        config = QuorumConfig.from_consistency_level(ConsistencyLevel.ONE, n=3)
        assert config.n == 3
        assert config.w == 1
        assert config.r == 1

    def test_quorum_level_produces_majority(self):
        """ConsistencyLevel.QUORUM produces W=2, R=2 for N=3."""
        config = QuorumConfig.from_consistency_level(ConsistencyLevel.QUORUM, n=3)
        assert config.n == 3
        assert config.w == 2
        assert config.r == 2

    def test_quorum_level_n5(self):
        """ConsistencyLevel.QUORUM produces W=3, R=3 for N=5."""
        config = QuorumConfig.from_consistency_level(ConsistencyLevel.QUORUM, n=5)
        assert config.n == 5
        assert config.w == 3
        assert config.r == 3

    def test_all_level_produces_wn_rn(self):
        """ConsistencyLevel.ALL produces W=N, R=N."""
        config = QuorumConfig.from_consistency_level(ConsistencyLevel.ALL, n=3)
        assert config.n == 3
        assert config.w == 3
        assert config.r == 3

    def test_default_n_is_3(self):
        """Default N is 3 when not specified."""
        config = QuorumConfig.from_consistency_level(ConsistencyLevel.QUORUM)
        assert config.n == 3

    def test_quorum_level_is_strongly_consistent(self):
        """QUORUM level always produces a strongly consistent config."""
        config = QuorumConfig.from_consistency_level(ConsistencyLevel.QUORUM, n=3)
        assert config.is_strongly_consistent() is True

    def test_one_level_is_not_strongly_consistent(self):
        """ONE level is not strongly consistent for N >= 3."""
        config = QuorumConfig.from_consistency_level(ConsistencyLevel.ONE, n=3)
        assert config.is_strongly_consistent() is False


class TestQuorumManagerGetConfig:
    """Tests for QuorumManager.get_quorum_config()."""

    def test_default_config_from_replication_config(self):
        """Without consistency override, uses ReplicationConfig defaults."""
        repl_config = ReplicationConfig(n_replicas=3, w_quorum=2, r_quorum=2)
        manager = QuorumManager(config=repl_config)
        qc = manager.get_quorum_config()
        assert qc.n == 3
        assert qc.w == 2
        assert qc.r == 2

    def test_override_with_consistency_level(self):
        """With consistency override, uses the specified level."""
        repl_config = ReplicationConfig(n_replicas=3, w_quorum=2, r_quorum=2)
        manager = QuorumManager(config=repl_config)
        qc = manager.get_quorum_config(consistency=ConsistencyLevel.ONE)
        assert qc.w == 1
        assert qc.r == 1

    def test_override_all_level(self):
        """ALL consistency level overrides to W=N, R=N."""
        repl_config = ReplicationConfig(n_replicas=3, w_quorum=2, r_quorum=2)
        manager = QuorumManager(config=repl_config)
        qc = manager.get_quorum_config(consistency=ConsistencyLevel.ALL)
        assert qc.w == 3
        assert qc.r == 3


class TestWriteQuorum:
    """Tests for QuorumManager.write_quorum()."""

    @pytest.fixture
    def repl_config(self):
        return ReplicationConfig(n_replicas=3, w_quorum=2, r_quorum=2)

    @pytest.fixture
    def vector_clock(self):
        return VectorClock(entries={"node-1": 1})

    @pytest.fixture
    def replica_nodes(self):
        return ["node-1", "node-2", "node-3"]

    @pytest.fixture
    def quorum_config(self):
        return QuorumConfig(n=3, w=2, r=2)

    @pytest.mark.asyncio
    async def test_write_succeeds_with_w_acks(
        self, repl_config, vector_clock, replica_nodes, quorum_config
    ):
        """Write quorum succeeds when W nodes acknowledge."""
        # All 3 nodes succeed
        write_func = AsyncMock(return_value=True)
        manager = QuorumManager(config=repl_config, write_func=write_func)

        result = await manager.write_quorum(
            key="test-key",
            value=b"test-value",
            vector_clock=vector_clock,
            replica_nodes=replica_nodes,
            quorum_config=quorum_config,
        )

        assert result.success is True
        assert result.responses_received == 3
        assert result.responses_required == 2
        assert result.failed_nodes == []

    @pytest.mark.asyncio
    async def test_write_succeeds_with_exactly_w_acks(
        self, repl_config, vector_clock, replica_nodes, quorum_config
    ):
        """Write quorum succeeds when exactly W nodes acknowledge (one fails)."""
        # 2 succeed, 1 fails
        call_count = 0

        async def write_func(node_id, key, value, vc):
            nonlocal call_count
            call_count += 1
            if node_id == "node-3":
                raise ConnectionError("Node unavailable")
            return True

        manager = QuorumManager(config=repl_config, write_func=write_func)

        result = await manager.write_quorum(
            key="test-key",
            value=b"test-value",
            vector_clock=vector_clock,
            replica_nodes=replica_nodes,
            quorum_config=quorum_config,
        )

        assert result.success is True
        assert result.responses_received == 2
        assert result.responses_required == 2
        assert result.failed_nodes == ["node-3"]

    @pytest.mark.asyncio
    async def test_write_fails_with_fewer_than_w_acks(
        self, repl_config, vector_clock, replica_nodes, quorum_config
    ):
        """Write quorum fails when fewer than W nodes acknowledge."""
        # Only 1 succeeds, 2 fail
        async def write_func(node_id, key, value, vc):
            if node_id == "node-1":
                return True
            raise ConnectionError("Node unavailable")

        manager = QuorumManager(config=repl_config, write_func=write_func)

        result = await manager.write_quorum(
            key="test-key",
            value=b"test-value",
            vector_clock=vector_clock,
            replica_nodes=replica_nodes,
            quorum_config=quorum_config,
        )

        assert result.success is False
        assert result.responses_received == 1
        assert result.responses_required == 2
        assert "node-2" in result.failed_nodes
        assert "node-3" in result.failed_nodes

    @pytest.mark.asyncio
    async def test_write_fails_when_all_nodes_fail(
        self, repl_config, vector_clock, replica_nodes, quorum_config
    ):
        """Write quorum fails when all nodes fail."""
        async def write_func(node_id, key, value, vc):
            raise ConnectionError("Node unavailable")

        manager = QuorumManager(config=repl_config, write_func=write_func)

        result = await manager.write_quorum(
            key="test-key",
            value=b"test-value",
            vector_clock=vector_clock,
            replica_nodes=replica_nodes,
            quorum_config=quorum_config,
        )

        assert result.success is False
        assert result.responses_received == 0
        assert result.responses_required == 2
        assert len(result.failed_nodes) == 3

    @pytest.mark.asyncio
    async def test_write_false_return_counts_as_failure(
        self, repl_config, vector_clock, replica_nodes, quorum_config
    ):
        """A write function returning False counts as a failure."""
        async def write_func(node_id, key, value, vc):
            if node_id == "node-1":
                return True
            return False

        manager = QuorumManager(config=repl_config, write_func=write_func)

        result = await manager.write_quorum(
            key="test-key",
            value=b"test-value",
            vector_clock=vector_clock,
            replica_nodes=replica_nodes,
            quorum_config=quorum_config,
        )

        assert result.success is False
        assert result.responses_received == 1
        assert "node-2" in result.failed_nodes
        assert "node-3" in result.failed_nodes

    @pytest.mark.asyncio
    async def test_write_sends_to_all_replicas_concurrently(
        self, repl_config, vector_clock, replica_nodes, quorum_config
    ):
        """Write sends requests to all replica nodes."""
        called_nodes = []

        async def write_func(node_id, key, value, vc):
            called_nodes.append(node_id)
            return True

        manager = QuorumManager(config=repl_config, write_func=write_func)

        await manager.write_quorum(
            key="test-key",
            value=b"test-value",
            vector_clock=vector_clock,
            replica_nodes=replica_nodes,
            quorum_config=quorum_config,
        )

        assert set(called_nodes) == {"node-1", "node-2", "node-3"}

    @pytest.mark.asyncio
    async def test_write_raises_without_write_func(
        self, repl_config, vector_clock, replica_nodes, quorum_config
    ):
        """Write raises RuntimeError if no write function is configured."""
        manager = QuorumManager(config=repl_config)

        with pytest.raises(RuntimeError, match="No write function configured"):
            await manager.write_quorum(
                key="test-key",
                value=b"test-value",
                vector_clock=vector_clock,
                replica_nodes=replica_nodes,
                quorum_config=quorum_config,
            )


class TestReadQuorum:
    """Tests for QuorumManager.read_quorum()."""

    @pytest.fixture
    def repl_config(self):
        return ReplicationConfig(n_replicas=3, w_quorum=2, r_quorum=2)

    @pytest.fixture
    def replica_nodes(self):
        return ["node-1", "node-2", "node-3"]

    @pytest.fixture
    def quorum_config(self):
        return QuorumConfig(n=3, w=2, r=2)

    @pytest.mark.asyncio
    async def test_read_succeeds_with_r_responses(
        self, repl_config, replica_nodes, quorum_config
    ):
        """Read quorum succeeds when R nodes respond."""
        vc = VectorClock(entries={"node-1": 1})

        async def read_func(node_id, key):
            return (b"value-from-" + node_id.encode(), vc)

        manager = QuorumManager(config=repl_config, read_func=read_func)

        result = await manager.read_quorum(
            key="test-key",
            replica_nodes=replica_nodes,
            quorum_config=quorum_config,
        )

        assert result.success is True
        assert result.responses_received == 3
        assert result.responses_required == 2
        assert result.failed_nodes == []
        assert len(result.values) == 3

    @pytest.mark.asyncio
    async def test_read_succeeds_with_exactly_r_responses(
        self, repl_config, replica_nodes, quorum_config
    ):
        """Read quorum succeeds when exactly R nodes respond (one fails)."""
        vc = VectorClock(entries={"node-1": 1})

        async def read_func(node_id, key):
            if node_id == "node-3":
                raise ConnectionError("Node unavailable")
            return (b"value", vc)

        manager = QuorumManager(config=repl_config, read_func=read_func)

        result = await manager.read_quorum(
            key="test-key",
            replica_nodes=replica_nodes,
            quorum_config=quorum_config,
        )

        assert result.success is True
        assert result.responses_received == 2
        assert result.responses_required == 2
        assert result.failed_nodes == ["node-3"]
        assert len(result.values) == 2

    @pytest.mark.asyncio
    async def test_read_fails_with_fewer_than_r_responses(
        self, repl_config, replica_nodes, quorum_config
    ):
        """Read quorum fails when fewer than R nodes respond."""
        vc = VectorClock(entries={"node-1": 1})

        async def read_func(node_id, key):
            if node_id == "node-1":
                return (b"value", vc)
            raise ConnectionError("Node unavailable")

        manager = QuorumManager(config=repl_config, read_func=read_func)

        result = await manager.read_quorum(
            key="test-key",
            replica_nodes=replica_nodes,
            quorum_config=quorum_config,
        )

        assert result.success is False
        assert result.responses_received == 1
        assert result.responses_required == 2
        assert "node-2" in result.failed_nodes
        assert "node-3" in result.failed_nodes
        assert len(result.values) == 1

    @pytest.mark.asyncio
    async def test_read_collects_values_from_responding_nodes(
        self, repl_config, replica_nodes, quorum_config
    ):
        """Read quorum collects (value, vector_clock) pairs from responding nodes."""
        vc1 = VectorClock(entries={"node-1": 1})
        vc2 = VectorClock(entries={"node-1": 2})

        async def read_func(node_id, key):
            if node_id == "node-1":
                return (b"old-value", vc1)
            elif node_id == "node-2":
                return (b"new-value", vc2)
            else:
                return (b"new-value", vc2)

        manager = QuorumManager(config=repl_config, read_func=read_func)

        result = await manager.read_quorum(
            key="test-key",
            replica_nodes=replica_nodes,
            quorum_config=quorum_config,
        )

        assert result.success is True
        assert len(result.values) == 3
        # Verify we got the different values
        values_bytes = [v[0] for v in result.values]
        assert b"old-value" in values_bytes
        assert b"new-value" in values_bytes

    @pytest.mark.asyncio
    async def test_read_none_response_counts_as_success(
        self, repl_config, replica_nodes, quorum_config
    ):
        """A None response (key not found) still counts toward quorum."""
        async def read_func(node_id, key):
            return None  # Key not found

        manager = QuorumManager(config=repl_config, read_func=read_func)

        result = await manager.read_quorum(
            key="test-key",
            replica_nodes=replica_nodes,
            quorum_config=quorum_config,
        )

        assert result.success is True
        assert result.responses_received == 3
        assert result.values == []  # No values since all returned None

    @pytest.mark.asyncio
    async def test_read_raises_without_read_func(
        self, repl_config, replica_nodes, quorum_config
    ):
        """Read raises RuntimeError if no read function is configured."""
        manager = QuorumManager(config=repl_config)

        with pytest.raises(RuntimeError, match="No read function configured"):
            await manager.read_quorum(
                key="test-key",
                replica_nodes=replica_nodes,
                quorum_config=quorum_config,
            )

    @pytest.mark.asyncio
    async def test_read_fails_when_all_nodes_fail(
        self, repl_config, replica_nodes, quorum_config
    ):
        """Read quorum fails when all nodes fail."""
        async def read_func(node_id, key):
            raise ConnectionError("Node unavailable")

        manager = QuorumManager(config=repl_config, read_func=read_func)

        result = await manager.read_quorum(
            key="test-key",
            replica_nodes=replica_nodes,
            quorum_config=quorum_config,
        )

        assert result.success is False
        assert result.responses_received == 0
        assert result.responses_required == 2
        assert len(result.failed_nodes) == 3
        assert result.values == []
