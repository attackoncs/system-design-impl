"""Regression checks for input boundaries and cached Redis execution."""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import fakeredis
import pytest
from redis.exceptions import TimeoutError as RedisTimeoutError

from rate_limiter import Algorithm, RateLimiter, RateLimiterConfig, RateLimitRule
from rate_limiter.rule_files import RuleReloader
from rate_limiter.storage.memory import MemoryStorage
from rate_limiter.storage.redis import RedisStorage


@pytest.fixture
def redis_storage():
    with fakeredis.FakeRedis() as client:
        with patch("redis.Redis.from_url", return_value=client):
            storage = RedisStorage()
        try:
            yield storage
        finally:
            storage.close()


@pytest.mark.parametrize("key", ["", "x" * 8193], ids=["empty", "too-long"])
def test_string_request_keys_are_validated_before_charging(key):
    storage = MemoryStorage()
    limiter = RateLimiter(
        RateLimiterConfig(rules=[RateLimitRule(1, 60)], fail_open=False),
        storage=storage,
    )
    with pytest.raises(ValueError, match="key"):
        limiter.check_request(key)
    assert limiter.metrics()["checks"] == 0
    assert not storage._data


def test_deep_json_does_not_stop_rule_reload_and_next_update_succeeds(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text('{"rules":[{"limit":1,"window":60}]}', encoding="utf-8")
    limiter = RateLimiter(RateLimiterConfig(fail_open=False))
    worker = RuleReloader(limiter, path)
    assert worker.poll()
    assert limiter.check_request("user").allowed

    # Under the file-size bound, but deeper than the JSON decoder can handle.
    path.write_text("[" * 3000 + "]" * 3000, encoding="utf-8")
    assert not worker.poll()
    assert worker.status()["errors"] == 1
    assert not limiter.check_request("user").allowed

    path.write_text('{"rules":[{"limit":2,"window":60}]}', encoding="utf-8")
    assert worker.poll()
    assert worker.status()["reloads"] == 2
    assert worker.status()["last_error"] is None
    assert limiter.check_request("user").remaining == 1


def test_warm_redis_check_uses_digest_without_resending_script(redis_storage):
    limiter = RateLimiter(RateLimiterConfig(fail_open=False), storage=redis_storage)
    rule = RateLimitRule(10, 3600)
    assert limiter.check("user", rule).allowed
    client = redis_storage._client
    with patch.object(
        client, "execute_command", wraps=client.execute_command
    ) as command:
        assert limiter.check("user", rule).remaining == 8
    calls = command.call_args_list
    assert len(calls) == 1
    assert calls[0].args[0] == "EVALSHA"
    assert len(calls[0].args[1]) == 40


@pytest.mark.parametrize("algorithm", list(Algorithm))
def test_script_cache_loss_preserves_shared_quota(redis_storage, algorithm):
    first = RateLimiter(RateLimiterConfig(fail_open=False), storage=redis_storage)
    second = RateLimiter(RateLimiterConfig(fail_open=False), storage=redis_storage)
    rule = RateLimitRule(2, 3600, algorithm)
    assert first.check("user", rule).remaining == 1
    redis_storage._client.script_flush()
    assert second.check("user", rule).remaining == 0
    assert not first.check("user", rule).allowed
    assert first.metrics()["storage_errors"] == 0
    assert second.metrics()["storage_errors"] == 0


@pytest.mark.parametrize("algorithm", list(Algorithm))
def test_cold_script_cache_has_exact_concurrent_admission(redis_storage, algorithm):
    limiter = RateLimiter(RateLimiterConfig(fail_open=False), storage=redis_storage)
    rule = RateLimitRule(7, 3600, algorithm)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: limiter.check("user", rule), range(64)))
    assert sum(result.allowed for result in results) == 7
    assert limiter.metrics()["storage_errors"] == 0


@pytest.mark.parametrize("fail_open", [True, False])
def test_script_timeout_is_not_retried(redis_storage, fail_open):
    limiter = RateLimiter(RateLimiterConfig(fail_open=fail_open), storage=redis_storage)
    client = redis_storage._client
    with patch.object(client, "evalsha", side_effect=RedisTimeoutError) as execute:
        with patch.object(client, "script_load", wraps=client.script_load) as load:
            result = limiter.check("user", RateLimitRule(1, 60))
    assert result.allowed is fail_open
    assert execute.call_count == 1
    load.assert_not_called()
    assert limiter.metrics()["storage_errors"] == 1
