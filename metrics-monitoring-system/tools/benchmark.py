import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import platform
import secrets
import socket
import statistics
import subprocess
import sys
import time
from urllib.parse import urlencode
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from metrics_monitor.common import http_json
from metrics_monitor.storage import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--series", type=int, default=100)
    parser.add_argument("--points", type=int, default=60)
    parser.add_argument("--requests", type=int, default=40)
    parser.add_argument("--clients", type=int, default=4)
    parser.add_argument("--output", default=".runtime/query.json")
    args = parser.parse_args()
    if (
        not 1 <= args.series <= 200
        or not 2 <= args.points <= 200
        or not 4 <= args.requests <= 1000
        or not 1 <= args.clients <= 16
    ):
        parser.error("series 1..200, points 2..200, requests 4..1000, clients 1..16")
    root = ROOT / ".runtime" / ("query-" + uuid.uuid4().hex)
    root.mkdir(parents=True)
    store = Store(root / "timeseries.sqlite")
    now = time.time()
    samples = [
        {
            "metric": "synthetic_load",
            "type": "gauge",
            "labels": {"instance": f"host_{i}", "zone": str(i % 3)},
            "timestamp": now - (args.points - j) * 30,
            "value": 50 + 10 * math.sin(i + j),
        }
        for i in range(args.series)
        for j in range(args.points)
    ]
    started = time.perf_counter()
    for start in range(0, len(samples), 100):
        store.ingest("seed_" + str(start), samples[start : start + 100])
    ingest_seconds = time.perf_counter() - started
    config = {"read_token": secrets.token_hex(24), "admin_token": secrets.token_hex(24)}
    config_path = root / "secrets.json"
    config_path.write_text(json.dumps(config))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with (root / "api.log").open("wb") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "metrics_monitor.cli",
                "api",
                "--root",
                str(root),
                "--config",
                str(config_path),
                "--port",
                str(port),
            ],
            env=dict(os.environ, PYTHONPATH=str(ROOT / "src")),
            stdout=log,
            stderr=log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            base = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 10
            while True:
                try:
                    http_json(base + "/health")
                    break
                except OSError:
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError("query process startup failed")
                    time.sleep(0.05)
            path = "/query?" + urlencode(
                {
                    "metric": "synthetic_load",
                    "start": now - args.points * 30,
                    "end": now,
                    "step": 60,
                }
            )
            started = time.perf_counter()
            cold = http_json(base + path, token=config["read_token"])
            cold_ms = (time.perf_counter() - started) * 1000

            def query(_):
                start = time.perf_counter()
                result = http_json(base + path, token=config["read_token"])
                assert len(result["series"]) == args.series
                return (time.perf_counter() - start) * 1000

            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=args.clients) as executor:
                timings = list(executor.map(query, range(args.requests)))
            seconds = time.perf_counter() - started
            report = {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "series": args.series,
                "raw_samples": len(samples),
                "ingest_seconds": round(ingest_seconds, 3),
                "query_requests": args.requests,
                "clients": args.clients,
                "query_points": sum(len(s["points"]) for s in cold["series"]),
                "cold_query_ms": round(cold_ms, 3),
                "warm_p50_ms": round(statistics.median(timings), 3),
                "warm_p95_ms": round(
                    sorted(timings)[int(0.95 * (len(timings) - 1))], 3
                ),
                "warm_query_qps": round(args.requests / seconds, 2),
                "sqlite_bytes": sum(
                    p.stat().st_size for p in root.glob("timeseries.sqlite*")
                ),
                "errors": 0,
                "scope": "direct fixture ingestion; independent localhost HTTP query process; repeated fixed range warms revision cache; not queue ingestion throughput or production TSDB scale",
            }
            target = Path(args.output)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps(report, indent=2))
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    main()
