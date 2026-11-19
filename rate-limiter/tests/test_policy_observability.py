"""Policy preflight, global quota and bounded observability regressions."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from rate_limiter import Algorithm, RateLimiter, RateLimiterConfig, RateLimitRule
from rate_limiter.rule_files import parse_rules
from rate_limiter.storage.memory import MemoryStorage


def request(subject):
    return SimpleNamespace(remote_addr=subject, path="/api/orders")


@pytest.mark.parametrize("failure", ["empty", "oversized", "exception"])
def test_all_subjects_are_validated_before_any_quota_is_charged(failure):
    first = RateLimitRule(1, 60, name="valid")

    def invalid(_):
        if failure == "exception":
            raise ValueError("identity unavailable")
        return "" if failure == "empty" else "x" * 8193

    second = RateLimitRule(1, 60, key_func=invalid, name="invalid")
    limiter = RateLimiter(RateLimiterConfig(rules=[first, second]))
    with pytest.raises(ValueError):
        limiter.check_request(request("client"))
    assert limiter.metrics()["checks"] == 0
    assert limiter.check("client", first).allowed


@pytest.mark.parametrize("algorithm", list(Algorithm))
def test_global_quota_is_shared_by_every_subject_and_configurable_in_json(algorithm):
    payload = (
        '{"rules":[{"limit":3,"window":60,"key_type":"global",'
        f'"algorithm":"{algorithm.value}"' + "}]}"
    ).encode()
    limiter = RateLimiter(
        RateLimiterConfig(rules=parse_rules(payload), fail_open=False),
        storage=MemoryStorage(clock=lambda: 120),
    )
    assert all(limiter.check_request(request(str(i))).allowed for i in range(3))
    assert not limiter.check_request(request("new-client")).allowed
    assert not limiter.check_request("explicit-subject").allowed


def test_global_quota_can_be_overridden_by_custom_resolver():
    rule = RateLimitRule(1, 60, key_type="global", key_func=lambda req: req.remote_addr)
    limiter = RateLimiter(RateLimiterConfig(rules=[rule]))
    assert limiter.check_request(request("a")).allowed
    assert limiter.check_request(request("b")).allowed
    assert not limiter.check_request(request("a")).allowed


def test_global_quota_is_atomic_under_concurrent_subjects():
    limiter = RateLimiter(
        RateLimiterConfig(rules=[RateLimitRule(13, 60, key_type="global")]),
        storage=MemoryStorage(clock=lambda: 120),
    )
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(limiter.check_request, map(request, map(str, range(100)))))
    assert sum(result.allowed for result in results) == 13
    assert limiter.metrics()["storage_errors"] == 0


def test_rule_metrics_aggregate_subjects_without_retaining_identities():
    first = RateLimitRule(1, 60, Algorithm.FIXED_WINDOW, name="login")
    second = RateLimitRule(2, 60, Algorithm.FIXED_WINDOW, name="orders")
    limiter = RateLimiter(RateLimiterConfig(rules=[first, first, second]))
    limiter.check_request(request("private-client-a"))
    limiter.check_request(request("private-client-a"))
    limiter.check_request(request("private-client-b"))
    metrics = limiter.rule_metrics()
    assert len(metrics) == 2
    by_name = {entry["name"]: entry for entry in metrics}
    assert by_name["login"]["checks"] == 3
    assert by_name["login"]["allowed"] == 2
    assert by_name["login"]["denied"] == 1
    assert by_name["orders"]["allowed"] == 3
    assert all(entry["algorithm"] == "fixed_window" for entry in metrics)
    assert all(len(entry["policy_id"]) == 64 for entry in metrics)
    assert "private-client" not in repr(metrics)
    metrics[0]["checks"] = 999
    assert limiter.rule_metrics()[0]["checks"] == 3


@pytest.mark.parametrize("fail_open", [False, True])
def test_rule_metrics_track_storage_failure_policy(fail_open):
    class BrokenStorage(MemoryStorage):
        def execute_algorithm(self, *args):
            raise ConnectionError("unavailable")

    limiter = RateLimiter(
        RateLimiterConfig(fail_open=fail_open), storage=BrokenStorage()
    )
    rule = RateLimitRule(1, 60, name="rule")
    assert limiter.check("client", rule).allowed == fail_open
    [entry] = limiter.rule_metrics()
    assert entry["checks"] == entry["storage_errors"] == 1
    assert entry["allowed"] == int(fail_open)
    assert entry["denied"] == int(not fail_open)


def test_rule_metrics_are_bounded_across_reloads_and_track_recent_activity():
    limiter = RateLimiter(RateLimiterConfig())
    assert limiter.rule_metrics() == []
    rules = [RateLimitRule(100, 60, name=f"rule-{i}") for i in range(101)]
    for rule in rules[:100]:
        limiter.update_rules([rule])
        limiter.check_request(request("client"))
    # Refresh the oldest policy, then force one eviction.
    limiter.check("other", rules[0])
    limiter.check("client", rules[100])
    metrics = {entry["name"]: entry for entry in limiter.rule_metrics()}
    assert len(metrics) == 100
    assert metrics["rule-0"]["checks"] == 2
    assert "rule-1" not in metrics
    assert "rule-100" in metrics
    assert limiter.metrics()["checks"] == 102


def test_rule_metrics_preserve_concurrent_counts():
    limiter = RateLimiter(RateLimiterConfig(rules=[RateLimitRule(1000, 60)]))
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(limiter.check_request, map(str, range(100))))
    [entry] = limiter.rule_metrics()
    assert entry["checks"] == entry["allowed"] == 100
    assert entry["denied"] == entry["storage_errors"] == 0
