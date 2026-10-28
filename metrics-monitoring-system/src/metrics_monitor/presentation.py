"""Process exporter and bounded SVG rendering, independent of HTTP routing."""

from html import escape
import threading
import time
import tracemalloc

from .common import Error, dumps, identifier, number

_TRACE_LOCK = threading.Lock()
_TRACE_USERS = 0
_TRACE_OWNED = False


class Exporter:
    """Process CPU and Python traced allocation, not host CPU utilization/RSS."""

    def __init__(self, instance, *, clock=time.time):
        global _TRACE_USERS, _TRACE_OWNED
        self.instance = identifier(instance)
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.clock, self.requests, self.lock = clock, 0, threading.Lock()
        self._closed = False
        with _TRACE_LOCK:
            if not _TRACE_USERS:
                _TRACE_OWNED = not tracemalloc.is_tracing()
                if _TRACE_OWNED:
                    tracemalloc.start()
            _TRACE_USERS += 1

    def close(self):
        global _TRACE_USERS, _TRACE_OWNED
        with self.lock:
            if self._closed:
                return
            self._closed = True
            with _TRACE_LOCK:
                _TRACE_USERS -= 1
                if _TRACE_USERS == 0 and _TRACE_OWNED:
                    tracemalloc.stop()
                    _TRACE_OWNED = False

    def samples(self):
        with self.lock:
            if self._closed:
                raise RuntimeError("exporter is closed")
            self.requests += 1
            readings = (
                ("process_cpu_seconds_total", "counter", time.process_time()),
                ("python_memory_bytes", "gauge", tracemalloc.get_traced_memory()[0]),
                ("http_requests_total", "counter", self.requests),
            )
            now = number(self.clock())
        return {
            "samples": [
                {
                    "metric": metric,
                    "labels": {"instance": self.instance},
                    "type": kind,
                    "timestamp": now,
                    "value": value,
                }
                for metric, kind, value in readings
            ]
        }


def chart(result):
    """Show missing buckets as gaps instead of inventing a connecting trend."""
    series = result["series"]
    if not isinstance(series, list) or len(series) > 200:
        raise Error(413, "chart series budget exceeded")
    if sum(len(item["points"]) for item in series) > 20000:
        raise Error(413, "chart point budget exceeded")
    step = number(result["step"])
    if step <= 0:
        raise ValueError("positive chart step required")
    values, stamps, segments = [], [], []
    for item in series:
        current, groups, previous = [], [], None
        for point in item["points"]:
            timestamp = number(point["timestamp"])
            value = None if point["value"] is None else number(point["value"])
            stamps.append(timestamp)
            if value is None or (
                previous is not None and timestamp - previous > step * 1.5
            ):
                if current:
                    groups.append(current)
                    current = []
            if value is not None:
                values.append(value)
                current.append((timestamp, value))
            previous = timestamp
        if current:
            groups.append(current)
        segments.append(groups)
    low, high = (min(values), max(values)) if values else (0, 1)
    first, last = (min(stamps), max(stamps)) if stamps else (0, 1)
    colors = ("#1463a5", "#c24c26", "#237b45", "#8544a1")
    lines = []
    for index, (item, groups) in enumerate(zip(series, segments)):
        label = escape(item["metric"] + " " + dumps(item["labels"]))
        color = colors[index % len(colors)]
        for group in groups:
            coordinates = [
                (
                    50 + 700 * (stamp - first) / max(1, last - first),
                    240 - 190 * (value - low) / max(1e-12, high - low),
                )
                for stamp, value in group
            ]
            points = " ".join(f"{x:.2f},{y:.2f}" for x, y in coordinates)
            lines.append(
                f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{points}"><title>{label}</title></polyline>'
            )
            if len(coordinates) == 1:
                x, y = coordinates[0]
                lines.append(
                    f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2.5" fill="{color}"><title>{label}</title></circle>'
                )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="280" viewBox="0 0 800 280">'
        '<rect width="100%" height="100%" fill="#f8fafc"/>'
        '<path d="M50 35V240H770" fill="none" stroke="#64748b"/>'
        f'<text x="50" y="20" font-family="sans-serif" font-size="13">Metrics — revision {escape(str(result["revision"]))}</text>'
        + "".join(lines)
        + f'<text x="50" y="265" font-family="sans-serif" font-size="12">{low:.4g} … {high:.4g}; step {step:g}s</text></svg>'
    ).encode()
