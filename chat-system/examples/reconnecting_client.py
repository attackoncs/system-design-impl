"""Watch a user's durable inbox, rediscovering nodes after socket failure."""
import asyncio
import os

from chat_system.client import ReconnectingChatClient


async def main():
    token = os.environ.get("CHAT_TOKEN")
    if not token:
        raise ValueError("Set CHAT_TOKEN to a provisioned user token")
    client = ReconnectingChatClient(
        os.environ.get("CHAT_DISCOVERY_URL", "http://127.0.0.1:8080/discover"),
        token, os.environ.get("CHAT_DEVICE_ID", "demo-device"),
        region=os.environ.get("CHAT_REGION"),
        cursor_file=os.environ.get("CHAT_CURSOR_FILE", "chat-cursor.txt"))
    try:
        async for messages in client.watch():
            for message in messages:
                print(message["sender_id"], message["content"], flush=True)
            client.acknowledge(messages)
    finally:
        await client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
