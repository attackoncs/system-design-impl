#!/usr/bin/env python3
"""Run the chapter's feed endpoints with live asynchronous fanout workers.

Set NEWS_FEED_TOKEN for the author; optionally set a distinct
NEWS_FEED_VIEWER_TOKEN to read the author's posts as a follower. SQLite mode
persists posts, profiles and follows. Demo credentials are reloaded at startup.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news_feed_system.http import Application, create_server
from news_feed_system.models import UserProfile


async def bootstrap(api, author_token: str, viewer_token: str | None) -> None:
    """Load demo identity/relationship data before accepting requests."""
    if not author_token.strip():
        raise ValueError("NEWS_FEED_TOKEN must be nonempty")
    if viewer_token is not None and (
        not viewer_token.strip() or viewer_token == author_token
    ):
        raise ValueError("NEWS_FEED_VIEWER_TOKEN must be nonempty and distinct")
    api.auth.register_token(author_token, "demo_author")
    if viewer_token is not None:
        api.auth.register_token(viewer_token, "demo_viewer")
    api.users.put(UserProfile("demo_author", "Author"))
    api.users.put(UserProfile("demo_viewer", "Viewer"))
    await api.graph.add_follow("demo_viewer", "demo_author")


def run_server(host: str = "127.0.0.1", port: int = 8000, database=None) -> None:
    author_token = os.environ.get("NEWS_FEED_TOKEN")
    if not author_token:
        raise ValueError("Set NEWS_FEED_TOKEN before starting the server")
    with Application(database_path=database) as app:
        app.call(
            bootstrap(app.api, author_token, os.environ.get("NEWS_FEED_VIEWER_TOKEN"))
        )
        with create_server(app, host, port) as server:
            print(
                f"News feed demo listening on http://{host}:{server.server_port}/v1/me/feed"
            )
            print("Use Bearer authentication; the viewer follows demo_author.")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--database", help="Optional SQLite file for restart recovery")
    args = parser.parse_args()
    run_server(args.host, args.port, args.database)
