#!/usr/bin/env python3
"""HTTP demo server for the Notification System.

A lightweight REST API server using only Python standard library modules
(http.server, asyncio, json). Demonstrates the full notification pipeline
through HTTP endpoints.

Usage:
    python examples/demo_server.py [--port PORT]

Endpoints:
    POST /notifications          - Send a notification (201, 400, 429)
    GET  /notifications/<id>     - Get delivery status and event history (200, 404)
    GET  /analytics              - Aggregate analytics (200)
    GET  /settings/<user_id>     - Get user preferences (200)
    PUT  /settings/<user_id>     - Update user preferences (200)
    POST /templates              - Create a template (201)
    GET  /templates              - List templates (200)
"""

from __future__ import annotations

import asyncio
import json
import sys
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any
from urllib.parse import urlparse

# Add the src directory to the path so we can import the library
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from notification_system import (
    Channel,
    ContactInfo,
    ContactInfoStore,
    DuplicateNotificationError,
    NotificationConfig,
    NotificationRequest,
    NotificationService,
    NotificationSettings,
    NotificationTemplate,
    QueueFullError,
    RateLimitExceededError,
    TemplateRenderError,
    ValidationError,
)
from notification_system.tracker import EventTracker
from notification_system.templates import TemplateRegistry


# Channel name mapping for user-friendly input
CHANNEL_MAP = {
    "ios_push": Channel.IOS_PUSH,
    "android_push": Channel.ANDROID_PUSH,
    "sms": Channel.SMS,
    "email": Channel.EMAIL,
}


def create_service() -> NotificationService:
    """Create and configure a NotificationService with demo data."""
    config = NotificationConfig()
    contacts = ContactInfoStore()
    settings = NotificationSettings()
    tracker = EventTracker()
    template_registry = TemplateRegistry()

    # Seed some demo contact data
    contacts.set(ContactInfo(
        user_id="user_1",
        email="user1@example.com",
        phone="+1234567890",
        device_tokens=["ios_token_abc123"],
    ))
    contacts.set(ContactInfo(
        user_id="user_2",
        email="user2@example.com",
        phone="+0987654321",
        device_tokens=["android_token_xyz789"],
    ))

    # Seed a demo template
    welcome_template = NotificationTemplate(
        template_id="welcome",
        title="Welcome, {name}!",
        body="Hello {name}, thanks for joining {app_name}.",
    )
    template_registry.register(welcome_template)

    service = NotificationService(
        config=config,
        contacts=contacts,
        settings=settings,
        tracker=tracker,
        template_registry=template_registry,
    )
    return service


# Global service instance
SERVICE = create_service()

# Global asyncio event loop for running async operations
LOOP = asyncio.new_event_loop()


class NotificationHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the notification system REST API."""

    def do_GET(self) -> None:
        """Handle GET requests."""
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path.startswith("/notifications/"):
            notification_id = path[len("/notifications/"):]
            self._handle_get_notification(notification_id)
        elif path == "/analytics":
            self._handle_get_analytics()
        elif path.startswith("/settings/"):
            user_id = path[len("/settings/"):]
            self._handle_get_settings(user_id)
        elif path == "/templates":
            self._handle_list_templates()
        else:
            self._send_error(404, "Not found")

    def do_POST(self) -> None:
        """Handle POST requests."""
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/notifications":
            self._handle_send_notification()
        elif path == "/templates":
            self._handle_create_template()
        else:
            self._send_error(404, "Not found")

    def do_PUT(self) -> None:
        """Handle PUT requests."""
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path.startswith("/settings/"):
            user_id = path[len("/settings/"):]
            self._handle_update_settings(user_id)
        else:
            self._send_error(404, "Not found")

    def _read_json_body(self) -> dict[str, Any] | None:
        """Read and parse JSON request body."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        body = self.rfile.read(content_length)
        try:
            return json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _send_json(self, status: int, data: Any) -> None:
        """Send a JSON response."""
        body = json.dumps(data, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, message: str) -> None:
        """Send a JSON error response."""
        self._send_json(status, {"error": message})

    def _handle_send_notification(self) -> None:
        """POST /notifications - Send a notification."""
        data = self._read_json_body()
        if data is None:
            self._send_error(400, "Invalid JSON body")
            return

        # Validate required fields
        recipient_id = data.get("recipient_id")
        channel_str = data.get("channel")
        if not recipient_id or not channel_str:
            self._send_error(400, "Missing required fields: recipient_id, channel")
            return

        # Parse channel
        channel = CHANNEL_MAP.get(channel_str)
        if channel is None:
            self._send_error(
                400,
                f"Invalid channel '{channel_str}'. "
                f"Valid channels: {list(CHANNEL_MAP.keys())}",
            )
            return

        # Build the notification request
        request = NotificationRequest(
            recipient_id=recipient_id,
            channel=channel,
            title=data.get("title", ""),
            body=data.get("body", ""),
            template_id=data.get("template_id"),
            template_params=data.get("template_params", {}),
            metadata=data.get("metadata", {}),
        )

        # Send through the service pipeline (async)
        try:
            notification_id = LOOP.run_until_complete(SERVICE.send(request))
        except ValidationError as e:
            self._send_error(400, str(e))
            return
        except RateLimitExceededError as e:
            self._send_error(429, str(e))
            return
        except DuplicateNotificationError as e:
            self._send_error(409, str(e))
            return
        except QueueFullError as e:
            self._send_error(503, str(e))
            return
        except TemplateRenderError as e:
            self._send_error(400, str(e))
            return

        self._send_json(201, {
            "notification_id": notification_id,
            "status": "queued",
            "message": "Notification enqueued successfully",
        })

    def _handle_get_notification(self, notification_id: str) -> None:
        """GET /notifications/<id> - Get delivery status and event history."""
        tracker = SERVICE.tracker
        status = tracker.get_current_status(notification_id)
        if status is None:
            self._send_error(404, f"Notification '{notification_id}' not found")
            return

        history = tracker.get_history(notification_id)
        events = [
            {
                "status": event.status.value,
                "channel": event.channel.value,
                "timestamp": event.timestamp.isoformat(),
                "details": event.details,
            }
            for event in history
        ]

        self._send_json(200, {
            "notification_id": notification_id,
            "current_status": status.value,
            "events": events,
        })

    def _handle_get_analytics(self) -> None:
        """GET /analytics - Aggregate analytics."""
        tracker = SERVICE.tracker
        counts = tracker.get_aggregate_counts()

        # Per-channel stats
        channel_stats = {}
        for channel in Channel:
            stats = tracker.get_channel_stats(channel)
            channel_stats[channel.value] = stats

        self._send_json(200, {
            "aggregate": counts,
            "per_channel": channel_stats,
        })

    def _handle_get_settings(self, user_id: str) -> None:
        """GET /settings/<user_id> - Get user preferences."""
        settings = SERVICE.settings
        preferences = settings.get_all_preferences(user_id)
        result = {channel.value: opted_in for channel, opted_in in preferences.items()}
        self._send_json(200, {
            "user_id": user_id,
            "preferences": result,
        })

    def _handle_update_settings(self, user_id: str) -> None:
        """PUT /settings/<user_id> - Update user preferences."""
        data = self._read_json_body()
        if data is None:
            self._send_error(400, "Invalid JSON body")
            return

        preferences = data.get("preferences", {})
        settings = SERVICE.settings

        # Parse and apply preferences
        updates: dict[Channel, bool] = {}
        for channel_str, opted_in in preferences.items():
            channel = CHANNEL_MAP.get(channel_str)
            if channel is None:
                self._send_error(
                    400,
                    f"Invalid channel '{channel_str}'. "
                    f"Valid channels: {list(CHANNEL_MAP.keys())}",
                )
                return
            if not isinstance(opted_in, bool):
                self._send_error(400, f"Preference for '{channel_str}' must be a boolean")
                return
            updates[channel] = opted_in

        settings.bulk_update(user_id, updates)

        # Return updated preferences
        all_prefs = settings.get_all_preferences(user_id)
        result = {channel.value: opted_in for channel, opted_in in all_prefs.items()}
        self._send_json(200, {
            "user_id": user_id,
            "preferences": result,
            "message": "Preferences updated successfully",
        })

    def _handle_create_template(self) -> None:
        """POST /templates - Create a template."""
        data = self._read_json_body()
        if data is None:
            self._send_error(400, "Invalid JSON body")
            return

        template_id = data.get("template_id")
        title = data.get("title", "")
        body = data.get("body", "")

        if not template_id:
            self._send_error(400, "Missing required field: template_id")
            return

        template = NotificationTemplate(
            template_id=template_id,
            title=title,
            body=body,
        )
        SERVICE.templates.register(template)

        self._send_json(201, {
            "template_id": template_id,
            "title": title,
            "body": body,
            "required_keys": sorted(template.get_required_keys()),
            "message": "Template created successfully",
        })

    def _handle_list_templates(self) -> None:
        """GET /templates - List all templates."""
        registry = SERVICE.templates
        template_ids = registry.list_ids()
        templates = []
        for tid in template_ids:
            tmpl = registry.get(tid)
            if tmpl:
                templates.append({
                    "template_id": tmpl.template_id,
                    "title": tmpl.title_template,
                    "body": tmpl.body_template,
                    "required_keys": sorted(tmpl.get_required_keys()),
                })

        self._send_json(200, {"templates": templates})

    def log_message(self, format: str, *args: Any) -> None:
        """Override to provide cleaner log output."""
        sys.stderr.write(
            f"[{self.log_date_time_string()}] {format % args}\n"
        )


def run_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Start the HTTP demo server.

    Args:
        host: The host address to bind to.
        port: The port number to listen on.
    """
    server = HTTPServer((host, port), NotificationHandler)
    print(f"Notification System Demo Server")
    print(f"================================")
    print(f"Listening on http://{host}:{port}")
    print()
    print("Available endpoints:")
    print(f"  POST http://{host}:{port}/notifications")
    print(f"  GET  http://{host}:{port}/notifications/<id>")
    print(f"  GET  http://{host}:{port}/analytics")
    print(f"  GET  http://{host}:{port}/settings/<user_id>")
    print(f"  PUT  http://{host}:{port}/settings/<user_id>")
    print(f"  POST http://{host}:{port}/templates")
    print(f"  GET  http://{host}:{port}/templates")
    print()
    print("Demo users: user_1, user_2")
    print("Demo template: welcome (params: name, app_name)")
    print()
    print("Press Ctrl+C to stop the server.")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.shutdown()
        LOOP.close()
        print("Server stopped.")


if __name__ == "__main__":
    port = 8000
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    run_server(port=port)
