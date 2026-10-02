"""Run after installing the package, or with PYTHONPATH=src."""
import asyncio

from chat_system import ChatService, SQLiteMessageStore


async def main():
    store = SQLiteMessageStore()
    chat = ChatService(store)
    try:
        alice = chat.connect(chat.auth.issue("alice"), "phone")
        bob_token = chat.auth.issue("bob")
        direct = chat.create_direct(alice, "bob")
        sent = await chat.send(alice, direct.channel_id, "Hello Bob!", "alice-1")
        alice.queue.get_nowait()  # Consume Alice's own message event before Bob's reply.
        print("Offline push:", len(chat.notifier.notifications))
        bob_phone = chat.connect(bob_token, "phone")
        bob_laptop = chat.connect(bob_token, "laptop")
        print("Phone sync:", chat.sync(bob_phone))
        print("Laptop sync:", chat.sync(bob_laptop))
        reply = await chat.send(bob_phone, direct.channel_id, "Hello Alice!", "bob-1")
        print("Alice live event:", alice.queue.get_nowait())
        print("Bob next page:", chat.sync(bob_laptop, after=sent.message_id))
        assert reply.message_id > sent.message_id
        group = chat.create_group(alice, ["bob", "carol"])
        await chat.send(alice, group.channel_id, "Welcome to the group", "alice-2")
        print("Group members:", group.members)
    finally:
        store.close()


if __name__ == "__main__":
    asyncio.run(main())
