"""Bounded independent executors sharing durable SQLite job claims."""

import threading
import time
from urllib.error import HTTPError
from .common import http_json, integer
from .provider import MockHTTPProvider


def work_once(config, stop, *, provider=None):
    if stop.is_set():
        return False
    url, token = config["api_url"], config["internal_token"]
    provider = provider or MockHTTPProvider(config)
    deadline, job = time.monotonic() + 20, None

    def call(path, body=None):
        return http_json(url + path, token, body, deadline=deadline, cancel=stop)

    try:
        job = call("/internal/claim", {})["job"]
        if not job:
            return False
        if job["kind"] == "execute":
            op = call("/internal/operation/" + job["payload"]["operation"])
            if op["status"] in ("SUCCEEDED", "FAILED"):
                call("/internal/finish", {"job": job})
                return True
            try:
                receipt = provider.query(op["id"], deadline=deadline, cancel=stop)
            except HTTPError as exc:
                missing = exc.code == 404
                exc.close()
                if not missing:
                    raise
                receipt = provider.create(op, deadline=deadline, cancel=stop)
            if not isinstance(receipt, dict) or any(
                receipt.get(field) != op.get(field)
                for field in ("id", "merchant", "kind", "parent", "amount", "currency")
            ):
                raise ValueError("PSP receipt does not match claimed operation")
            applied = call("/internal/apply", {"receipt": receipt})
            if applied.get("review"):
                raise ValueError("PSP receipt requires manual review")
            call(
                "/internal/finish",
                {"job": job, "pending": receipt["status"] == "PENDING"},
            )
        elif job["kind"] == "notify":
            result = http_json(
                config["receiver_url"] + "/events",
                config["event_token"],
                job["payload"],
                deadline=deadline,
                cancel=stop,
            )
            if (
                not isinstance(result, dict)
                or result.get("received") != job["payload"]["event_id"]
            ):
                raise ValueError("event consumer acknowledgement mismatch")
            call("/internal/finish", {"job": job})
        elif job["kind"] == "reconcile":
            statement = http_json(
                config["provider_url"] + "/statements",
                config["provider_token"],
                deadline=deadline,
                cancel=stop,
            )
            call(
                "/internal/reconcile",
                {"run": job["payload"]["run"], "statement": statement, "job": job},
            )
            call("/internal/finish", {"job": job})
        else:
            raise ValueError("unknown durable job kind")
    except Exception as exc:
        if job and not stop.is_set() and time.monotonic() < deadline:
            try:
                call("/internal/finish", {"job": job, "error": str(exc)[:1000]})
            except Exception:
                pass  # The original durable lease remains reclaimable.
    return job is not None


def run(config, stop=None, concurrency=4):
    integer(concurrency, "worker concurrency", 1, 16)
    stop = stop or threading.Event()

    def loop():
        provider = MockHTTPProvider(config)
        while not stop.is_set():
            busy = work_once(config, stop, provider=provider)
            stop.wait(0.01 if busy else 0.05)

    threads = []
    try:
        for index in range(concurrency):
            thread = threading.Thread(
                target=loop, name="payment-executor-" + str(index)
            )
            thread.start()
            threads.append(thread)
        while not stop.wait(0.1):
            pass
    finally:
        stop.set()
        for thread in threads:
            thread.join()
