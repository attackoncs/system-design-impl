from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"tools"))
from lab import Lab, wait_for


def dates():
    first = datetime.now(timezone.utc).date()+timedelta(days=10)
    return first.isoformat(), (first+timedelta(days=2)).isoformat()


def request(hotel, key, start, end, **kwargs):
    return {"hotel": hotel, "room": "king", "start": start, "end": end, "quantity": 1,
            "request_key": str(key), "expected_total": 20000, "payment_method": "test-ok", **kwargs}


class NetworkTests(unittest.TestCase):
    def test_http_concurrency_two_shards_and_rounding(self):
        with Lab() as lab:
            start, end = dates()
            h0, h1 = lab.hotels()
            for hotel in (h0, h1):
                lab.setup_hotel(hotel, start, end)
            def reserve(i):
                try:
                    return lab.api("/v1/reservations", request(h0, i, start, end))["id"]
                except HTTPError as exc:
                    self.assertEqual(exc.code, 409)
                    return None
            with ThreadPoolExecutor(max_workers=12) as pool:
                accepted = [value for value in pool.map(reserve, range(40)) if value]
            self.assertEqual(len(accepted), 11)  # floor(10 * 1.1), including pending holds.
            lab.api("/v1/reservations", request(h1, "other-shard", start, end))
            wait_for(lab.settled)
            for booking in accepted:
                self.assertEqual(lab.api("/v1/reservations/"+booking)["status"], "CONFIRMED")
            available = lab.api(f"/v1/hotels/{h0}/rooms/king/availability?start={start}&end={end}")
            self.assertEqual([d["reserved"] for d in available["days"]], [11, 11])
            self.assertEqual(available["available"], 0)
            stats = lab.call("payment", "/admin/status", role="admin")
            self.assertEqual(stats["operations"], 12)
            for shard, expected in ((0, 11), (1, 1)):
                self.assertEqual(lab.call(f"shard-{shard}", "/internal/status", role="internal")["bookings"], expected)

    def test_lost_payment_ack_cancel_restart_refund_and_acl(self):
        with Lab() as lab:
            start, end = dates()
            hotel = lab.hotels()[0]
            lab.setup_hotel(hotel, start, end)
            lab.call("payment", "/admin/control", {"fail_after_commit": True, "fail_refunds": False}, "admin")
            body = request(hotel, "lost-ack", start, end)
            booking = lab.api("/v1/reservations", body)
            wait_for(lambda: lab.call("payment", "/admin/status", role="admin")["operations"]==1)
            lab.stop("worker-0")
            cancelled = lab.api("/v1/reservations/"+booking["id"], method="DELETE")
            self.assertEqual(cancelled["status"], "CANCELLED")
            self.assertEqual(lab.api("/v1/reservations", body)["id"], booking["id"])
            with self.assertRaises(HTTPError) as conflict:
                lab.api("/v1/reservations", {**body, "quantity": 2, "expected_total": 40000})
            self.assertEqual(conflict.exception.code, 409)
            with self.assertRaises(HTTPError) as forbidden:
                lab.api("/v1/reservations/"+booking["id"], role="bob")
            self.assertEqual(forbidden.exception.code, 404)
            with self.assertRaises(HTTPError) as noadmin:
                lab.api(f"/v1/hotels/{hotel}", {"name": "x", "address": "x"}, method="PUT")
            self.assertEqual(noadmin.exception.code, 401)
            lab.stop("payment")
            lab.spawn("payment")
            lab.call("payment", "/admin/control", {"fail_after_commit": False, "fail_refunds": True}, "admin")
            lab.spawn("worker-0")
            wait_for(lambda: lab.api("/v1/reservations/"+booking["id"])["payment"]=="REFUND_PENDING")
            self.assertEqual(lab.api(f"/v1/hotels/{hotel}/rooms/king/availability?start={start}&end={end}")["available"], 11)
            lab.stop("shard-0")
            lab.spawn("shard-0")
            lab.call("payment", "/admin/control", {"fail_after_commit": False, "fail_refunds": False}, "admin")
            wait_for(lambda: lab.api("/v1/reservations/"+booking["id"])["payment"]=="REFUNDED")
            lab.api("/v1/reservations/"+booking["id"], method="DELETE")
            wait_for(lab.settled)
            stats = lab.call("payment", "/admin/status", role="admin")
            self.assertEqual(stats["operations"], 2)
            self.assertGreaterEqual(stats["attempts"], 3)
            self.assertEqual(stats["charges"][0]["status"], "refunded")
            kinds = [e["kind"] for e in lab.call("shard-0", "/internal/events", role="internal")["events"]]
            self.assertIn("COMPENSATION_REQUIRED", kinds)
            self.assertIn("REFUNDED", kinds)

    def test_hold_expiry_and_mock_decline_release_inventory(self):
        with Lab(hold_seconds=2) as lab:
            start, end = dates()
            hotel = lab.hotels()[0]
            lab.setup_hotel(hotel, start, end)
            lab.stop("worker-0")
            first = lab.api("/v1/reservations", request(hotel, "expires", start, end))
            wait_for(lambda: lab.call("shard-0", "/internal/status", role="internal")["expired_pending"]==1)
            lab.spawn("worker-0")
            wait_for(lambda: lab.api("/v1/reservations/"+first["id"])["status"]=="EXPIRED")
            wait_for(lab.settled)
            self.assertEqual(lab.call("payment", "/admin/status", role="admin")["operations"], 0)
            second = lab.api("/v1/reservations", request(hotel, "decline", start, end, payment_method="test-decline"))
            wait_for(lambda: lab.api("/v1/reservations/"+second["id"])["status"]=="REJECTED")
            self.assertEqual(lab.api(f"/v1/hotels/{hotel}/rooms/king/availability?start={start}&end={end}")["available"], 11)

    def test_actual_precommit_exit_multinight_failure_and_price_change(self):
        with Lab() as lab:
            start, end = dates()
            hotel = lab.hotels()[0]
            lab.setup_hotel(hotel, start, end)
            payload = request(hotel, "precommit", start, end)
            code = """import json,os,sys
from hotel_reservation.store import Store
s=Store(sys.argv[1],0,2); body=json.loads(sys.argv[2])
s.reserve('alice',body,lambda:os._exit(74))
"""
            result = subprocess.run([sys.executable, "-c", code, str(lab.root/"shard-0.sqlite"), json.dumps(payload)],
                                    env=dict(os.environ, PYTHONPATH=str(ROOT/"src")), capture_output=True, timeout=15,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(result.returncode, 74, result.stderr)
            self.assertEqual(lab.call("shard-0", "/internal/status", role="internal")["bookings"], 0)
            available = lab.api(f"/v1/hotels/{hotel}/rooms/king/availability?start={start}&end={end}")
            self.assertEqual([d["reserved"] for d in available["days"]], [0, 0])
            last = available["days"][-1]["day"]
            lab.api(f"/v1/hotels/{hotel}/rooms/king/calendar", {"days": [{"day": last, "price": 10000, "capacity": 0, "overbook": 10}]}, "admin", "PUT")
            with self.assertRaises(HTTPError):
                lab.api("/v1/reservations", payload)
            available = lab.api(f"/v1/hotels/{hotel}/rooms/king/availability?start={start}&end={end}")
            self.assertEqual([d["reserved"] for d in available["days"]], [0, 0])
            lab.setup_hotel(hotel, start, end, price=12000)
            with self.assertRaises(HTTPError):
                lab.api("/v1/reservations", payload)
            success = lab.api("/v1/reservations", {**payload, "expected_total": 24000})
            wait_for(lab.settled)
            self.assertEqual(lab.api("/v1/reservations/"+success["id"])["amount"], 24000)


if __name__ == "__main__":
    unittest.main()
