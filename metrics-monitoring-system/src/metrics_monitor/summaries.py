"""Composable gauge and counter sufficient statistics."""


def singleton(timestamp, value):
    return {
        "count": 1,
        "sum": value,
        "min": value,
        "max": value,
        "first_t": timestamp,
        "first": value,
        "last_t": timestamp,
        "last": value,
        "increase": 0.0,
    }


def combine(values, kind):
    ordered = sorted(values, key=lambda value: value["first_t"])
    if not ordered:
        return None
    result = dict(ordered[0])
    for item in ordered[1:]:
        boundary = (
            item["first"] - result["last"]
            if item["first"] >= result["last"]
            else item["first"]
        )
        result["increase"] += item["increase"] + (boundary if kind == "counter" else 0)
        result["count"] += item["count"]
        result["sum"] += item["sum"]
        result["min"] = min(result["min"], item["min"])
        result["max"] = max(result["max"], item["max"])
        result["last_t"], result["last"] = item["last_t"], item["last"]
    return result


def reading(stats, aggregation, kind):
    if not stats:
        return None
    if aggregation == "last":
        return stats["last"]
    if kind == "gauge" and aggregation in ("avg", "min", "max"):
        return (
            stats["sum"] / stats["count"]
            if aggregation == "avg"
            else stats[aggregation]
        )
    if kind == "counter" and aggregation in ("increase", "rate"):
        return (
            stats["increase"]
            if aggregation == "increase"
            else stats["increase"] / (stats["last_t"] - stats["first_t"])
            if stats["last_t"] > stats["first_t"]
            else None
        )
    raise ValueError("aggregation incompatible with metric type")
