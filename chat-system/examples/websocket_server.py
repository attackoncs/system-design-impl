"""Local demo with file-backed messages and externally provisioned demo tokens."""
import asyncio
import os

from chat_system import ChatService, SQLiteMessageStore
from chat_system.transport import WebSocketChatServer


async def main():
    store = SQLiteMessageStore(os.environ.get("CHAT_DB", "chat-demo.sqlite3"))
    service = ChatService(store)
    # Demo-only provisioning. These credentials are printed locally, not served over HTTP.
    for user in ("alice", "bob", "carol"):
        print(f"{user} token: {service.auth.issue(user)}")
    try:
        async with WebSocketChatServer(service).running():
            print("Listening on ws://127.0.0.1:8765; Ctrl+C to stop")
            await asyncio.Future()
    finally:
        store.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
