"""Durable restart protection survives rejected generation and cwd changes."""

import os
from types import SimpleNamespace

import pytest
from helpers import FakeClock
from unique_id import HighWaterMark, SnowflakeConfig, SnowflakeGenerator
from unique_id.exceptions import ClockMovedBackwardsError


@pytest.mark.parametrize("failure", ["store", "range"])
def test_failed_generation_after_restart_wait_keeps_previous_ceiling(
    tmp_path, monkeypatch, failure
):
    config = SnowflakeConfig()
    timestamp = config.epoch_ms + 100
    mark = HighWaterMark(tmp_path / "node.hw", window_ms=1)
    previous = SnowflakeGenerator(
        config, FakeClock(timestamp), high_water=mark
    ).generate()
    ceiling = mark.load()
    jumped = (
        ceiling + 1
        if failure == "store"
        else config.epoch_ms + config.max_timestamp + 1
    )
    ticks = iter([ceiling, jumped])
    clock = SimpleNamespace(current_millis=lambda: next(ticks, timestamp))
    generator = SnowflakeGenerator(
        config, clock, high_water=mark, overflow_timeout_seconds=0.01
    )
    store = mark.store
    if failure == "store":

        def fail_store(ceiling_ms):
            raise OSError("reservation unavailable")

        monkeypatch.setattr(mark, "store", fail_store)
    with pytest.raises(OSError if failure == "store" else ValueError):
        generator.generate()
    monkeypatch.setattr(mark, "store", store)
    # Returning to an old, already issued timestamp must still be rejected.
    with pytest.raises(ClockMovedBackwardsError):
        generator.generate()
    clock.current_millis = lambda: ceiling + 1
    assert generator.generate() > previous
    del generator
    restarted = SnowflakeGenerator(
        config, FakeClock(ceiling + 3), high_water=mark, overflow_timeout_seconds=0
    )
    assert restarted.generate() > previous


def test_relative_high_water_path_remains_bound_after_chdir(tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(first)
    mark = HighWaterMark("node.hw", window_ms=1)
    clock = FakeClock(SnowflakeConfig().epoch_ms + 100)
    generator = SnowflakeGenerator(clock=clock, high_water=mark)
    generator.generate()
    monkeypatch.chdir(second)
    clock.advance(10)
    previous = generator.generate()
    del generator
    monkeypatch.chdir(first)
    restarted = SnowflakeGenerator(
        clock=clock,
        high_water=HighWaterMark("node.hw", window_ms=1),
        overflow_timeout_seconds=0,
    )
    with pytest.raises(ClockMovedBackwardsError):
        restarted.generate()
    clock.advance(2)
    assert restarted.generate() > previous
    assert not (second / "node.hw").exists()


def test_one_durable_reservation_covers_all_ids_inside_window(tmp_path, monkeypatch):
    calls = []
    fsync = os.fsync

    def record_sync(fd):
        fsync(fd)
        calls.append(fd)

    monkeypatch.setattr(os, "fsync", record_sync)
    clock = FakeClock(SnowflakeConfig().epoch_ms + 100)
    mark = HighWaterMark(tmp_path / "node.hw", window_ms=10)
    generator = SnowflakeGenerator(clock=clock, high_water=mark)
    ids = [generator.generate()]
    initial_syncs = len(calls)
    assert initial_syncs > 0
    ids.extend(generator.generate_batch(100))
    clock.advance(10)
    ids.append(generator.generate())
    assert len(calls) == initial_syncs
    clock.advance(1)
    ids.append(generator.generate())
    assert len(calls) > initial_syncs
    assert len(set(ids)) == len(ids) and ids == sorted(ids)
    assert mark.load() == clock.current_millis() + 10
