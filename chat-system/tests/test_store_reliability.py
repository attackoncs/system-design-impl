"""Storage invariants under independent connections, threads, and restart."""

import concurrent.futures
import dataclasses
import json
import sqlite3
import threading

import pytest

from chat_system import ChatError, SQLiteMessageStore


@pytest.fixture
def stores(tmp_path):
    path = tmp_path / "chat.sqlite3"
    instances = [SQLiteMessageStore(path) for _ in range(2)]
    yield instances
    for store in instances:
        store.close()


def _race(functions):
    """Start operations on independent connections at the same barrier."""
    barrier = threading.Barrier(len(functions))

    def execute(function):
        barrier.wait(timeout=10)
        return function()

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(functions)) as pool:
        futures = [pool.submit(execute, function) for function in functions]
        return [future.result(timeout=15) for future in futures]


def test_direct_creation_is_unique_across_connections(stores):
    first, second = stores
    channels = _race(
        [
            lambda: first.create_channel("alice", ["bob"], "direct", 2),
            lambda: second.create_channel("bob", ["alice"], "direct", 2),
        ]
    )
    assert channels[0] == channels[1]
    assert first.db.execute("SELECT count(*) FROM channels").fetchone()[0] == 1
    assert first.db.execute("SELECT count(*) FROM direct_pairs").fetchone()[0] == 1


def test_group_capacity_check_and_insert_are_atomic(stores):
    first, second = stores
    group = first.create_channel("owner", ["member"], "group", 3)

    def add(store, user):
        try:
            return store.add_member(group.channel_id, "owner", user, 3)
        except ChatError as error:
            return str(error)

    results = _race(
        [lambda: add(first, "candidate-1"), lambda: add(second, "candidate-2")]
    )
    assert sum(result == "group is full" for result in results) == 1
    assert len(first.get_channel(group.channel_id).members) == 3


def test_identical_retries_commit_exactly_one_message_and_inbox(stores):
    first, second = stores
    channel = first.create_channel("alice", ["bob"], "direct", 2)
    results = _race(
        [
            lambda: first.append(channel.channel_id, "alice", "hello", "retry"),
            lambda: second.append(channel.channel_id, "alice", "hello", "retry"),
        ]
    )
    assert results[0][0] == results[1][0]
    assert sorted(result[1] for result in results) == [False, True]
    assert first.sync("alice") == second.sync("bob") == [results[0][0]]
    assert first.db.execute("SELECT count(*) FROM inbox").fetchone()[0] == 2


def test_conflicting_retry_cannot_overwrite_the_winning_message(stores):
    first, second = stores
    channel = first.create_channel("alice", ["bob"], "direct", 2)

    def append(store, content):
        try:
            return store.append(channel.channel_id, "alice", content, "same-key")[0]
        except ChatError:
            return None

    results = _race([lambda: append(first, "one"), lambda: append(second, "two")])
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert first.sync("alice") == second.sync("bob") == winners


def test_in_memory_connection_can_be_offloaded_and_shared_by_threads():
    store = SQLiteMessageStore()
    try:
        channel = store.create_channel("alice", ["bob"], "direct", 2)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [
                pool.submit(store.append, channel.channel_id, "alice", str(i), str(i))
                for i in range(30)
            ]
            messages = [future.result(timeout=15)[0] for future in futures]
        assert len({message.message_id for message in messages}) == 30
        assert store.sync("bob") == sorted(
            messages, key=lambda message: message.message_id
        )
    finally:
        store.close()


