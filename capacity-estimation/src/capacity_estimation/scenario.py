"""Scenario defaults and boundary validation, separate from capacity formulas."""

from .quantities import number, parse_bytes


DEFAULTS = {
    "monthly_active_users": 300_000_000,
    "daily_active_ratio": "0.5",
    "writes_per_dau": 2,
    "reads_per_dau": 0,
    "peak_factor": 2,
    "metadata_size": "0 B",
    "media_fraction": "0.1",
    "media_size": "1 MB",
    "read_response_size": "0 B",
    "retention_days": 1825,
    "replicas": 1,
    "compression_ratio": 1,
    "index_overhead_ratio": 0,
    "cache_fraction": 0,
    "cache_item_size": "0 B",
    "server_qps": 1000,
    "utilization": "0.7",
    "safety_factor": 1,
    "spare_nodes": 0,
    "mean_latency_ms": 100,
    "availability_percent": "99.99",
}


def parse_scenario(inputs):
    if not isinstance(inputs, dict) or set(inputs) - set(DEFAULTS):
        raise ValueError("unknown scenario fields")
    values = {**DEFAULTS, **inputs}
    n = {
        key: number(value) for key, value in values.items() if not key.endswith("size")
    }
    for key in ("daily_active_ratio", "media_fraction", "cache_fraction"):
        number(n[key], 0, 1)
    for key in ("compression_ratio", "utilization"):
        number(n[key], 0, 1, low_open=True)
    for key in ("peak_factor", "safety_factor"):
        number(n[key], 1)
    number(n["server_qps"], 0, low_open=True)
    number(n["availability_percent"], 0, 100)
    for key in (
        "monthly_active_users",
        "retention_days",
        "replicas",
        "spare_nodes",
    ):
        if n[key] != n[key].to_integral_value() or (key == "replicas" and n[key] < 1):
            raise ValueError(key + " must be a whole count, replicas >= 1")
    sizes = {
        key: parse_bytes(value) for key, value in values.items() if key.endswith("size")
    }
    return values, n, sizes
