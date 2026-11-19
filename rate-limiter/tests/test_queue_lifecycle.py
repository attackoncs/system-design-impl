"""Shutdown regressions for callback reentrancy and pending FIFO work."""

import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest
from rate_limiter.queue import LeakyBucketQueue


@pytest.mark.parametrize("operation", ["submit", "close"])
def test_close_allows_cancel_callbacks_to_reenter_queue(operation):
    # A child process makes the original mutex deadlock a bounded test failure.
    script = textwrap.dedent(
        """
        import sys
        from rate_limiter.queue import LeakyBucketQueue

        worker = LeakyBucketQueue(capacity=1, rate=1)
        pending = worker.submit(lambda: None)
        completed = []

        def on_cancel(future):
            assert future.cancelled()
            if sys.argv[1] == "close":
                worker.close()
            else:
                try:
                    worker.submit(lambda: None)
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("closed queue accepted work")
            completed.append(True)

        pending.add_done_callback(on_cancel)
        worker.close()
        assert pending.cancelled()
        assert completed == [True]
        """
    )
    source = Path(__file__).resolve().parents[1] / "src"
    environment = {**os.environ, "PYTHONPATH": str(source)}
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, operation],
            env=environment,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"close deadlocked when a cancellation callback called {operation}")
    assert result.returncode == 0, result.stderr


def test_close_cancels_pending_callback_after_pacing_wait(monkeypatch):
    worker = LeakyBucketQueue(capacity=1, rate=1)
    after_wait = threading.Event()
    resume = threading.Event()
    ran = threading.Event()
    pending = worker.submit(ran.set)
    original_wait = worker._stop.wait

    def pause_after_wait(timeout=None):
        stopped = original_wait(timeout)
        if not stopped:
            after_wait.set()
            assert resume.wait(2)
        return stopped

    # Freeze the worker after its pacing wait, before it starts pending work.
    monkeypatch.setattr(worker._stop, "wait", pause_after_wait)
    closer = threading.Thread(target=worker.close, daemon=True)
    worker.start()
    try:
        assert after_wait.wait(2)
        assert not pending.running()
        closer.start()
        assert original_wait(2)
    finally:
        resume.set()
        worker._stop.set()
        if closer.ident is not None:
            closer.join(2)
        worker._thread.join(2)

    assert not closer.is_alive()
    assert not worker._thread.is_alive()
    assert pending.cancelled()
    assert not ran.is_set()


def test_concurrent_close_waits_for_pending_cancellation(monkeypatch):
    worker = LeakyBucketQueue(capacity=1, rate=1)
    pending = worker.submit(lambda: None)
    cancelling = threading.Event()
    resume = threading.Event()
    second_returned = threading.Event()
    original_cancel = pending.cancel

    def pause_cancel():
        cancelling.set()
        assert resume.wait(2)
        return original_cancel()

    monkeypatch.setattr(pending, "cancel", pause_cancel)
    first = threading.Thread(target=worker.close, daemon=True)
    second = threading.Thread(
        target=lambda: (worker.close(), second_returned.set()), daemon=True
    )
    first.start()
    try:
        assert cancelling.wait(2)
        second.start()
        assert not second_returned.wait(0.1)
    finally:
        resume.set()
        first.join(2)
        if second.ident is not None:
            second.join(2)
    assert pending.cancelled()
    assert second_returned.is_set()
