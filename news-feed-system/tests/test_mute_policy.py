"""Durable privacy policy regressions, including cold start and failed writes."""

import sqlite3

import pytest

from news_feed_system.fanout_service import FanoutService
from news_feed_system.models import Post
from news_feed_system.policy import InMemoryMuteStore, SQLiteMuteStore
from news_feed_system.post_cache import PostCache
from news_feed_system.queue import MessageQueue


@pytest.mark.parametrize("persistent", [False, True])
def test_mutes_are_idempotent_and_return_defensive_snapshots(tmp_path, persistent):
    store = (
        SQLiteMuteStore(str(tmp_path / "feed.sqlite3"))
        if persistent
        else InMemoryMuteStore()
    )
    store.add("reader", "author")
    store.add("reader", "author")
    snapshot = store.get_muted("reader")
    snapshot.clear()
    assert store.get_muted("reader") == {"author"}
    assert store.is_muted("reader", "author")
    store.remove("reader", "author")
    store.remove("reader", "author")
    assert store.get_muted("reader") == set()


def test_fanout_visibility_restores_mute_after_database_reopen(tmp_path):
    path = str(tmp_path / "feed.sqlite3")
    first = FanoutService(
        object(), PostCache(), MessageQueue(), mute_store=SQLiteMuteStore(path)
    )
    first.add_mute("reader", "author")
    post = Post(post_id="p", author_id="author", content="hidden")
    reopened = FanoutService(
        object(), PostCache(), MessageQueue(), mute_store=SQLiteMuteStore(path)
    )
    assert reopened.get_mute_list("reader") == {"author"}
    assert not reopened.can_view("reader", post)
    assert reopened._filter_followers(["reader", "other"], "author", None) == ["other"]
    reopened.remove_mute("reader", "author")
    assert not SQLiteMuteStore(path).is_muted("reader", "author")


def test_external_store_writes_require_explicit_snapshot_reload(tmp_path):
    path = str(tmp_path / "feed.sqlite3")
    first = SQLiteMuteStore(path)
    second = SQLiteMuteStore(path)
    first.add("reader", "author")
    assert not second.is_muted("reader", "author")
    second.reload()
    assert second.is_muted("reader", "author")
    first.remove("reader", "author")
    second.reload()
    assert not second.is_muted("reader", "author")


def test_failed_durable_mutation_does_not_publish_an_uncommitted_policy(
    tmp_path, monkeypatch
):
    store = SQLiteMuteStore(str(tmp_path / "feed.sqlite3"))
    store.add("reader", "author")

    def fail_connect():
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(store._database, "connect", fail_connect)
    with pytest.raises(sqlite3.OperationalError):
        store.remove("reader", "author")
    assert store.is_muted("reader", "author")
    with pytest.raises(sqlite3.OperationalError):
        store.add("reader", "other")
    assert not store.is_muted("reader", "other")
    # Reads keep serving the committed local snapshot without opening SQLite.
    assert store.get_muted("reader") == {"author"}
