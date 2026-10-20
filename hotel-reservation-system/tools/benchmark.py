import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import platform
import statistics
import time
from urllib.error import HTTPError

from lab import Lab, wait_for


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="benchmarks/results.json")
    args = parser.parse_args()
    with Lab() as lab:
        first = datetime.now(timezone.utc).date()+timedelta(days=10)
        start, end = first.isoformat(), (first+timedelta(days=2)).isoformat()
        hotel = lab.hotels()[0]
        lab.setup_hotel(hotel, start, end, capacity=20)
        def reserve(i):
            begin = time.perf_counter()
            try:
                value = lab.api("/v1/reservations", dict(hotel=hotel, room="king", start=start, end=end,
                                quantity=1, request_key=f"bench-{i}", expected_total=20000, payment_method="test-ok"))
                return {"id": value["id"], "ms": (time.perf_counter()-begin)*1000, "accepted": True}
            except HTTPError as exc:
                if exc.code!=409:
                    raise
                return {"ms": (time.perf_counter()-begin)*1000, "accepted": False}
        begin = time.perf_counter()
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(reserve, range(100)))
        admission_seconds = time.perf_counter()-begin
        wait_for(lab.settled)
        settled_seconds = time.perf_counter()-begin
        accepted = [r for r in results if r["accepted"]]
        availability = lab.api(f"/v1/hotels/{hotel}/rooms/king/availability?start={start}&end={end}")
        ledger = lab.call("payment", "/admin/status", role="admin")
        if len(accepted)!=22 or any(r["reserved"]!=22 for r in availability["days"]) or ledger["operations"]!=22:
            raise AssertionError("inventory or simulated payment invariant failed")
        for row in accepted:
            if lab.api("/v1/reservations/"+row["id"])["status"]!="CONFIRMED":
                raise AssertionError("accepted order failed to settle")
        durations = sorted(r["ms"] for r in results)
        result = {"python": platform.python_version(), "platform": platform.platform(),
                  "processes": 6, "hotel_shards": 2, "hot_hotel_requests": 100, "clients": 16,
                  "physical_rooms_per_night": 20, "overbook_percent": 10, "night_count": 2,
                  "accepted_unique": len(accepted), "capacity_conflicts": 100-len(accepted),
                  "admission_seconds": round(admission_seconds, 3), "all_settled_seconds": round(settled_seconds, 3),
                  "attempt_requests_per_second": round(100/admission_seconds, 2),
                  "successful_bookings_per_second": round(len(accepted)/settled_seconds, 2),
                  "request_p50_ms": round(statistics.median(durations), 3), "request_p95_ms": round(durations[94], 3),
                  "reserved_per_night": [r["reserved"] for r in availability["days"]],
                  "simulated_unique_charges": ledger["operations"], "simulated_payments": True,
                  "sqlite_bytes": sum(p.stat().st_size for p in lab.root.glob("*.sqlite*")),
                  "scope": "localhost HTTP gateway + two SQLite shards + two saga workers + simulated payment ledger; contention on one hot hotel; no real funds or HA"}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2)+"\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
