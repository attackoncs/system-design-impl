"""Discrete capacity must not be rounded below the validated demand."""

from capacity_estimation import estimate


def test_tiny_excess_over_one_server_still_requires_two():
    # With a=1e-30, demand/capacity=(1-2a)^2*(1+a)/(1-3a)
    # =1+4*a^3/(1-3a), strictly above one despite 80-digit rounding.
    result = estimate(
        {
            "monthly_active_users": 86400,
            "daily_active_ratio": "0." + "9" * 29 + "8",
            "writes_per_dau": "0." + "9" * 29 + "8",
            "reads_per_dau": 0,
            "peak_factor": "1." + "0" * 29 + "1",
            "server_qps": 1,
            "utilization": "0." + "9" * 29 + "7",
            "safety_factor": 1,
        }
    )
    assert result["nodes"] == {"serving": 2, "spare": 0, "total": 2}


def test_large_node_count_and_spare_are_exact_integers():
    result = estimate(
        {
            "monthly_active_users": "1e30",
            "daily_active_ratio": 1,
            "writes_per_dau": "1e30",
            "peak_factor": "1e30",
            "safety_factor": "1e30",
            "server_qps": "1e-30",
            "utilization": "1e-30",
            "spare_nodes": 1,
        }
    )
    quotient, remainder = divmod(10**180, 86400)
    expected = quotient + bool(remainder)
    assert result["nodes"] == {
        "serving": expected,
        "spare": 1,
        "total": expected + 1,
    }
