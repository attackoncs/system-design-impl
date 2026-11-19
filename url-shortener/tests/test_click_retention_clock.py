"""Click retention uses elapsed time while preserving real UTC event time."""

import datetime
from types import SimpleNamespace

import pytest
import url_shortener.shortener as module


@pytest.mark.parametrize("wall_delta,elapsed,retained", [(-100, 11, 0), (10000, 1, 1)])
def test_wall_clock_changes_cannot_change_retention(
    monkeypatch, wall_delta, elapsed, retained
):
    wall = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    current = [wall, 0.0]
    monkeypatch.setattr(
        module,
        "datetime",
        SimpleNamespace(
            datetime=SimpleNamespace(now=lambda tz: current[0]),
            timezone=datetime.timezone,
            timedelta=datetime.timedelta,
        ),
    )
    monkeypatch.setattr(module, "monotonic", lambda: current[1], raising=False)
    service = module.URLShortener(
        click_retention_max_age=datetime.timedelta(seconds=10)
    )
    code = service.shorten("https://example.com").rsplit("/", 1)[1]
    service.resolve(code)
    current[:] = [wall + datetime.timedelta(seconds=wall_delta), elapsed]
    records = service.get_click_records(code)
    assert len(records) == retained
    if records:
        assert records[0].timestamp == wall
    assert service.get_click_count(code) == 1


def test_valid_large_retention_does_not_break_redirects():
    service = module.URLShortener(click_retention_max_age=datetime.timedelta.max)
    code = service.shorten("https://example.com").rsplit("/", 1)[1]
    assert service.resolve(code)[0] == "https://example.com"
    assert len(service.get_click_records(code)) == 1
