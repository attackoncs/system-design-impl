"""Private adaptive navigation, durable CAS and finite event recovery."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from maps_service.http import server
from maps_service.store import Error, Store
from maps_service.tracking import NavigationTracker
from maps_service.navigation import route


@contextmanager
def running(http):
    thread = threading.Thread(target=http.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}"
    finally:
        http.shutdown()
        http.server_close()
        thread.join(2)


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = [2000000000.0]
        self.path = Path(self.temp.name) / "metadata.sqlite"
        self.store = Store(self.path, clock=lambda: self.now[0])
        self.graph = json.loads((ROOT / "examples/roads.json").read_text())
        self.store.import_graph(self.graph)
        self.store.provision("alice", "alice-private")
        self.store.provision("bob", "bob-private")
        self.tracker = self.tracked()

    def tracked(self, store=None, **kwargs):
        tracker = NavigationTracker(self.store if store is None else store, **kwargs)
        self.addCleanup(tracker.close)
        return tracker

    def create(self, **changes):
        return self.tracker.create(
            "alice", {"origin": "A", "destination": "C", **changes}
        )

    def assert_error(self, status, function, *args, **kwargs):
        with self.assertRaises(Error) as failure:
            function(*args, **kwargs)
        self.assertEqual(failure.exception.status, status)

    def test_owner_expiry_response_isolation_and_restart(self):
        options = {
            "origin": {"latitude": 0, "longitude": -0.002},
            "destination": "C",
            "ttl": 10,
        }
        result = self.tracker.create("alice", options)
        identifier = result["id"]
        options["origin"]["longitude"] = 100
        result["route"]["routes"][0]["edges"].clear()
        self.assertEqual(
            self.tracker.read("alice", identifier)["route"]["routes"][0]["edges"],
            ["ab", "bc"],
        )
        self.assert_error(404, self.tracker.read, "bob", identifier)
        self.assertNotIn(identifier, json.dumps(self.store.export()))
        reopened = self.tracked(Store(self.path, clock=lambda: self.now[0]))
        self.assertEqual(reopened.read("alice", identifier)["revision"], 1)
        self.now[0] += 10
        self.assert_error(404, reopened.read, "alice", identifier)
        self.assert_error(404, reopened.events, "alice", identifier, wait=0)
        self.tracker.run_once()
        with self.store.transaction() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM navigation_sessions").fetchone()[0], 0
            )

    def test_capacity_is_transactional_and_expiry_frees_it(self):
        tracker = self.tracked(max_sessions=1)
        first = tracker.create("alice", {"origin": "A", "destination": "C", "ttl": 1})
        self.assert_error(
            503, tracker.create, "bob", {"origin": "A", "destination": "C"}
        )
        self.now[0] += 1
        second = tracker.create("bob", {"origin": "A", "destination": "C"})
        self.assertNotEqual(first["id"], second["id"])
        self.assert_error(404, tracker.read, "alice", first["id"])

    def test_move_cannot_overwrite_a_concurrent_newer_origin(self):
        created = self.create()
        entered, release = threading.Event(), threading.Event()

        def slow(store, **options):
            result = route(store, **options)
            if options["origin"] == "A":
                entered.set()
                self.assertTrue(release.wait(3))
            return result

        with patch("maps_service.tracking.route", slow), ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.tracker.move, "alice", created["id"], "A")
            try:
                self.assertTrue(entered.wait(1))
                accepted = self.tracker.move("alice", created["id"], "B")
            finally:
                release.set()
            self.assert_error(409, pending.result, 2)
        latest = self.tracker.read("alice", created["id"])
        self.assertEqual(latest["revision"], accepted["revision"])
        self.assertEqual(latest["route"]["routes"][0]["nodes"][0], "B")

    def test_obsolete_refresh_cannot_overwrite_concurrent_position_update(self):
        created = self.create()
        entered, release = threading.Event(), threading.Event()

        def slow(store, **options):
            result = route(store, **options)
            if options["origin"] == "A":
                entered.set()
                self.assertTrue(release.wait(3))
            return result

        with patch("maps_service.tracking.route", slow), ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.tracker.run_once)
            try:
                self.assertTrue(entered.wait(1))
                accepted = self.tracker.move("alice", created["id"], "B")
            finally:
                release.set()
            self.assertEqual(pending.result(2), 0)
        self.assertEqual(
            self.tracker.read("alice", created["id"])["route"], accepted["route"]
        )

    def test_refresh_cannot_restore_removed_or_expired_session(self):
        created = self.create(ttl=1)
        entered, release = threading.Event(), threading.Event()

        def slow(store, **options):
            result = route(store, **options)
            entered.set()
            self.assertTrue(release.wait(3))
            return result

        with patch("maps_service.tracking.route", slow), ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.tracker.run_once)
            try:
                self.assertTrue(entered.wait(1))
                self.tracker.remove("alice", created["id"])
                self.now[0] += 1
            finally:
                release.set()
            self.assertEqual(pending.result(2), 0)
        self.assert_error(404, self.tracker.read, "alice", created["id"])

    def test_traffic_expiry_graph_change_unreachable_and_recovery_emit_latest(self):
        created = self.create()
        identifier = created["id"]
        self.tracker.run_once()
        self.store.traffic([{"edge": "bc", "speed_mps": 1}], ttl=5)
        self.assertEqual(self.tracker.run_once(), 1)
        detour = self.tracker.read("alice", identifier)
        self.assertEqual(detour["route"]["routes"][0]["edges"], ["ad", "de", "ec"])
        self.assertGreater(detour["revision"], created["revision"])
        self.now[0] += 5
        self.tracker.run_once()
        restored = self.tracker.read("alice", identifier)
        self.assertEqual(restored["route"]["routes"][0]["edges"], ["ab", "bc"])
        self.store.traffic(
            [{"edge": "bc", "closed": True}, {"edge": "ec", "closed": True}]
        )
        self.tracker.run_once()
        blocked = self.tracker.read("alice", identifier)
        self.assertEqual(blocked["route"]["routes"], [])
        self.assertEqual(blocked["route"]["error"]["status"], 404)
        graph = self.store.import_graph(self.graph)["graph"]
        self.tracker.run_once()
        final = self.tracker.read("alice", identifier)
        self.assertEqual(final["route"]["graph"], graph)
        self.assertEqual(final["route"]["routes"][0]["edges"], ["ab", "bc"])
        events = self.tracker.events(
            "alice", identifier, after=created["revision"], wait=0
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["id"], final["revision"])
        self.assertEqual(events[0]["data"], final)

    def test_full_refresh_discovers_faster_route_outside_current_route_area(self):
        points = [
            ("A", 10, 10),
            ("B", 10, 10.001),
            ("C", 10, 10.002),
            ("D", 10.02, 10),
            ("E", 10.02, 10.002),
        ]
        edges = [
            ("ab", "A", "B", 200, 2),
            ("bc", "B", "C", 200, 2),
            ("ad", "A", "D", 3000, 100),
            ("de", "D", "E", 300, 1),
            ("ec", "E", "C", 3000, 100),
        ]
        self.store.import_graph(
            {
                "nodes": [
                    {"id": name, "latitude": lat, "longitude": lon}
                    for name, lat, lon in points
                ],
                "edges": [
                    {
                        "id": name,
                        "source": start,
                        "target": end,
                        "length_m": length,
                        "speeds": {"driving": speed},
                    }
                    for name, start, end, length, speed in edges
                ],
            }
        )
        created = self.create()
        self.tracker.run_once()
        self.store.traffic([{"edge": "de", "speed_mps": 100}])
        self.assertEqual(self.tracker.run_once(), 0)
        self.assertEqual(
            self.tracker.read("alice", created["id"])["route"]["routes"][0]["edges"],
            ["ab", "bc"],
        )
        self.now[0] += self.tracker.refresh_interval
        self.assertEqual(self.tracker.run_once(), 1)
        self.assertEqual(
            self.tracker.read("alice", created["id"])["route"]["routes"][0]["edges"],
            ["ad", "de", "ec"],
        )

    def test_events_reopen_cancellation_and_cursor_validation(self):
        created = self.create()
        identifier = created["id"]
        other = self.tracked(Store(self.path, clock=lambda: self.now[0]))
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(
                other.events, "alice", identifier, created["revision"], 1
            )
            self.tracker.move("alice", identifier, "B")
            event = pending.result(2)[0]
            self.assertGreater(event["id"], created["revision"])
            self.assertEqual(event["data"]["route"]["routes"][0]["nodes"][0], "B")
        self.assert_error(409, other.events, "alice", identifier, event["id"] + 1, 0)
        cancelled = threading.Event()
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(
                other.events, "alice", identifier, event["id"], 5, cancelled
            )
            cancelled.set()
            self.assertEqual(pending.result(1), [])
        for wait in (True, -1, 6, float("inf"), float("nan"), 10**1000):
            with self.subTest(wait=type(wait).__name__), self.assertRaises(ValueError):
                other.events("alice", identifier, wait=wait)
        for after in (True, -1, 2**63, "0"):
            with self.subTest(after=after), self.assertRaises(ValueError):
                other.events("alice", identifier, after=after)

    def test_close_drains_accepted_create_without_rejecting_its_result(self):
        entered, release, closed = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        def slow(store, **options):
            result = route(store, **options)
            entered.set()
            self.assertTrue(release.wait(3))
            return result

        with patch("maps_service.tracking.route", slow), ThreadPoolExecutor(2) as pool:
            pending = pool.submit(self.create)
            try:
                self.assertTrue(entered.wait(1))

                def close():
                    self.tracker.close()
                    closed.set()

                closing = pool.submit(close)
                self.assertFalse(closed.wait(0.05))
            finally:
                release.set()
            self.assertEqual(pending.result(2)["revision"], 1)
            closing.result(2)
        self.assert_error(503, self.tracker.read, "alice", "absent")
        with self.assertRaises(RuntimeError):
            self.tracker.start()

    def test_close_wakes_event_waiters_and_stops_before_the_next_refresh(self):
        created = self.create()
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.tracker.events, "alice", created["id"], 1, 5)
            deadline = time.monotonic() + 1
            while self.tracker._active == 0 and time.monotonic() < deadline:
                time.sleep(0.001)
            self.tracker.close()
            self.assertEqual(pending.result(1), [])
        self.assertFalse(self.tracker.thread.is_alive())

    def test_close_finishes_current_route_without_starting_later_batch_work(self):
        self.create()
        self.create()
        entered, release, closing = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        calls = []

        def slow(store, **options):
            calls.append(1)
            result = route(store, **options)
            entered.set()
            self.assertTrue(release.wait(3))
            return result

        with patch("maps_service.tracking.route", slow), ThreadPoolExecutor(2) as pool:
            pending = pool.submit(self.tracker.run_once)
            try:
                self.assertTrue(entered.wait(1))

                def close():
                    closing.set()
                    self.tracker.close()

                stopped = pool.submit(close)
                self.assertTrue(closing.wait(1))
                self.assertTrue(self.tracker.stop.wait(1))
                self.assertFalse(stopped.done())
            finally:
                release.set()
            pending.result(2)
            stopped.result(2)
        self.assertEqual(len(calls), 1)

    def test_background_failure_retries_and_clears_diagnostic(self):
        tracker = self.tracked(interval=0.01)
        self.create()
        original, completed = tracker.run_once, threading.Event()
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("private coordinate 12.345678")
            result = original()
            completed.set()
            return result

        with (
            patch.object(tracker, "run_once", flaky),
            self.assertLogs("maps_service.tracking", level="WARNING") as logged,
        ):
            tracker.start()
            self.assertTrue(completed.wait(2))
            tracker.close()
        self.assertIsNone(tracker.last_error)
        self.assertNotIn("12.345678", " ".join(logged.output))
        self.assertGreaterEqual(len(attempts), 2)

    def test_configuration_and_invalid_requests(self):
        for kwargs in (
            {"interval": True},
            {"interval": 0},
            {"refresh_interval": float("nan")},
            {"max_sessions": True},
            {"max_sessions": 0},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                NavigationTracker(self.store, **kwargs)
        for body in (
            {},
            {"origin": "A", "destination": "C", "ttl": True},
            {"origin": "A", "destination": "C", "ttl": 3601},
            {"origin": "A", "destination": "C", "extra": 1},
        ):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.tracker.create("alice", body)
        self.store.clock = lambda: 10**1000
        with self.assertRaises(ValueError):
            self.tracker._now()

    def test_live_http_sse_delivers_new_route_and_enforces_owner(self):
        created = self.create()
        identifier = created["id"]
        self.store.traffic([{"edge": "bc", "closed": True}])
        self.tracker.run_once()
        with running(
            server("127.0.0.1", 0, store=self.store, tracker=self.tracker)
        ) as url:
            request = Request(
                url + f"/v1/navigation/{identifier}/events?after=1",
                headers={"Authorization": "Bearer alice-private"},
            )
            with urlopen(request, timeout=2) as response:
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                payload = response.read().decode()
            data = json.loads(
                next(
                    line[6:]
                    for line in payload.splitlines()
                    if line.startswith("data: ")
                )
            )
            self.assertEqual(data["route"]["routes"][0]["edges"], ["ad", "de", "ec"])
            with self.assertRaises(HTTPError) as error:
                urlopen(
                    Request(
                        url + f"/v1/navigation/{identifier}/events",
                        headers={"Authorization": "Bearer bob-private"},
                    ),
                    timeout=2,
                )
            self.assertEqual(error.exception.code, 404)
            error.exception.close()


if __name__ == "__main__":
    unittest.main()
