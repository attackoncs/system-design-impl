"""Pure capacity calculations over validated, explicit-unit scenarios."""

from decimal import Decimal, ROUND_CEILING

from .quantities import arithmetic_context, number, parse_bytes as parse_bytes, text
from .scenario import DEFAULTS as DEFAULTS, parse_scenario


def availability(percent):
    with arithmetic_context():
        value = number(percent, 0, 100)
        periods = {
            "day": 86400,
            "week": 7 * 86400,
            "month_30_days": 30 * 86400,
            "year_365_days": 365 * 86400,
        }
        return {
            "availability_percent": text(value),
            "downtime_seconds": {
                name: text(Decimal(seconds) * (1 - value / 100))
                for name, seconds in periods.items()
            },
            "assumption": "30-day month; 365-day year; no SLA exclusions",
        }


def estimate(inputs):
    with arithmetic_context():
        values, n, sizes = parse_scenario(inputs)
        dau = n["monthly_active_users"] * n["daily_active_ratio"]
        writes = dau * n["writes_per_dau"]
        reads = dau * n["reads_per_dau"]
        average_record = (
            sizes["metadata_size"] + n["media_fraction"] * sizes["media_size"]
        )
        daily_bytes = writes * average_record
        retained_bytes = daily_bytes * n["retention_days"]
        compressed = retained_bytes * n["compression_ratio"]
        physical = compressed * n["replicas"] * (1 + n["index_overhead_ratio"])
        peak_qps = (writes + reads) / 86400 * n["peak_factor"]
        baseline_nodes = (
            peak_qps * n["safety_factor"] / (n["server_qps"] * n["utilization"])
        ).to_integral_value(rounding=ROUND_CEILING)
        ingress = daily_bytes * 8 / 86400
        egress = reads * sizes["read_response_size"] * 8 / 86400
        quantities = {
            "daily_active_users": dau,
            "writes_per_day": writes,
            "reads_per_day": reads,
            "average_write_qps": writes / 86400,
            "average_read_qps": reads / 86400,
            "peak_write_qps": writes / 86400 * n["peak_factor"],
            "peak_read_qps": reads / 86400 * n["peak_factor"],
            "peak_total_qps": peak_qps,
            "average_record_bytes": average_record,
            "logical_bytes_per_day": daily_bytes,
            "retained_logical_bytes": retained_bytes,
            "compressed_primary_bytes": compressed,
            "physical_bytes_with_replicas_and_indexes": physical,
            "cache_bytes": writes
            * n["retention_days"]
            * n["cache_fraction"]
            * sizes["cache_item_size"],
            "average_ingress_bits_per_second": ingress,
            "peak_ingress_bits_per_second": ingress * n["peak_factor"],
            "average_egress_bits_per_second": egress,
            "peak_egress_bits_per_second": egress * n["peak_factor"],
            "estimated_peak_inflight_at_mean_latency": peak_qps
            * n["mean_latency_ms"]
            / 1000,
        }
        return {
            "assumptions": {
                key: text(value)
                if isinstance(value, (Decimal, int)) and not isinstance(value, bool)
                else value
                for key, value in values.items()
            },
            "estimates": {key: text(value) for key, value in quantities.items()},
            "nodes": {
                "serving": int(baseline_nodes),
                "spare": int(n["spare_nodes"]),
                "total": int(baseline_nodes + n["spare_nodes"]),
            },
            "availability": availability(n["availability_percent"]),
            "notes": [
                "Estimates, not measurements or a capacity/SLA guarantee.",
                "MB=10^6 bytes; MiB=2^20 bytes. Rates use bits/s, 8 bits per byte.",
                "Storage compression applies before replica and index multipliers; index overhead is on each compressed replica.",
                "Cache holds a fraction of retained write records at explicit cache-item size; no cache replication multiplier.",
                "Network uses uncompressed average write/read payloads, excluding protocol, replication and cache traffic.",
                "Node count assumes a supplied mixed-workload QPS benchmark; spare nodes are explicit, not an availability proof.",
                "Inflight estimate applies Little's law to assumed peak arrival rate and mean latency, not p99.",
            ],
        }


def sweep(inputs, field, values):
    if not isinstance(inputs, dict):
        raise ValueError("scenario must be an object")
    if not isinstance(values, (list, tuple)):
        raise ValueError("sweep values must be a list or tuple")
    if (
        field
        not in (
            "monthly_active_users",
            "peak_factor",
            "media_fraction",
            "replicas",
            "utilization",
            "safety_factor",
        )
        or not 1 <= len(values) <= 20
    ):
        raise ValueError("unsupported or oversized sensitivity sweep")
    return [
        {
            "field": field,
            "value": str(value),
            "result": estimate({**inputs, field: value}),
        }
        for value in values
    ]
