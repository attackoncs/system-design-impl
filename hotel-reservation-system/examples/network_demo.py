from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from lab import Lab, wait_for
from hotel_reservation.common import dumps


def main():
    with Lab() as lab:
        hotel = lab.hotels()[0]
        first = datetime.now(timezone.utc).date() + timedelta(days=10)
        start, end = first.isoformat(), (first + timedelta(days=2)).isoformat()
        lab.setup_hotel(hotel, start, end)
        body = dict(
            hotel=hotel,
            room="king",
            start=start,
            end=end,
            quantity=1,
            request_key="demo",
            expected_total=20000,
            payment_method="test-ok",
        )
        begin = time.perf_counter()
        booking = lab.api("/v1/reservations", body)
        duplicate = lab.api("/v1/reservations", body)
        paid = wait_for(
            lambda: (
                value
                if (value := lab.api("/v1/reservations/" + booking["id"]))["status"]
                == "CONFIRMED"
                else None
            )
        )
        confirmed_ms = (time.perf_counter() - begin) * 1000
        lab.api("/v1/reservations/" + booking["id"], method="DELETE")
        wait_for(
            lambda: (
                lab.api("/v1/reservations/" + booking["id"])["payment"] == "REFUNDED"
            )
        )
        ledger = lab.call("payment", "/admin/status", role="admin")
        print(
            dumps(
                {
                    "processes": 6,
                    "simulated_payments": True,
                    "same_id_on_retry": booking["id"] == duplicate["id"],
                    "confirmed_amount_cents": paid["amount"],
                    "hold_to_confirmed_ms": round(confirmed_ms, 2),
                    "payment_operations": ledger["operations"],
                    "payment_final_state": ledger["charges"][0]["status"],
                    "available_after_refund": lab.api(
                        f"/v1/hotels/{hotel}/rooms/king/availability?start={start}&end={end}"
                    )["available"],
                }
            )
        )


if __name__ == "__main__":
    main()
