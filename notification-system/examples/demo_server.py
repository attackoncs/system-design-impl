#!/usr/bin/env python3
"""Authenticated local HTTP demo with live workers and optional SQLite recovery.

Set NOTIFICATION_API_KEY and run `python examples/demo_server.py --database demo.sqlite3`.
Providers are simulated. Deploy behind a TLS gateway for remote access.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime
import hmac
import http.server
import json
import os
from pathlib import Path
import sys
import threading
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from notification_system import (
    Channel,
    ContactInfo,
    NotificationService,
    NotificationRequest,
    NotificationTemplate,
    NotificationStatus,
    NotificationLog,
    SimulatedProvider,
    SQLiteMessageQueue,
    SQLiteNotificationLog,
    ValidationError,
    TemplateRenderError,
    DuplicateNotificationError,
    RateLimitExceededError,
    QueueFullError,
)


class Application:
    """Own one event-loop thread so HTTP requests and workers share one loop."""

    def __init__(self, database=None):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.service = None
        self.pool = None
        self.call(self._initialize(database))

    async def _initialize(self, database):
        queue = SQLiteMessageQueue(database) if database else None
        self.service = NotificationService(queue=queue)
        self.audit = (
            NotificationLog(SQLiteNotificationLog(database))
            if database
            else NotificationLog()
        )
        self.service.contacts.set(
            ContactInfo("user_1", email="user1@example.com", phone="+1234567890")
        )
        self.service.contacts.add_device_token(
            "user_1", "ios_token_1", Channel.IOS_PUSH
        )
        self.service.contacts.add_device_token(
            "user_1", "android_token_1", Channel.ANDROID_PUSH
        )
        self.service.templates.register(
            NotificationTemplate(
                "welcome",
                "Welcome, {name}!",
                "Hello {name}, thanks for joining {app_name}.",
            )
        )
        self.pool = self.service.create_worker_pool(
            {channel: SimulatedProvider(channel) for channel in Channel}, self.audit
        )
        await self.pool.start()

    def call(self, operation):
        future = asyncio.run_coroutine_threadsafe(operation, self.loop)
        try:
            return future.result(timeout=15)
        except BaseException:
            future.cancel()
            raise

    async def dispatch(self, method, path, data):
        service = self.service
        if method == "POST" and path == "/notifications":
            payload = dict(data)
            scheduled_at = payload.pop("scheduled_at", None)
            if scheduled_at is not None:
                scheduled_at = datetime.datetime.fromisoformat(scheduled_at)
            payload["channel"] = Channel(payload["channel"])
            notification_id = await service.send(
                NotificationRequest(**payload), scheduled_at=scheduled_at
            )
            return 202, {"notification_id": notification_id, "status": "queued"}
        if method == "GET" and path.startswith("/notifications/"):
            notification_id = path.rsplit("/", 1)[1]
            events = service.tracker.get_history(notification_id)
            # Audit history survives restart when SQLite is selected.
            audit = await asyncio.to_thread(
                self.audit.query, notification_id=notification_id
            )
            if not events and not audit:
                return 404, {"error": "Notification not found"}
            return 200, {
                "events": [dataclasses.asdict(event) for event in events],
                "audit": [dataclasses.asdict(row) for row in audit],
            }
        if method == "GET" and path == "/analytics":
            return 200, {
                "aggregate": service.tracker.get_aggregate_counts(),
                "queues": {
                    channel.value: {
                        "pending": service.queue.depth(channel),
                        "in_flight": service.queue.in_flight(channel),
                    }
                    for channel in Channel
                },
            }
        if path.startswith("/settings/") and method in ("PUT", "GET"):
            user = path.rsplit("/", 1)[1]
            if method == "PUT":
                preferences = data.get("preferences", {})
                if not isinstance(preferences, dict) or any(
                    type(value) is not bool for value in preferences.values()
                ):
                    raise ValueError("preferences must map channels to booleans")
                service.settings.bulk_update(
                    user, {Channel(key): value for key, value in preferences.items()}
                )
            return 200, {
                key.value: value
                for key, value in service.settings.get_all_preferences(user).items()
            }
        if method == "POST" and path == "/templates":
            service.templates.register(NotificationTemplate(**data))
            return 201, {"template_id": data["template_id"]}
        if method == "GET" and path == "/templates":
            return 200, {"templates": service.templates.list_ids()}
        if method == "POST" and path == "/events":
            status = NotificationStatus(data["status"])
            if status not in (NotificationStatus.DELIVERED, NotificationStatus.CLICKED):
                raise ValueError("Only delivered/clicked callbacks are supported")
            notification_id = data["notification_id"]
            history = service.tracker.get_history(notification_id)
            if not history:
                raise ValueError(
                    "No live notification history; reload history before callbacks after restart"
                )
            service.tracker.record(notification_id, history[0].channel, status)
            return 200, {"status": status.value}
        return 404, {"error": "Not found"}

    def close(self):
        async def stop():
            if self.pool is not None:
                try:
                    await asyncio.wait_for(self.pool.stop(), timeout=5)
                except asyncio.TimeoutError:
                    await self.pool.stop(graceful=False)

        try:
            self.call(stop())
            self.call(self.loop.shutdown_asyncgens())
            self.call(self.loop.shutdown_default_executor())
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)
            self.loop.close()


def create_server(app, api_key, host="127.0.0.1", port=8000):
    if not isinstance(api_key, str) or not api_key:
        raise ValueError("Set a nonempty NOTIFICATION_API_KEY")

    class Handler(http.server.BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def _respond(self, status, payload):
            body = json.dumps(
                payload,
                default=lambda value: (
                    value.value if hasattr(value, "value") else str(value)
                ),
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle(self):
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(
                supplied.encode(), ("Bearer " + api_key).encode()
            ):
                self._respond(401, {"error": "Unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if (
                    self.headers.get("Transfer-Encoding")
                    or not 0 <= length <= 1024 * 1024
                ):
                    self._respond(
                        413, {"error": "Body must be at most 1 MiB with Content-Length"}
                    )
                    return
                payload = json.loads(self.rfile.read(length)) if length else {}
                if not isinstance(payload, dict):
                    raise ValueError("Expected a JSON object")
                status, result = app.call(
                    app.dispatch(
                        self.command, urlparse(self.path).path.rstrip("/"), payload
                    )
                )
                self._respond(status, result)
            except DuplicateNotificationError as error:
                self._respond(409, {"error": str(error)})
            except RateLimitExceededError as error:
                self._respond(429, {"error": str(error)})
            except QueueFullError as error:
                self._respond(503, {"error": str(error)})
            except (
                ValueError,
                TypeError,
                KeyError,
                UnicodeError,
                ValidationError,
                TemplateRenderError,
            ) as error:
                self._respond(400, {"error": str(error)})
            except Exception:
                self.log_error("Notification request failed")
                self._respond(500, {"error": "Internal error"})

        do_GET = _handle
        do_POST = _handle
        do_PUT = _handle

    return http.server.HTTPServer((host, port), Handler)


def run_server(host="127.0.0.1", port=8000, database=None):
    api_key = os.environ.get("NOTIFICATION_API_KEY")
    if not api_key:
        raise ValueError("Set NOTIFICATION_API_KEY before starting the server")
    app = Application(database)
    server = None
    try:
        server = create_server(app, api_key, host, port)
        print(
            f"Notification demo listening on http://{host}:{server.server_port}; providers are simulated"
        )
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None:
            server.server_close()
        app.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--database")
    args = parser.parse_args()
    run_server(port=args.port, database=args.database)
