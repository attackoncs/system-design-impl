"""Protocol, bounded blocking work and owned WebSocket session behavior."""

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nearby_friends.core import Error
from nearby_friends.protocol import MAX_MESSAGE, MAX_OUTPUT, parse, validate
from nearby_friends.server import DeliveryTimeout, Session, run_server
from nearby_friends.work import WorkPool
import nearby_friends.server as server_module


class Socket:
    def __init__(self, fail=None):
        self.sent = []
        self.messages = asyncio.Queue()
        self.fail = fail

    async def send(self, raw):
        if self.fail:
            raise self.fail
        self.sent.append(json.loads(raw))

    async def recv(self):
        return await self.messages.get()


class Backend:
    """Controlled domain adapter; real Redis integration lives in test_network."""

    def __init__(self):
        self.rows = []
        self.updates = []
        self.sweeps = 0
        self.config = {"epoch": 0, "nodes": {}}
        self.friends = []
        self.store = SimpleNamespace(
            ttl=600,
            ring=lambda: self.config,
            friends=lambda actor: list(self.friends),
            authenticate=self.authenticate,
            sweep=self.sweep,
            read_history=lambda actor, offset, limit: [],
        )

    def authenticate(self, token):
        if token != "test-token":
            raise Error(401, "invalid token")
        return "alice"

    def sweep(self):
        self.sweeps += 1

    def snapshot(self, actor, radius):
        return [dict(row) for row in self.rows if row.get("distance_m", 0) <= radius]

    def channel(self, user):
        return "test:user:" + user

    def update(self, actor, body):
        self.updates.append((actor, body))
        return {"revision": len(self.updates), "hint_published": True}


class ProtocolTests(unittest.TestCase):
    def test_duplicate_nonfinite_and_oversize_json_are_rejected(self):
        for raw in (
            '{"op":"history","op":"snapshot"}',
            '{"op":"snapshot","radius_m":NaN}',
            '{"op":"snapshot","radius_m":1e1000}',
            '{"op":"history","nested":{"a":1,"a":2}}',
            "x" * (MAX_MESSAGE + 1),
            b'{"op":"history"}',
        ):
            with (
                self.subTest(raw=str(raw)[:80]),
                self.assertRaises((Error, ValueError)),
            ):
                parse(raw)

    def test_operation_fields_and_correlation_are_strict(self):
        for message in (
            [],
            {"op": True},
            {"op": "unknown"},
            {"op": "snapshot", "latitude": 0},
            {"op": "history", "request_id": True},
            {"op": "history", "request_id": ""},
            {"op": "history", "request_id": "x" * 65},
        ):
            with self.subTest(message=message), self.assertRaises(Error):
                validate(message)
        self.assertEqual(
            validate(parse('{"op":"history","target":"bob"}')),
            {"op": "history", "target": "bob"},
        )


class WorkPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_capacity_prevents_executor_queue_and_waiter_can_cancel(self):
        pool = WorkPool(1)
        entered, release = threading.Event(), threading.Event()
        later = []

        def slow():
            entered.set()
            release.wait(3)
            return "complete"

        first = asyncio.create_task(pool.call(slow))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            second = asyncio.create_task(pool.call(lambda: later.append(1)))
            await asyncio.sleep(0.02)
            self.assertFalse(second.done())
            self.assertEqual(later, [])
            second.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await second
            release.set()
            self.assertEqual(await first, "complete")
            self.assertEqual(later, [])
        finally:
            release.set()
            await pool.close()

    async def test_cancelled_active_work_retains_slot_until_underlying_exit(self):
        pool = WorkPool(1)
        entered, release = threading.Event(), threading.Event()
        calls = []

        def slow():
            entered.set()
            release.wait(3)
            calls.append("finished")

        first = asyncio.create_task(pool.call(slow))
        second = None
        try:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            first.cancel()
            second = asyncio.create_task(pool.call(lambda: calls.append("next")))
            await asyncio.sleep(0.02)
            first.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(first.done())
            self.assertFalse(second.done())
            self.assertEqual(calls, [])
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await first
            await second
            self.assertEqual(calls, ["finished", "next"])
        finally:
            release.set()
            await pool.close()

    async def test_close_drains_and_rejects_new_work(self):
        pool = WorkPool(1)
        entered, release = threading.Event(), threading.Event()

        def slow():
            entered.set()
            release.wait(3)

        active = asyncio.create_task(pool.call(slow))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            closing = asyncio.create_task(pool.close())
            await asyncio.sleep(0.02)
            self.assertFalse(closing.done())
            with self.assertRaises(Error) as failure:
                await pool.call(lambda: None)
            self.assertEqual(failure.exception.code, 503)
            release.set()
            await asyncio.gather(active, closing)
            with self.assertRaises(Error):
                await pool.call(lambda: None)
        finally:
            release.set()
            await pool.close()

    async def test_failure_releases_capacity(self):
        pool = WorkPool(1)
        try:

            def fail():
                raise OSError("unavailable")

            with self.assertRaises(OSError):
                await pool.call(fail)
            self.assertEqual(await pool.call(lambda: 42), 42)
        finally:
            await pool.close()

    def test_worker_configuration_is_bounded(self):
        for workers in (True, 0, -1, 129, 1.0, None):
            with self.subTest(workers=workers), self.assertRaises(ValueError):
                WorkPool(workers)


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend, self.ws = Backend(), Socket()
        self.session = Session(self.backend, "alice", self.ws, 0.1)

    async def asyncTearDown(self):
        await self.session.close()

    async def test_location_does_not_forward_transport_fields_to_store(self):
        message = {
            "op": "location",
            "request_id": "one",
            "latitude": 0,
            "longitude": 1,
            "timestamp": 100,
            "device": "phone",
        }
        self.assertEqual((await self.session.dispatch(message))["revision"], 1)
        self.assertEqual(
            self.backend.updates,
            [
                (
                    "alice",
                    {
                        "latitude": 0,
                        "longitude": 1,
                        "timestamp": 100,
                        "device": "phone",
                    },
                )
            ],
        )

    async def test_snapshot_smaller_radius_delivers_leave_for_previously_loaded_friend(
        self,
    ):
        self.backend.rows = [
            {"id": "bob", "distance_m": 100},
            {"id": "carol", "distance_m": 1000},
        ]
        client = {"carol": self.backend.rows[1]}
        self.session.known = dict(client)
        result = await self.session.dispatch({"op": "snapshot", "radius_m": 500})
        self.assertEqual([row["id"] for row in result["friends"]], ["bob"])
        client.update((row["id"], row) for row in result["friends"])
        await self.session.deltas()
        changes = [change for frame in self.ws.sent for change in frame["changes"]]
        self.assertIn({"event": "leave", "id": "carol"}, changes)
        for change in changes:
            if change["event"] == "leave":
                client.pop(change["id"], None)
            else:
                client[change["friend"]["id"]] = change["friend"]
        self.assertEqual(set(client), {"bob"})
        with self.assertRaises(Error):
            await self.session.dispatch({"op": "snapshot", "offset": True})
        self.assertEqual(self.session.radius, 500)

    async def test_snapshot_later_page_does_not_swallow_update_on_another_loaded_page(
        self,
    ):
        client = {
            "bob": {"id": "bob", "distance_m": 100, "revision": 1},
            "carol": {"id": "carol", "distance_m": 200, "revision": 1},
        }
        self.session.known = dict(client)
        self.backend.rows = [dict(client["bob"], revision=2), dict(client["carol"])]
        page = await self.session.dispatch({"op": "snapshot", "offset": 1, "limit": 1})
        self.assertEqual([row["id"] for row in page["friends"]], ["carol"])
        client.update((row["id"], row) for row in page["friends"])
        await self.session.deltas()
        changes = [change for frame in self.ws.sent for change in frame["changes"]]
        self.assertEqual([change["event"] for change in changes], ["update"])
        for change in changes:
            client[change["friend"]["id"]] = change["friend"]
        self.assertEqual(client["bob"]["revision"], 2)
        self.assertEqual(set(client), {"bob", "carol"})

    async def test_initial_send_failure_still_cleans_up_watchers(self):
        self.backend.config = {"epoch": 1, "nodes": {"one": "redis://localhost:1"}}
        started, finished = asyncio.Event(), asyncio.Event()

        async def watch(url, channels):
            started.set()
            try:
                await asyncio.Future()
            finally:
                finished.set()

        self.session.watch = watch
        self.ws.fail = OSError("disconnected during initial snapshot")
        with self.assertRaises(OSError):
            await self.session.run()
        self.assertTrue(started.is_set())
        self.assertTrue(finished.is_set())
        self.assertEqual(self.session.tasks, [])

    async def test_initial_snapshot_failure_still_cleans_up_watchers(self):
        self.backend.config = {"epoch": 1, "nodes": {"one": "redis://localhost:1"}}
        ended = asyncio.Event()

        async def watch(*args):
            try:
                await asyncio.Future()
            finally:
                ended.set()

        def fail(*args):
            raise OSError("database unavailable")

        self.session.watch = watch
        self.backend.snapshot = fail
        with self.assertRaises(OSError):
            await self.session.run()
        self.assertTrue(ended.is_set())
        self.assertEqual(self.session.tasks, [])

    async def test_subscription_refresh_preserves_unchanged_watchers(self):
        self.backend.config = {"epoch": 1, "nodes": {"one": "redis://localhost:1"}}
        self.backend.friends = ["bob"]

        async def watch(*args):
            await asyncio.Future()

        self.session.watch = watch
        await self.session.subscriptions()
        original = self.session.tasks[0]
        self.backend.config["epoch"] = 2
        await self.session.subscriptions()
        self.assertIs(self.session.tasks[0], original)
        self.backend.friends.append("carol")
        await self.session.subscriptions()
        self.assertTrue(original.done())
        self.assertIsNot(self.session.tasks[0], original)

    async def test_watcher_cleanup_closes_both_resources_when_one_close_fails(self):
        ready, pub_closed, client_closed = asyncio.Event(), [], []

        class PubSub:
            async def subscribe(self, *channels):
                ready.set()

            async def get_message(self, **kwargs):
                await asyncio.Future()

            async def aclose(self):
                pub_closed.append(1)
                raise OSError("cleanup failure")

        class Redis:
            def pubsub(self):
                return PubSub()

            async def aclose(self):
                client_closed.append(1)

        with patch.object(
            server_module.aioredis.Redis, "from_url", return_value=Redis()
        ):
            watcher = asyncio.create_task(
                self.session.watch("redis://localhost:1", ["x"])
            )
            await ready.wait()
            watcher.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await watcher
        self.assertEqual((pub_closed, client_closed), ([1], [1]))

    async def test_delta_chunks_and_failed_send_does_not_advance_known(self):
        self.backend.rows = [{"id": str(i), "distance_m": 1} for i in range(201)]
        await self.session.deltas()
        self.assertEqual(
            [len(frame["changes"]) for frame in self.ws.sent], [100, 100, 1]
        )
        old = dict(self.session.known)
        self.backend.rows = []
        self.ws.fail = OSError("disconnected")
        with self.assertRaises(OSError):
            await self.session.deltas()
        self.assertEqual(self.session.known, old)

    async def test_invalid_command_correlates_error_and_session_continues(self):
        await self.ws.messages.put('{"op":"history","extra":true,"request_id":"bad"}')
        await self.ws.messages.put(
            '{"op":"history","target":"bob","request_id":"good"}'
        )
        task = asyncio.create_task(self.session.run())
        try:
            for _ in range(100):
                if any(frame.get("request_id") == "good" for frame in self.ws.sent):
                    break
                await asyncio.sleep(0.005)
            bad = next(
                frame for frame in self.ws.sent if frame.get("request_id") == "bad"
            )
            good = next(
                frame for frame in self.ws.sent if frame.get("request_id") == "good"
            )
            self.assertEqual((bad["type"], bad["code"]), ("error", 400))
            self.assertEqual((good["type"], good["history"]), ("ack", []))
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_slow_consumer_and_oversize_output_are_bounded(self):
        async def slow(raw):
            await asyncio.Future()

        self.ws.send = slow
        self.session.send_timeout = 0.02
        with self.assertRaises(DeliveryTimeout):
            await self.session.send({"type": "snapshot"})
        with self.assertRaises(Error) as failure:
            await self.session.send({"large": "x" * MAX_OUTPUT})
        self.assertEqual(failure.exception.code, 413)

    async def test_large_delta_batch_uses_one_total_sending_budget(self):
        self.backend.rows = [{"id": str(i), "distance_m": 1} for i in range(301)]
        self.session.send_timeout = 0.08
        delivered = []

        async def slow(raw):
            # Each frame individually fits the timeout; the whole batch does not.
            await asyncio.sleep(0.035)
            delivered.append(json.loads(raw))

        self.ws.send = slow
        with self.assertRaises(DeliveryTimeout):
            await self.session.deltas()
        self.assertLess(len(delivered), 4)
        self.assertTrue(all(frame["type"] == "delta" for frame in delivered))


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, backend, **options):
        ready = asyncio.get_running_loop().create_future()
        original = server_module.serve

        @asynccontextmanager
        async def observed(*args, **kwargs):
            async with original(*args, **kwargs) as listener:
                ready.set_result(listener.sockets[0].getsockname()[1])
                yield listener

        stop = asyncio.Event()
        with patch.object(server_module, "serve", observed):
            task = asyncio.create_task(
                run_server(backend, "127.0.0.1", 0, stop=stop, **options)
            )
            port = await asyncio.wait_for(ready, 3)
        return f"ws://127.0.0.1:{port}", stop, task

    async def test_real_websocket_capacity_protocol_and_stop(self):
        backend = Backend()
        url, stop, task = await self.start(backend, max_connections=1, workers=1)
        first = second = None
        try:
            first = await connect(
                url, additional_headers={"Authorization": "Bearer test-token"}
            )
            self.assertEqual(json.loads(await first.recv())["type"], "snapshot")
            with self.assertRaises(InvalidStatus) as failure:
                await connect(
                    url, additional_headers={"Authorization": "Bearer test-token"}
                )
            self.assertEqual(failure.exception.response.status_code, 503)
            await first.send('{"op":"history","request_id":"x","offset":0}')
            response = json.loads(await first.recv())
            self.assertEqual((response["type"], response["request_id"]), ("ack", "x"))
            stop.set()
            with self.assertRaises(ConnectionClosed):
                await first.recv()
            await asyncio.wait_for(task, 3)
            with self.assertRaises(OSError):
                await connect(url, open_timeout=0.2)
        finally:
            stop.set()
            for ws in (first, second):
                if ws is not None:
                    await ws.close()
            await asyncio.wait_for(task, 3)

    async def test_stop_waits_for_accepted_domain_work(self):
        backend = Backend()
        entered, release, exited = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        def update(actor, body):
            entered.set()
            release.wait(3)
            exited.set()
            return {"revision": 1}

        backend.update = update
        url, stop, task = await self.start(backend, workers=1)
        ws = None
        try:
            ws = await connect(
                url, additional_headers={"Authorization": "Bearer test-token"}
            )
            await ws.recv()
            await ws.send('{"op":"location","latitude":0,"longitude":0,"timestamp":1}')
            while not entered.is_set():
                await asyncio.sleep(0.001)
            stop.set()
            await asyncio.sleep(0.03)
            self.assertFalse(task.done())
            self.assertFalse(exited.is_set())
            release.set()
            await asyncio.wait_for(task, 3)
            self.assertTrue(exited.is_set())
        finally:
            release.set()
            stop.set()
            if ws is not None:
                await ws.close()
            await asyncio.wait_for(task, 3)

    async def test_bind_failure_does_not_start_maintenance(self):
        backend = Backend()
        with patch.object(server_module, "serve", side_effect=OSError("bind failed")):
            with self.assertRaises(OSError):
                await run_server(backend, "127.0.0.1", 0)
        self.assertEqual(backend.sweeps, 0)

    async def test_cancellation_drains_before_server_returns(self):
        backend = Backend()
        entered, release, exited = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        def update(actor, body):
            entered.set()
            release.wait(3)
            exited.set()
            return {"revision": 1}

        backend.update = update
        url, stop, task = await self.start(backend, workers=1)
        ws = None
        try:
            ws = await connect(
                url, additional_headers={"Authorization": "Bearer test-token"}
            )
            await ws.recv()
            await ws.send('{"op":"location","latitude":0,"longitude":0,"timestamp":1}')
            while not entered.is_set():
                await asyncio.sleep(0.001)
            task.cancel()
            await asyncio.sleep(0.03)
            self.assertFalse(task.done())
            self.assertFalse(exited.is_set())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
            self.assertTrue(exited.is_set())
        finally:
            release.set()
            stop.set()
            if ws is not None:
                await ws.close()
            await asyncio.gather(task, return_exceptions=True)

    async def test_duration_configuration_rejects_nonfinite_boolean_and_huge_values(
        self,
    ):
        for option in ("reconcile", "send_timeout"):
            for value in (True, float("inf"), float("nan"), 10**1000, "1", 0):
                with (
                    self.subTest(option=option, value=type(value).__name__),
                    self.assertRaises(ValueError),
                ):
                    await run_server(Backend(), "127.0.0.1", 0, **{option: value})


if __name__ == "__main__":
    unittest.main()
