"""Exercise the wired service, workers and durable queue rather than mocks."""

import asyncio
import datetime
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

from notification_system import (
    Channel,
    ContactInfo,
    DeliveryResult,
    DuplicateNotificationError,
    MessageQueue,
    NotificationConfig,
    NotificationRequest,
    NotificationService,
    NotificationStatus,
    NotificationTask,
    NotificationTemplate,
    QueueFullError,
    RateLimitConfig,
    RetryConfig,
    SQLiteMessageQueue,
    SQLiteNotificationLog,
)


def task(name="job"):
    request = NotificationRequest("user", Channel.EMAIL, body=name)
    return NotificationTask(name, request, Channel.EMAIL)


def service(queue=None, **kwargs):
    config = NotificationConfig(
        retry=RetryConfig(base_delay=0.03, max_delay=0.03, jitter_factor=0)
    )
    result = NotificationService(config=config, queue=queue, **kwargs)
    result.contacts.set(ContactInfo("user", email="user@example.com"))
    return result


async def wait_for_status(service, identifier, status):
    async def wait():
        while service.tracker.get_current_status(identifier) != status:
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), 2)


@pytest.mark.asyncio
async def test_template_and_endpoints_are_snapshotted_before_delivery():
    app = service()
    app.contacts.add_device_token("user", "ios-one", Channel.IOS_PUSH)
    app.contacts.add_device_token("user", "ios-two", Channel.IOS_PUSH)
    app.contacts.add_device_token("user", "android", Channel.ANDROID_PUSH)
    app.templates.register(NotificationTemplate("welcome", "Hi {name}", "Hello {name}"))
    params = {"name": "Alice"}
    identifier = await app.send(
        NotificationRequest(
            "user", Channel.IOS_PUSH, template_id="welcome", template_params=params
        )
    )
    params["name"] = "Changed"
    app.templates.register(NotificationTemplate("welcome", "new", "new"))
    app.contacts.remove_device_token("user", "ios-one")
    seen = []

    class Provider:
        async def deliver(self, item):
            seen.append((item.endpoint, item.request.title, item.request.body))
            return DeliveryResult(True)

    pool = app.create_worker_pool({Channel.IOS_PUSH: Provider()})
    await pool.start()
    try:
        await wait_for_status(app, identifier, NotificationStatus.SENT)
        assert seen == [
            ("ios-one", "Hi Alice", "Hello Alice"),
            ("ios-two", "Hi Alice", "Hello Alice"),
        ]
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_queue_rejection_does_not_consume_rate_limit_or_dedup():
    queue = MessageQueue(1)
    config = NotificationConfig(
        global_rate_limit=RateLimitConfig(2, 60), per_channel_rate_limits={}
    )
    app = NotificationService(config=config, queue=queue)
    app.contacts.set(ContactInfo("user", email="user@example.com"))
    await app.send(NotificationRequest("user", Channel.EMAIL, body="first"))
    with pytest.raises(QueueFullError):
        await app.send(NotificationRequest("user", Channel.EMAIL, body="second"))
    await queue.dequeue(Channel.EMAIL)
    assert await app.send(NotificationRequest("user", Channel.EMAIL, body="second"))


@pytest.mark.asyncio
async def test_retry_yields_worker_to_other_jobs_and_provider_exception_isolated():
    app = service()
    calls = []

    class Provider:
        async def deliver(self, item):
            calls.append(item.request.body)
            if item.request.body == "first" and calls.count("first") == 1:
                raise ConnectionError("temporary outage")
            return DeliveryResult(True)

    pool = app.create_worker_pool({Channel.EMAIL: Provider()})
    pool._workers_per_channel = 1
    first = await app.send(NotificationRequest("user", Channel.EMAIL, body="first"))
    second = await app.send(NotificationRequest("user", Channel.EMAIL, body="second"))
    await pool.start()
    try:
        await wait_for_status(app, first, NotificationStatus.SENT)
        assert calls == ["first", "second", "first"]
        assert app.tracker.get_current_status(second) == NotificationStatus.SENT
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_claim_reserves_capacity_and_retry_cannot_overflow():
    queue = MessageQueue(1)
    await queue.enqueue(task())
    item = await queue.claim(Channel.EMAIL)
    with pytest.raises(QueueFullError):
        await queue.enqueue(task("other"))
    await queue.reschedule(item, 0.01)
    assert queue.depth(Channel.EMAIL) == 1
    retried = await queue.claim(Channel.EMAIL)
    await queue.ack(retried)
    await queue.enqueue(task("other"))


