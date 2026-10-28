"""Bounded sender attempts and cancellable replication loops."""

import smtplib
import socket
import threading
import time

from .common import http_json
from .objects import Objects
from .transport import Deadline


def _smtp_send(config, job, raw, deadline, stop):
    smtp = smtplib.SMTP(timeout=min(5, max(0.001, deadline - time.monotonic())))

    def interrupt():
        connection = smtp.sock
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()

    try:
        # No MX lookup or external SMTP destination exists in this adapter.
        smtp.connect("127.0.0.1", config["smtp_port"])
        with Deadline(deadline, interrupt, stop):
            if stop.is_set() or time.monotonic() >= deadline:
                raise TimeoutError("sender attempt cancelled or expired")
            smtp.login("local-worker", config["smtp_token"])
            refused = smtp.sendmail(job["sender"], job["recipients"], raw)
            if refused:
                raise RuntimeError("some local recipients refused")
    finally:
        # A 250 DATA acknowledgement is sufficient. QUIT must not add another
        # unbudgeted round trip to an otherwise completed delivery attempt.
        smtp.close()


def run_sender(config, shard, stop=None):
    """Drain each accepted attempt within a shared twenty-second I/O budget.

    On shutdown an interrupted attempt retains its durable lease; the next
    worker can reclaim it, reusing the original raw MIME and delivery ID.
    """
    stop = threading.Event() if stop is None else stop
    url, token = config["primary_urls"][shard], config["internal_token"]
    while not stop.is_set():
        job = None
        deadline = time.monotonic() + 20
        try:
            job = http_json(
                url + "/internal/claim", token, {}, deadline=deadline, cancel=stop
            )["job"]
            if not job:
                stop.wait(0.05)
                continue
            raw = Objects(config).get(job["raw"], deadline=deadline, cancel=stop)
            _smtp_send(config, job, raw, deadline, stop)
            http_json(
                url + "/internal/settle",
                token,
                {"job": job},
                deadline=deadline,
                cancel=stop,
            )
        except Exception as exc:
            if job and not stop.is_set() and time.monotonic() < deadline:
                try:
                    http_json(
                        url + "/internal/settle",
                        token,
                        {"job": job, "error": str(exc)},
                        deadline=deadline,
                        cancel=stop,
                    )
                except Exception:
                    pass
            stop.wait(0.1)


def replicate(config, store, stop, state):
    url = config["primary_urls"][store.shard]
    while not stop.is_set():
        try:
            after = store.status()["position"]
            batch = http_json(
                url + f"/internal/changes?after={after}",
                config["internal_token"],
                deadline=time.monotonic() + 5,
                cancel=stop,
            )
            store.apply(batch)
            state.update(error=None, last_poll=time.time())
            stop.wait(0.02 if batch["changes"] else 0.1)
        except Exception as exc:
            state["error"] = str(exc)
            stop.wait(0.2)