def test_partial_fanout_failure_rolls_back_retry_key_and_every_inbox(stores):
    first, second = stores
    channel = first.create_channel("alice", ["bob", "charlie"], "group", 3)
    first.db.execute(
        "CREATE TRIGGER fail_second_recipient BEFORE INSERT ON inbox "
        "WHEN NEW.user='bob' BEGIN SELECT RAISE(ABORT, 'inbox unavailable'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="inbox unavailable"):
        first.append(channel.channel_id, "alice", "hello", "key")
    assert second.sync("alice") == second.sync("bob") == []
    assert second.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    first.db.execute("DROP TRIGGER fail_second_recipient")
    message, created, _ = second.append(channel.channel_id, "alice", "hello", "key")
    assert created
    assert all(first.sync(user) == [message] for user in channel.members)


def test_member_join_and_fanout_use_one_membership_snapshot(stores):
    first, second = stores
    channel = first.create_channel("alice", ["bob"], "group", 3)
    insert_started = threading.Event()
    release_insert = threading.Event()

    def pause_insert():
        insert_started.set()
        assert release_insert.wait(timeout=10)
        return 1

    first.db.create_function("pause_insert", 0, pause_insert)
    first.db.execute(
        "CREATE TRIGGER pause_message BEFORE INSERT ON messages "
        "BEGIN SELECT pause_insert(); END"
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        append = pool.submit(first.append, channel.channel_id, "alice", "before", "1")
        assert insert_started.wait(timeout=10)
        join = pool.submit(second.add_member, channel.channel_id, "alice", "charlie", 3)
        release_insert.set()
        before, _, recipients = append.result(timeout=15)
        join.result(timeout=15)
    assert recipients == ("alice", "bob")
    assert second.sync("charlie") == []
    first.db.execute("DROP TRIGGER pause_message")
    after, _, _ = second.append(channel.channel_id, "alice", "after", "2")
    assert first.history(channel.channel_id, "charlie") == [after]
    assert first.sync("bob") == [before, after]


def test_restart_preserves_retry_identity_and_increasing_message_ids(tmp_path):
    path = tmp_path / "chat.sqlite3"
    first = SQLiteMessageStore(path)
    channel = first.create_channel("alice", ["bob"], "direct", 2)
    message, _, _ = first.append(channel.channel_id, "alice", "hello", "retry")
    first.close()
    reopened = SQLiteMessageStore(path)
    try:
        assert reopened.create_channel("bob", ["alice"], "direct", 2) == channel
        retried, created, _ = reopened.append(
            channel.channel_id, "alice", "hello", "retry"
        )
        assert retried == message and not created
        following, _, _ = reopened.append(channel.channel_id, "bob", "next", "next")
        assert following.message_id > message.message_id
        assert reopened.sync("bob") == [message, following]
    finally:
        reopened.close()


def test_legacy_direct_channels_gain_a_canonical_pair_index(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE channels (id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL);"
            "CREATE TABLE members (channel TEXT, user TEXT, PRIMARY KEY(channel,user));"
            "INSERT INTO channels VALUES ('original','alice','direct');"
            "INSERT INTO channels VALUES ('legacy-duplicate','bob','direct');"
            "INSERT INTO members VALUES ('original','alice'),('original','bob'),"
            "('legacy-duplicate','alice'),('legacy-duplicate','bob');"
        )
    store = SQLiteMessageStore(path)
    try:
        assert (
            store.create_channel("bob", ["alice"], "direct", 2).channel_id == "original"
        )
        assert store.get_channel("legacy-duplicate").members == ("alice", "bob")
    finally:
        store.close()


@pytest.mark.parametrize("after,limit", [(False, 1), (0, True), (2**63, 10), (1.0, 10)])
def test_pagination_rejects_bool_and_unbindable_cursors(stores, after, limit):
    store = stores[0]
    channel = store.create_channel("alice", ["bob"], "direct", 2)
    with pytest.raises(ChatError, match="cursor or page size"):
        store.sync("bob", after, limit)
    with pytest.raises(ChatError, match="cursor or page size"):
        store.history(channel.channel_id, "bob", after, limit)


@pytest.mark.parametrize("limit", [True, 1, 101, 3.0])
def test_membership_limits_reject_invalid_types_and_bounds(stores, limit):
    with pytest.raises(ChatError, match="group limit"):
        stores[0].create_channel("alice", ["bob"], "group", limit)


def test_byte_bounded_pages_advance_without_losing_messages(stores):
    store = stores[0]
    channel = store.create_channel("alice", ["bob"], "direct", 2)
    messages = [
        store.append(channel.channel_id, "alice", "\u4f60" * 500, str(i))[0]
        for i in range(3)
    ]
    budget = (
        max(
            len(
                json.dumps(dataclasses.asdict(message), ensure_ascii=False).encode(
                    "utf-8"
                )
            )
            for message in messages
        )
        + 4
    )
    first = store.sync_bounded("bob", limit=1000, byte_limit=budget)
    assert first == messages[:1]
    assert (
        store.history_bounded(
            channel.channel_id, "bob", after=first[-1].message_id, byte_limit=budget
        )
        == messages[1:2]
    )
    assert (
        store.sync_bounded("bob", after=messages[1].message_id, byte_limit=budget)
        == messages[2:]
    )
    assert store.sync("bob") == messages


@pytest.mark.parametrize("budget", [0, -1, True, 1.5, 10])
def test_byte_limit_cannot_silently_return_an_empty_nonterminal_page(stores, budget):
    store = stores[0]
    channel = store.create_channel("alice", ["bob"], "direct", 2)
    store.append(channel.channel_id, "alice", "hello", "key")
    with pytest.raises(ChatError, match="byte limit"):
        store.sync_bounded("bob", byte_limit=budget)


def test_exact_lookup_requires_membership_in_the_original_recipient_snapshot(stores):
    first, second = stores
    channel = first.create_channel("alice", ["bob"], "group", 3)
    before, _, _ = first.append(channel.channel_id, "alice", "before join", "1")
    second.add_member(channel.channel_id, "alice", "charlie", 3)
    after, _, _ = first.append(channel.channel_id, "bob", "after join", "2")
    assert second.get_message(before.message_id, "alice") == before
    assert second.get_message(before.message_id, "bob") == before
    assert first.get_message(after.message_id, "charlie") == after
    for user in ("outsider", "charlie"):
        with pytest.raises(ChatError, match="^message not found$"):
            second.get_message(before.message_id, user)
    with pytest.raises(ChatError, match="^message not found$"):
        second.get_message(after.message_id + 100, "alice")


@pytest.mark.parametrize("message_id", [True, False, 0, -1, 1.0, "1", None, 2**63])
def test_exact_lookup_rejects_invalid_message_identifiers(stores, message_id):
    with pytest.raises(ChatError, match="invalid message ID"):
        stores[0].get_message(message_id, "alice")