@pytest.mark.asyncio
async def test_temporary_claim_error_does_not_kill_the_worker():
    class FlakyQueue(MessageQueue):
        failed = False

        async def claim(self, channel):
            if not self.failed:
                self.failed = True
                raise OSError("temporary queue outage")
            return await super().claim(channel)

    class Provider:
        async def deliver(self, item):
            return DeliveryResult(True)

    app = service(FlakyQueue())
    app.config.workers_per_channel = 1
    identifier = await app.send(
        NotificationRequest("user", Channel.EMAIL, body="retry claim")
    )
    pool = app.create_worker_pool({Channel.EMAIL: Provider()})
    await pool.start()
    try:
        await wait_for_status(app, identifier, NotificationStatus.SENT)
        assert pool.is_running
    finally:
        await pool.stop()


@pytest.mark.parametrize("completed", ["ack", "reschedule"])
@pytest.mark.asyncio
async def test_released_memory_claim_cannot_be_resurrected_by_compensation(completed):
    queue = MessageQueue()
    await queue.enqueue(task())
    claimed = await queue.claim(Channel.EMAIL)
    await getattr(queue, completed)(claimed)
    with pytest.raises(RuntimeError, match="already released"):
        await queue.reschedule(claimed)
    assert queue.depth(Channel.EMAIL) == (1 if completed == "reschedule" else 0)


@pytest.mark.asyncio
async def test_event_id_dedup_survives_ack_restart_and_concurrent_acceptance(tmp_path):
    path = tmp_path / "events.sqlite3"
    first = service(SQLiteMessageQueue(path))
    second = service(SQLiteMessageQueue(path))
    request = NotificationRequest(
        "user", Channel.EMAIL, body="event", event_id="invoice-123"
    )
    results = await asyncio.gather(
        first.send(request), second.send(request), return_exceptions=True
    )
    identifiers = [item for item in results if isinstance(item, str)]
    duplicates = [
        item for item in results if isinstance(item, DuplicateNotificationError)
    ]
    assert len(identifiers) == len(duplicates) == 1
    original = identifiers[0]
    assert original in str(duplicates[0])
    claimed = await first.queue.claim(Channel.EMAIL)
    await first.queue.ack(claimed, NotificationStatus.SENT)
    reopened = service(SQLiteMessageQueue(path))
    # First acceptance wins even when a replay supplies a different body.
    with pytest.raises(DuplicateNotificationError) as caught:
        await reopened.send(
            NotificationRequest(
                "user", Channel.EMAIL, body="changed", event_id="invoice-123"
            )
        )
    assert original in str(caught.value)
    assert reopened.queue.depth(Channel.EMAIL) == 0
    # Identical content attached to a distinct business event is allowed.
    assert await reopened.send(
        NotificationRequest("user", Channel.EMAIL, body="event", event_id="invoice-124")
    )


