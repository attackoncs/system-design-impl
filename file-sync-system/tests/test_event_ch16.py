from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident

import pytest
from test_sync import drive as drive
from test_sync import upload


@pytest.mark.parametrize("wake", ["publish", "close"])
def test_metadata_reads_leave_idle_poll_asleep(drive, monkeypatch, wake):
    drive.device(drive.token, "waiting")
    condition = drive.events.condition
    wait, notify = condition.wait, condition.notify_all
    waiting, broadcasts = Event(), []
    main_thread = get_ident()

    def observe_wait(timeout=None):
        if timeout is not None:
            waiting.set()
            # Isolate local wakeups from the periodic cross-process SQL poll.
            return wait(5)
        return wait()

    def observe_notify():
        if get_ident() == main_thread:
            broadcasts.append(True)
        notify()

    monkeypatch.setattr(condition, "wait", observe_wait)
    monkeypatch.setattr(condition, "notify_all", observe_notify)
    with ThreadPoolExecutor(1) as pool:
        poll = pool.submit(drive.changes, drive.token, "waiting", 0, 25)
        assert waiting.wait(2)
        drive.list_files(drive.token, drive.space)
        read_broadcasts = len(broadcasts)
        if wake == "publish":
            upload(drive, drive.token, drive.space)
        else:
            drive.close()
        batch = poll.result(2)

    assert read_broadcasts == 0
    assert broadcasts
    assert bool(batch["events"]) == (wake == "publish")
