"""Actual SQLite calendar policies, bounded rolling and consistent room queries."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from hotel_reservation.calendar_policy import CalendarPolicies
from hotel_reservation.catalog_queries import available_rooms
from hotel_reservation.common import Error
from hotel_reservation.store import Store


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp()
        self.store = Store(self.root / "hotel.sqlite", clock=lambda: self.now)
        self.store.manage("h", {"name": "Hotel", "address": "Address"})
        self.room("king")
        self.policies = CalendarPolicies(self.store)

    def room(self, room):
        self.store.manage("h", {"name": room, "description": "Room type"}, room)

    def policy(self, room="king", **changes):
        return self.policies.put(
            "h",
            room,
            {
                "capacity": 10,
                "overbook": 10,
                "price": 100,
                "horizon_days": 10,
                **changes,
            },
        )

    def rows(self, room="king"):
        with self.store.transaction() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM inventory WHERE hotel='h' AND room=? ORDER BY day",
                    (room,),
                )
            ]

    def test_default_two_year_horizon_and_bounded_restart_progress(self):
        self.policies.put("h", "king", {"capacity": 10, "overbook": 10, "price": 100})
        first = self.policies.roll_once(max_rows=2)
        self.assertEqual(first["inserted_days"], 2)
        self.assertTrue(first["pending"])
        reopened = CalendarPolicies(Store(self.store.path, clock=lambda: self.now))
        second = reopened.roll_once()
        self.assertEqual(second["inserted_days"], 728)
        self.assertFalse(second["pending"])
        rows = self.rows()
        self.assertEqual(len(rows), 730)
        self.assertEqual(
            rows[-1]["day"],
            (
                datetime.fromtimestamp(self.now, timezone.utc).date()
                + timedelta(days=729)
            ).isoformat(),
        )
        revision = self.store.revision()
        self.assertEqual(reopened.roll_once()["inserted_days"], 0)
        self.assertEqual(self.store.revision(), revision)

    def test_existing_manual_prices_and_holds_are_preserved(self):
        self.store.set_days(
            "h",
            "king",
            [
                {"day": day, "capacity": 9, "overbook": 0, "price": 200}
                for day in ("2030-01-02", "2030-01-03")
            ],
        )
        booking = self.store.reserve(
            "alice",
            {
                "hotel": "h",
                "room": "king",
                "start": "2030-01-02",
                "end": "2030-01-04",
                "quantity": 1,
                "request_key": "held",
                "expected_total": 400,
                "payment_method": "test-ok",
            },
        )
        self.policy()
        result = self.policies.roll_once()
        self.assertEqual(result["inserted_days"], 8)
        for row in self.rows()[1:3]:
            self.assertEqual(
                (row["capacity"], row["overbook"], row["price"], row["reserved"]),
                (9, 0, 200, 1),
            )
        self.assertEqual(self.store.booking(booking["id"])["amount"], 400)

    def test_daily_roll_adds_only_new_horizon_day(self):
        self.policy()
        self.policies.roll_once()
        self.now += 86400
        self.assertEqual(self.policies.roll_once()["inserted_days"], 1)
        self.assertEqual(self.rows()[-1]["day"], "2030-01-11")
        self.assertEqual(
            len(self.rows()), 11
        )  # Old dates remain available for history.

    def test_policy_changes_only_default_for_missing_dates(self):
        self.policy(horizon_days=2)
        self.policies.roll_once()
        self.policy(price=300, horizon_days=3)
        self.policies.roll_once()
        self.assertEqual([row["price"] for row in self.rows()], [100, 100, 300])
        self.assertEqual(self.policies.get("h", "king")["version"], 2)

    def test_identical_policy_put_preserves_pending_progress(self):
        self.policy()
        self.policies.roll_once(max_rows=2)
        before = self.policies.get("h", "king")
        self.assertEqual(self.policy(), before)
        self.assertEqual(self.policies.roll_once()["inserted_days"], 8)

    def test_deletion_keeps_inventory_and_is_idempotent(self):
        self.policy()
        self.policies.roll_once()
        self.policies.delete("h", "king")
        self.policies.delete("h", "king")
        self.now += 86400
        self.assertEqual(self.policies.roll_once()["inserted_days"], 0)
        self.assertEqual(len(self.rows()), 10)
        with self.assertRaises(Error):
            self.policies.get("h", "king")

    def test_soft_deleted_room_is_not_filled_until_reactivated(self):
        self.policy()
        self.store.manage("h", room="king", delete=True)
        self.assertEqual(self.policies.roll_once()["inserted_days"], 0)
        self.room("king")
        self.assertEqual(self.policies.roll_once()["inserted_days"], 10)

    def test_parallel_rollers_publish_one_cursor_and_generation(self):
        self.policy()
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: self.policies.roll_once(), range(12)))
        self.assertEqual(sum(result["inserted_days"] for result in results), 10)
        self.assertEqual(len(self.rows()), 10)
        self.assertEqual(self.policies.get("h", "king")["next_day"], "2030-01-11")

    def test_policy_update_during_preparation_rejects_old_generation(self):
        self.policy()
        transaction = self.store.transaction
        changed = [False]

        @contextmanager
        def racing(write=False):
            if write and not changed[0]:
                changed[0] = True
                with transaction(True) as db:
                    db.execute(
                        "UPDATE calendar_policies SET price=300,version=version+1"
                    )
            with transaction(write) as db:
                yield db

        with patch.object(self.store, "transaction", racing):
            self.assertEqual(self.policies.roll_once()["inserted_days"], 0)
        self.assertEqual(self.policies.roll_once()["inserted_days"], 10)
        self.assertTrue(all(row["price"] == 300 for row in self.rows()))

    def test_clock_crossing_utc_day_does_not_publish_mixed_horizon(self):
        self.policy()
        transaction = self.store.transaction
        changed = [False]

        @contextmanager
        def midnight(write=False):
            if write and not changed[0]:
                changed[0] = True
                self.now += 86400
            with transaction(write) as db:
                yield db

        with patch.object(self.store, "transaction", midnight):
            self.assertEqual(self.policies.roll_once()["inserted_days"], 0)
        self.policies.roll_once()
        self.assertEqual(self.rows()[0]["day"], "2030-01-02")

    def test_delete_and_recreate_cannot_reuse_old_generation(self):
        self.policy()
        generation = self.policies.get("h", "king")["generation"]
        transaction = self.store.transaction
        changed = [False]

        @contextmanager
        def recreating(write=False):
            if write and not changed[0]:
                changed[0] = True
                self.policies.delete("h", "king")
                self.policy(price=300)
            with transaction(write) as db:
                yield db

        with patch.object(self.store, "transaction", recreating):
            self.assertEqual(self.policies.roll_once()["inserted_days"], 0)
        self.assertNotEqual(self.policies.get("h", "king")["generation"], generation)
        self.policies.roll_once()
        self.assertTrue(all(row["price"] == 300 for row in self.rows()))

    def test_input_is_owned_before_waiting_for_writer(self):
        body = {"capacity": 10, "overbook": 0, "price": 100, "horizon_days": 2}
        transaction = self.store.transaction

        @contextmanager
        def changing(write=False):
            if write:
                body["price"] = 999
            with transaction(write) as db:
                yield db

        with patch.object(self.store, "transaction", changing):
            self.policies.put("h", "king", body)
        self.assertEqual(self.policies.get("h", "king")["price"], 100)

    def test_invalid_policy_and_work_limits_have_no_side_effects(self):
        for changes in (
            {"price": True},
            {"overbook": 11},
            {"horizon_days": 731},
            {"capacity": -1},
            {"extra": 1},
        ):
            with self.assertRaises(ValueError):
                self.policy(**changes)
        for options in ({"max_rows": 0}, {"max_policies": True}):
            with self.assertRaises(ValueError):
                self.policies.roll_once(**options)
        self.assertEqual(self.rows(), [])

    def test_multi_room_availability_filters_quantity_and_missing_calendar(self):
        self.room("queen")
        self.room("incomplete")
        self.policy()
        self.policy("queen", capacity=1, overbook=0, price=250)
        self.policies.roll_once()
        result = available_rooms(
            self.store, "h", "2030-01-02", "2030-01-04", quantity=2
        )
        self.assertEqual([row["id"] for row in result["room_types"]], ["king"])
        self.assertEqual(result["room_types"][0]["total"], 400)
        self.assertTrue(result["advisory"])
        result["room_types"][0]["available"] = 999
        self.assertEqual(
            available_rooms(self.store, "h", "2030-01-02", "2030-01-04")["room_types"][
                0
            ]["available"],
            11,
        )

    def test_hotel_query_uses_one_snapshot_during_price_change(self):
        self.room("queen")
        self.policy()
        self.policy("queen")
        self.policies.roll_once()
        other = Store(self.store.path, clock=lambda: self.now)
        transaction = self.store.transaction
        fired = [False]

        @contextmanager
        def racing(write=False):
            with transaction(write) as db:

                def trace(statement):
                    if (
                        statement.startswith("SELECT id,name,description")
                        and not fired[0]
                    ):
                        fired[0] = True
                        other.set_days(
                            "h",
                            "queen",
                            [
                                {
                                    "day": day,
                                    "capacity": 10,
                                    "overbook": 10,
                                    "price": 500,
                                }
                                for day in ("2030-01-02", "2030-01-03")
                            ],
                        )

                db.set_trace_callback(trace)
                yield db

        with patch.object(self.store, "transaction", racing):
            result = available_rooms(self.store, "h", "2030-01-02", "2030-01-04")
        self.assertTrue(fired[0])
        self.assertEqual(
            [room["unit_total"] for room in result["room_types"]], [200, 200]
        )
        self.assertEqual(
            available_rooms(self.store, "h", "2030-01-02", "2030-01-04")["room_types"][
                1
            ]["unit_total"],
            1000,
        )

    def test_hotel_query_rejects_deleted_hotel_and_invalid_quantity(self):
        with self.assertRaises(ValueError):
            available_rooms(self.store, "h", "2030-01-02", "2030-01-04", quantity=True)
        self.store.manage("h", delete=True)
        with self.assertRaises(Error) as caught:
            available_rooms(self.store, "h", "2030-01-02", "2030-01-04")
        self.assertEqual(caught.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
