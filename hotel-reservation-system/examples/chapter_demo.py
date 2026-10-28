"""Real HTTP rolling calendar and room-type discovery; simulated payments only."""

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from lab import Lab, wait_for


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=".runtime/chapter-features.json")
    args = parser.parse_args()
    with Lab() as lab:
        hotel = lab.hotels()[0]
        today = datetime.now(timezone.utc).date()
        start, end = (
            (today + timedelta(days=10)).isoformat(),
            (today + timedelta(days=12)).isoformat(),
        )
        lab.api(
            f"/v1/hotels/{hotel}",
            {"name": "Calendar Hotel", "address": "Local reference"},
            "admin",
            "PUT",
        )
        for room in ("king", "queen"):
            lab.api(
                f"/v1/hotels/{hotel}/rooms/{room}",
                {"name": room, "description": room + " room type"},
                "admin",
                "PUT",
            )
        # An operator's specific date survives rolling default fill.
        lab.api(
            f"/v1/hotels/{hotel}/rooms/king/calendar",
            {"days": [{"day": start, "capacity": 10, "overbook": 10, "price": 12000}]},
            "admin",
            "PUT",
        )
        policy_path = f"/v1/hotels/{hotel}/rooms/king/calendar-policy"
        policy = {"capacity": 10, "overbook": 10, "price": 10000}
        first = lab.api(policy_path, policy, "admin", "PUT")
        assert (
            lab.api(policy_path, policy, "admin", "PUT")["version"] == first["version"]
        )
        lab.api(
            f"/v1/hotels/{hotel}/rooms/queen/calendar-policy",
            {"capacity": 1, "overbook": 0, "price": 8000},
            "admin",
            "PUT",
        )
        denied = False
        try:
            lab.api(policy_path)
        except HTTPError as exc:
            denied = exc.code in (401, 403)
            exc.close()
        assert denied
        # Do not call internal/calendar-roll: this verifies the actual CLI worker.
        target_day = (today + timedelta(days=730)).isoformat()
        wait_for(lambda: lab.api(policy_path, role="admin")["next_day"] >= target_day)
        wait_for(
            lambda: (
                lab.api(
                    f"/v1/hotels/{hotel}/rooms/queen/calendar-policy", role="admin"
                )["next_day"]
                >= target_day
            )
        )
        path = f"/v1/hotels/{hotel}/availability?start={start}&end={end}"
        all_rooms = lab.api(path)
        assert [room["id"] for room in all_rooms["room_types"]] == ["king", "queen"]
        rooms = lab.api(path + "&quantity=2")["room_types"]
        assert [room["id"] for room in rooms] == ["king"]
        assert rooms[0]["unit_total"] == 22000
        request = {
            "hotel": hotel,
            "room": "king",
            "start": start,
            "end": end,
            "quantity": 2,
            "request_key": "chapter-book",
            "expected_total": 44000,
            "payment_method": "test-ok",
        }
        booking = lab.api("/v1/reservations", request)
        assert lab.api("/v1/reservations", request)["id"] == booking["id"]
        wait_for(
            lambda: (
                lab.api("/v1/reservations/" + booking["id"])["status"] == "CONFIRMED"
            )
        )
        lab.api(policy_path, {**policy, "price": 99999}, "admin", "PUT")
        # A changed default is still not a repricing operation.
        shard = "shard-0"
        lab.call(shard, "/internal/calendar-roll", {}, "internal")
        current = lab.api("/v1/reservations/" + booking["id"])
        assert current["amount"] == 44000
        assert [night["price"] for night in current["nights"]] == [12000, 10000]
        lab.api("/v1/reservations/" + booking["id"], method="DELETE")
        wait_for(
            lambda: (
                lab.api("/v1/reservations/" + booking["id"])["payment"] == "REFUNDED"
            )
        )
        available = lab.api(path)["room_types"][0]["available"]
        assert available == 11
        lab.api(policy_path, role="admin", method="DELETE")
        assert lab.api(path)["room_types"][0]["unit_total"] == 22000
        report = {
            "scope": "six independent localhost gateway/shard/worker/simulated-payment processes; no real funds",
            "background_calendar_days_per_room": 730,
            "auto_filled_room_types": 2,
            "hotel_query_room_types": [room["id"] for room in all_rooms["room_types"]],
            "two_room_query_types": [room["id"] for room in rooms],
            "policy_admin_only": denied,
            "identical_policy_preserves_version": True,
            "manual_night_price_preserved": 12000,
            "booking_total_cents": current["amount"],
            "booking_retry_same_id": True,
            "final_payment": "REFUNDED",
            "available_after_refund": available,
            "disabled_policy_keeps_inventory": True,
            "simulated_payments": True,
        }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