@pytest.mark.asyncio
async def test_durable_lease_recovers_process_exit_and_fences_stale_ack(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    queue = SQLiteMessageQueue(path, lease_seconds=0.08)
    await queue.enqueue(task())
    script = """
import asyncio, sys
from notification_system import SQLiteMessageQueue, Channel
async def main():
    queue = SQLiteMessageQueue(sys.argv[1], lease_seconds=0.08)
    item = await queue.claim(Channel.EMAIL)
    print(item.receipt, flush=True)
asyncio.run(main())
"""
    process = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", script, str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    receipt = process.stdout.strip()
    stale = task()
    stale.receipt = receipt
    await asyncio.sleep(0.1)
    recovered = await queue.claim(Channel.EMAIL)
    assert recovered.notification_id == "job" and recovered.receipt != receipt
    with pytest.raises(RuntimeError, match="lease was lost"):
        await queue.ack(stale, NotificationStatus.SENT)
    await queue.ack(recovered, NotificationStatus.SENT)
    audit = SQLiteNotificationLog(path).query(notification_id="job")
    assert [row.status for row in audit] == [
        NotificationStatus.QUEUED,
        NotificationStatus.SENT,
    ]
    assert queue.depth(Channel.EMAIL) == 0 and queue.in_flight(Channel.EMAIL) == 0


@pytest.mark.asyncio
async def test_cancellation_after_claim_completion_returns_memory_reservation():
    class RacingQueue(MessageQueue):
        async def claim(self, channel):
            claimed = await super().claim(channel)
            # Run cancellation before the awaiting worker's continuation.
            asyncio.get_running_loop().call_soon(pool._tasks[0].cancel)
            return claimed

    class Provider:
        async def deliver(self, item):
            pytest.fail("Cancellation should happen before provider invocation")

    queue = RacingQueue()
    app = service(queue)
    app.config.workers_per_channel = 1
    await app.send(NotificationRequest("user", Channel.EMAIL, body="race"))
    pool = app.create_worker_pool({Channel.EMAIL: Provider()})
    await pool.start()
    try:
        with pytest.raises(asyncio.CancelledError):
            await pool._tasks[0]
        assert queue.depth(Channel.EMAIL) == 1
        assert queue.in_flight(Channel.EMAIL) == 0
    finally:
        await pool.stop(graceful=False)


@pytest.mark.asyncio
async def test_two_sqlite_instances_cannot_claim_same_live_job(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    first, second = SQLiteMessageQueue(path), SQLiteMessageQueue(path)
    await first.enqueue(task("one"))
    await first.enqueue(task("two"))
    left, right = await asyncio.gather(
        first.claim(Channel.EMAIL), second.claim(Channel.EMAIL)
    )
    assert left.notification_id != right.notification_id
    await first.ack(left)
    await second.ack(right)


@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.asyncio
async def test_cancelling_pool_retains_accepted_work(tmp_path, durable):
    queue = SQLiteMessageQueue(tmp_path / "jobs.sqlite3") if durable else MessageQueue()
    app = service(queue)
    entered = asyncio.Event()

    class Blocked:
        async def deliver(self, item):
            entered.set()
            await asyncio.Event().wait()

    identifier = await app.send(
        NotificationRequest("user", Channel.EMAIL, body="cancel")
    )
    pool = app.create_worker_pool({Channel.EMAIL: Blocked()})
    await pool.start()
    await asyncio.wait_for(entered.wait(), 1)
    await pool.stop(graceful=False)
    assert queue.depth(Channel.EMAIL) == 1 and queue.in_flight(Channel.EMAIL) == 0

    class Success:
        async def deliver(self, item):
            return DeliveryResult(True)

    pool = app.create_worker_pool({Channel.EMAIL: Success()})
    await pool.start()
    try:
        await wait_for_status(app, identifier, NotificationStatus.SENT)
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_live_workers_renew_leases_during_slow_delivery(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    app = service(SQLiteMessageQueue(path, lease_seconds=0.09))
    entered = asyncio.Event()

    class Slow:
        async def deliver(self, item):
            entered.set()
            await asyncio.sleep(0.3)
            return DeliveryResult(True)

    identifier = await app.send(NotificationRequest("user", Channel.EMAIL, body="slow"))
    pool = app.create_worker_pool({Channel.EMAIL: Slow()})
    await pool.start()
    try:
        await entered.wait()
        rival = SQLiteMessageQueue(path, lease_seconds=0.09)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(rival.claim(Channel.EMAIL), 0.2)
        await wait_for_status(app, identifier, NotificationStatus.SENT)
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_opt_out_after_enqueue_suppresses_provider_delivery():
    app = service()

    class Provider:
        async def deliver(self, item):
            pytest.fail("An opted-out recipient must not be sent to the provider")

    identifier = await app.send(
        NotificationRequest("user", Channel.EMAIL, body="promo")
    )
    app.settings.opt_out("user", Channel.EMAIL)
    pool = app.create_worker_pool({Channel.EMAIL: Provider()})
    await pool.start()
    try:
        await wait_for_status(app, identifier, NotificationStatus.UNSUBSCRIBED)
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_partial_device_failure_retries_only_unsent_endpoints():
    app = service()
    app.contacts.set(ContactInfo("user", device_tokens=["one", "two"]))
    calls = []

    class Provider:
        async def deliver(self, item):
            calls.append(item.endpoint)
            return DeliveryResult(item.endpoint != "two" or calls.count("two") > 1)

    identifier = await app.send(
        NotificationRequest("user", Channel.IOS_PUSH, body="push")
    )
    pool = app.create_worker_pool({Channel.IOS_PUSH: Provider()})
    await pool.start()
    try:
        await wait_for_status(app, identifier, NotificationStatus.SENT)
        assert calls == ["one", "two", "two"]
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_scheduled_jobs_survive_reopen_without_blocking_immediate_jobs(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    app = service(SQLiteMessageQueue(path))
    later = await app.send(
        NotificationRequest("user", Channel.EMAIL, body="later"),
        scheduled_at=datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(seconds=0.1),
    )
    now = await app.send(NotificationRequest("user", Channel.EMAIL, body="now"))
    reopened = SQLiteMessageQueue(path)
    first = await reopened.claim(Channel.EMAIL)
    assert first.notification_id == now
    await reopened.ack(first)
    second = await reopened.claim(Channel.EMAIL)
    assert second.notification_id == later
    await reopened.ack(second)


@pytest.mark.asyncio
async def test_mixed_device_failures_preserve_permanent_rejection_and_retry_transient(
    tmp_path,
):
    app = service(SQLiteMessageQueue(tmp_path / "fanout.sqlite3"))
    app.contacts.set(ContactInfo("user", device_tokens=["invalid", "temporary"]))
    calls = []

    class Provider:
        async def deliver(self, item):
            calls.append(item.endpoint)
            if item.endpoint == "invalid":
                return DeliveryResult(False, error="Invalid token", retryable=False)
            return DeliveryResult(
                calls.count("temporary") > 1, error="Temporary outage"
            )

    identifier = await app.send(
        NotificationRequest("user", Channel.IOS_PUSH, body="mixed")
    )
    pool = app.create_worker_pool({Channel.IOS_PUSH: Provider()})
    await pool.start()
    try:
        await wait_for_status(app, identifier, NotificationStatus.FAILED)
        assert calls == ["invalid", "temporary", "temporary"]
        audit = SQLiteNotificationLog(tmp_path / "fanout.sqlite3").query(
            notification_id=identifier
        )
        assert audit[-1].status == NotificationStatus.FAILED
        assert app.queue.depth(Channel.IOS_PUSH) == 0
    finally:
        await pool.stop()


def test_http_api_authentication_and_live_delivery(tmp_path):
    import http.client
    import threading
    import time

    path = Path(__file__).resolve().parents[1] / "examples" / "demo_server.py"
    spec = importlib.util.spec_from_file_location("notification_demo", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = module.Application(tmp_path / "demo.sqlite3")
    server = module.create_server(app, "test-secret", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method, path, body=None, auth=True):
        import json

        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_port, timeout=2
        )
        try:
            connection.request(
                method,
                path,
                json.dumps(body) if body is not None else None,
                {"Authorization": "Bearer test-secret"} if auth else {},
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    try:
        assert request("GET", "/analytics", auth=False)[0] == 401
        assert request("POST", "/notifications", ["bad"])[0] == 400
        status, body = request(
            "POST",
            "/notifications",
            {"recipient_id": "user_1", "channel": "email", "body": "HTTP delivery"},
        )
        assert status == 202
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status, result = request("GET", "/notifications/" + body["notification_id"])
            if any(row["status"] == "sent" for row in result["audit"]):
                break
            time.sleep(0.01)
        else:
            pytest.fail("HTTP-accepted request never reached the live provider")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        app.close()
