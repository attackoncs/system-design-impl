from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
from pathlib import Path
import threading
import time
import tracemalloc
from urllib.parse import parse_qs, urlsplit

from .common import Error, dumps


class Exporter:
    def __init__(self, instance):
        self.instance, self.requests, self.lock = instance, 0, threading.Lock()
        tracemalloc.start()

    def samples(self):
        with self.lock:
            self.requests += 1
            requests = self.requests
        now = time.time()
        return {
            "samples": [
                {
                    "metric": metric,
                    "labels": {"instance": self.instance},
                    "type": kind,
                    "timestamp": now,
                    "value": value,
                }
                for metric, kind, value in [
                    ("process_cpu_seconds_total", "counter", time.process_time()),
                    (
                        "python_memory_bytes",
                        "gauge",
                        tracemalloc.get_traced_memory()[0],
                    ),
                    ("http_requests_total", "counter", requests),
                ]
            ]
        }


def chart(result):
    width, height = 800, 280
    values = [
        point["value"]
        for series in result["series"]
        for point in series["points"]
        if point["value"] is not None
    ]
    stamps = [
        point["timestamp"] for series in result["series"] for point in series["points"]
    ]
    low, high = (min(values), max(values)) if values else (0, 1)
    first, last = (min(stamps), max(stamps)) if stamps else (0, 1)
    colors = ["#1463a5", "#c24c26", "#237b45", "#8544a1"]
    lines = []
    for index, series in enumerate(result["series"]):
        points = " ".join(
            f"{50 + 700 * (point['timestamp'] - first) / max(1, last - first):.2f},{240 - 190 * (point['value'] - low) / max(1e-12, high - low):.2f}"
            for point in series["points"]
            if point["value"] is not None
        )
        label = escape(series["metric"] + " " + dumps(series["labels"]))
        lines.append(
            f'<polyline fill="none" stroke="{colors[index % 4]}" stroke-width="2" points="{points}"><title>{label}</title></polyline>'
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}"><rect width="100%" height="100%" fill="#f8fafc"/><path d="M50 35V240H770" fill="none" stroke="#64748b"/><text x="50" y="20" font-family="sans-serif" font-size="13">Metrics — revision {result["revision"]}</text>'
        + "".join(lines)
        + f'<text x="50" y="265" font-family="sans-serif" font-size="12">{low:.4g} … {high:.4g}; step {result["step"]}s</text></svg>'
    ).encode()


def server(host, port, config, store=None, alerts=None, collector=None, exporter=None):
    class Handler(BaseHTTPRequestHandler):
        # Apply before parsing headers, including clients that send no request.
        timeout = 10.0

        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def auth(self, role):
            actual = self.headers.get("Authorization", "").encode()
            for key in (
                [role + "_token", "admin_token"] if role != "admin" else ["admin_token"]
            ):
                token = config.get(key)
                if token and hmac.compare_digest(actual, ("Bearer " + token).encode()):
                    return
            raise Error(401, "credential required for role")

        def body(self):
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("Transfer-Encoding unsupported")
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1024 * 1024:
                raise Error(413, "body must be 1 byte..1 MiB")
            value = json.loads(self.rfile.read(size))
            if not isinstance(value, dict):
                raise ValueError("JSON object required")
            return value

        def dispatch(self):
            parsed = urlsplit(self.path)
            params = parse_qs(parsed.query, keep_blank_values=True)
            if any(len(values) != 1 for values in params.values()):
                raise ValueError("duplicate query field")
            params = {key: values[0] for key, values in params.items()}
            path = parsed.path
            if self.command == "GET" and path == "/health":
                return (
                    {"role": "exporter"}
                    if exporter
                    else {"role": "collector", **collector.health()}
                    if collector
                    else {"role": "query", **store.health()}
                )
            if exporter and path == "/metrics" and self.command == "GET":
                self.auth("exporter")
                return exporter.samples()
            if collector and path == "/push" and self.command == "POST":
                self.auth("push")
                return collector.enqueue(
                    self.body()["samples"], self.headers.get("Idempotency-Key")
                )
            if store and self.command == "GET" and path == "/dashboard":
                return Path(__file__).with_name(
                    "dashboard.html"
                ).read_bytes(), "text/html; charset=utf-8"
            if store and self.command == "GET" and path in ("/query", "/chart.svg"):
                self.auth("read")
                if set(params) - {
                    "metric",
                    "start",
                    "end",
                    "match",
                    "step",
                    "aggregation",
                }:
                    raise ValueError("unknown query parameter")
                result = store.query(
                    params["metric"],
                    float(params["start"]),
                    float(params["end"]),
                    json.loads(params.get("match", "{}")),
                    int(params["step"]) if "step" in params else None,
                    params.get("aggregation", "avg"),
                )
                return (
                    (chart(result), "image/svg+xml") if path == "/chart.svg" else result
                )
            if alerts and self.command == "GET" and path == "/alerts":
                self.auth("read")
                return alerts.status()
            if (
                alerts
                and self.command == "POST"
                and path in ("/rules", "/silences", "/evaluate")
            ):
                self.auth("admin")
                body = self.body()
                return (
                    alerts.put_rule(body)
                    if path == "/rules"
                    else alerts.silence(**body)
                    if path == "/silences"
                    else alerts.evaluate()
                )
            if store and path == "/compact" and self.command == "POST":
                self.auth("admin")
                store.compact()
                return store.health()
            raise Error(404, "route not found")

        def handle_request(self):
            self.connection.settimeout(10)
            try:
                result = self.dispatch()
                payload, kind = (
                    result
                    if isinstance(result, tuple)
                    else (dumps(result).encode(), "application/json")
                )
                status = 200
            except Error as exc:
                status, payload, kind = (
                    exc.status,
                    dumps({"error": exc.message}).encode(),
                    "application/json",
                )
            except (ValueError, TypeError, KeyError, OverflowError):
                status, payload, kind = (
                    400,
                    b'{"error":"invalid request"}',
                    "application/json",
                )
            except Exception:
                status, payload, kind = (
                    500,
                    b'{"error":"internal error"}',
                    "application/json",
                )
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(payload)
            except OSError:
                pass

        do_GET = do_POST = handle_request

    result = ThreadingHTTPServer((host, port), Handler)
    result.daemon_threads = True
    return result
