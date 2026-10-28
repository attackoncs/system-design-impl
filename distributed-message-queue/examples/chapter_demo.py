"""Exercise chapter additions over independent localhost services and disks."""

import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from lab import Cluster


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=".runtime/chapter-features.json")
    args = parser.parse_args()
    with Cluster() as cluster:
        client = cluster.client
        client.call("/topic", topic="events", partitions=2)
        producer = client.producer("chapter_buffer")
        with producer.buffered("events", batch_size=4, linger_seconds=0.2) as buffer:
            futures = [
                buffer.submit(
                    {
                        "value": f"event-{index}",
                        "key": str(index),
                        "tags": ["checkout" if index % 2 == 0 else "audit"],
                    }
                )
                for index in range(8)
            ]
            receipts = [future.result(15) for future in futures]
        filtered = [
            client.call("/fetch", topic="events", partition=p, tags=["checkout"])
            for p in range(2)
        ]
        assert sum(len(batch["records"]) for batch in filtered) == 4
        assert all(
            set(record["tags"]) == {"checkout"}
            for batch in filtered
            for record in batch["records"]
        )
        assert all(
            batch["next_offset"] == batch["high_water"] + 1 for batch in filtered
        )

        first = client.call("/join", topic="events", group="workers", member="a")
        second = client.call("/join", topic="events", group="workers", member="b")
        client.call(
            "/leave",
            topic="events",
            group="workers",
            member="b",
            session=second["session"],
        )
        remaining = client.call(
            "/heartbeat",
            topic="events",
            group="workers",
            member="a",
            session=first["session"],
        )
        assert remaining["assignments"] == [0, 1]
        client.call("/expand", topic="events", partitions=3)
        expanded = client.call(
            "/heartbeat",
            topic="events",
            group="workers",
            member="a",
            session=first["session"],
        )
        assert expanded["assignments"] == [0, 1, 2]
        new_partition = producer.send(
            "events", [{"value": "new partition"}], partition=2
        )
        assert (
            client.call("/fetch", topic="events", partition=2)["records"][0]["value"]
            == "new partition"
        )

        client.call("/topic", topic="scheduled")
        due = time.time() + 0.3
        producer.send(
            "scheduled",
            [{"value": "due", "deliver_at": due}, {"value": "behind"}],
            partition=0,
        )
        scheduled = client.call(
            "/fetch", topic="scheduled", partition=0, wait_seconds=2
        )
        assert [record["value"] for record in scheduled["records"]] == ["due", "behind"]
        assert time.time() >= due
        report = {
            "scope": "independent localhost HTTP, three separate broker disks, one durable coordinator and three TCP proxies; synthetic text only",
            "buffered_records": len(receipts),
            "buffered_quorum_confirmed": all(
                receipt["committed"] for receipt in receipts
            ),
            "keyed_partitions": sorted(
                set(receipt["partition"] for receipt in receipts)
            ),
            "tag_filtered_records": 4,
            "filter_cursors_reached_high_water": True,
            "assignments_after_leave": remaining["assignments"],
            "assignments_after_expansion": expanded["assignments"],
            "new_partition_offsets": new_partition["offsets"],
            "scheduled_long_poll_records": [
                record["value"] for record in scheduled["records"]
            ],
            "scheduled_not_delivered_early": True,
        }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
