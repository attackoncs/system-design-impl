import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from lab import Lab
from metrics_monitor.common import http_json


def main():
    with Lab() as lab:
        lab.wait(lambda: lab.points("python_memory_bytes"))
        rule = {
            "id": "example_high",
            "metric": "demo_load",
            "op": ">",
            "threshold": 80,
            "for_seconds": 0,
            "destination": "local",
        }
        http_json(lab.urls["api"] + "/rules", "POST", rule, lab.config["admin_token"])
        lab.receiver_control.write_text('{"fail_after_commit":true}')
        started = time.perf_counter()
        lab.push(
            [
                {
                    "metric": "demo_load",
                    "timestamp": time.time(),
                    "value": 95,
                    "labels": {"instance": "demo"},
                }
            ],
            "demo-load",
        )
        lab.wait(lambda: lab.points("demo_load"))
        persisted_ms = (time.perf_counter() - started) * 1000
        lab.wait(
            lambda: (
                http_json(lab.urls["receiver"] + "/stats")["unique_notifications"] == 1
            )
        )
        lab.receiver_control.write_text('{"fail_after_commit":false}')
        lab.wait(
            lambda: any(
                r["status"] == "sent"
                for r in http_json(
                    lab.urls["api"] + "/alerts", token=lab.config["read_token"]
                )["deliveries"]
            )
        )
        receipts = http_json(lab.urls["receiver"] + "/stats")
        print(
            json.dumps(
                {
                    "queue_brokers": 3,
                    "queue_coordinators": 1,
                    "tcp_proxies": 3,
                    "monitor_processes": 6,
                    "pull_metric": "python_memory_bytes",
                    "push_to_query_ms": round(persisted_ms, 3),
                    "unique_webhook_notifications": receipts["unique_notifications"],
                    "webhook_attempts": receipts["attempts"],
                    "dedupe_after_lost_confirmation": receipts["unique_notifications"]
                    == 1
                    and receipts["attempts"] >= 2,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
