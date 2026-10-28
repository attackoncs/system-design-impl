"""Chapter 21 additions over independent localhost services and real queues."""

import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from lab import Lab, port
from metrics_monitor.common import http_json
from metrics_monitor.discovery import CollectorRing


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=".runtime/chapter-features.json")
    args = parser.parse_args()
    lab = Lab()
    lab.config.update(
        queue_partitions=3,
        partition_by="series",
        collector_id="a",
        collectors=["a", "b"],
    )
    lab.config_path.write_text(json.dumps(lab.config), encoding="utf-8")
    ring = CollectorRing(["a", "b"])
    targets = {}
    for index in range(100):
        instance = "exporter-" + str(index)
        targets.setdefault(ring.owner(instance), []).append(instance)
        if all(len(targets.get(owner, [])) >= 2 for owner in ("a", "b")):
            break
    instances = targets["a"][:2] + targets["b"][:2]
    lab.targetfile.write_text(
        json.dumps(
            [
                {
                    "instance": instance,
                    "url": lab.urls["exporter"] + "/metrics",
                    "token": lab.config["exporter_token"],
                    "interval": 0.5,
                }
                for instance in instances
            ]
        ),
        encoding="utf-8",
    )
    with lab:
        secondary = lab.root / "collector-b.json"
        secondary.write_text(
            json.dumps({**lab.config, "collector_id": "b"}), encoding="utf-8"
        )
        secondary_url = f"http://127.0.0.1:{port()}"
        lab.spawn(
            "collector-b",
            [
                "-m",
                "metrics_monitor.cli",
                "collector",
                "--root",
                str(lab.root / "collector-b"),
                "--config",
                str(secondary),
                "--targets",
                str(lab.targetfile),
                "--interval",
                "0.5",
                "--port",
                secondary_url.rsplit(":", 1)[1],
            ],
        )
        lab.wait(lambda: http_json(secondary_url + "/health"))
        expected_a, expected_b = set(targets["a"][:2]), set(targets["b"][:2])

        def ownership():
            first = http_json(lab.urls["collector"] + "/health")
            second = http_json(secondary_url + "/health")
            seen_a = {
                row["target"]
                for row in first["scrapes"]
                if row["target"] != "_discovery"
            }
            seen_b = {
                row["target"]
                for row in second["scrapes"]
                if row["target"] != "_discovery"
            }
            return seen_a == expected_a and seen_b == expected_b and not seen_a & seen_b

        lab.wait(ownership)
        now = time.time()
        samples = [
            {
                "metric": "chapter_cpu",
                "labels": {"instance": "host" + str(i), "region": "west"},
                "type": "gauge",
                "timestamp": now - offset,
                "value": value,
            }
            for i, offset, value in [(0, 20, 10), (0, 10, 30), (1, 10, 80)]
        ]
        first = lab.push(samples, "chapter-stable")
        assert lab.push(samples, "chapter-stable") == first
        lab.wait(lambda: len(lab.points("chapter_cpu", step=60)) >= 2)
        # Use a single wide aligned bucket, so all three samples share one group.
        start = int(now // 60) * 60 - 60
        result = lab.query(
            "chapter_cpu", start=start, end=now + 1, step=3600, group_by='["region"]'
        )
        points = result["series"][0]["points"]
        average = sum(point["value"] * point["count"] for point in points) / sum(
            point["count"] for point in points
        )
        assert abs(average - 40) < 1e-9
        assert sum(point["count"] for point in points) == 3
        rule = {
            "id": "chapter_static",
            "metric": "chapter_cpu",
            "op": ">",
            "threshold": 0,
            "destination": "local",
            "labels": {"severity": "page"},
        }
        http_json(lab.urls["api"] + "/rules", "POST", rule, lab.config["admin_token"])
        lab.wait(
            lambda: (
                http_json(lab.urls["receiver"] + "/stats")["unique_notifications"] > 0
            )
        )
        receipts = http_json(lab.urls["receiver"] + "/stats")["receipts"]
        assert all(
            event["labels"]["severity"] == "page"
            for receipt in receipts
            for event in receipt["payload"]["events"]
        )
        metadata = http_json(
            lab.queue.url + "/metadata", "POST", {}, lab.queue.config["client_token"]
        )
        heads = [
            part["hwm"] for part in metadata["partitions"] if part["topic"] == "metrics"
        ]
        report = {
            "scope": "14 independent localhost processes: 3 broker disks, coordinator, 3 TCP proxies, 2 collectors, exporter, query API, storage, alerts, owned webhook",
            "static_collectors": 2,
            "disjoint_scrape_targets": len(instances),
            "all_targets_owned_once": True,
            "queue_partitions": len(heads),
            "active_queue_partitions": sum(head >= 0 for head in heads),
            "weighted_group_average": average,
            "group_sample_count": 3,
            "push_replay_identical": True,
            "notification_severity": "page",
            "external_provider": "owned localhost webhook only",
        }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
