from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from lab import Lab, wait_for
from ad_click.common import dumps
from ad_click.service import reconcile


def main():
    with Lab() as lab:
        end = int(time.time()) // 60 * 60
        events = [{"event_id": f"demo-{i}", "ad_id": "hot" if i < 80 else "other",
                   "click_timestamp": end - 20, "user_id": "user-" + str(i % 5),
                   "ip": "127.0.0.1", "country": "US" if i < 80 else "GB"} for i in range(100)]
        start = time.perf_counter()
        lab.send(events)
        duplicated = lab.send(events)
        wait_for(lab.caught_up)
        result = lab.query({"from": end - 60, "to": end, "count": 2})
        elapsed = (time.perf_counter() - start) * 1000
        print(dumps({"processes": 6, "accepted_unique": 100,
                     "duplicate_receipts": sum(r["duplicate"] for r in duplicated["receipts"]),
                     "top_ads": result["ads"], "push_duplicate_to_query_ms": round(elapsed, 2),
                     "all_partition_lag": result["lag"], "reconciliation_matches": reconcile(lab.config)["matches"]}))


if __name__ == "__main__":
    main()
