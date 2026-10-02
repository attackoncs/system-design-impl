import argparse
import json
import os
from pathlib import Path

from .http import server
from .media import FFmpegMedia
from .service import VideoService
from .worker import Worker
from .remote_storage import S3Objects


def main():
    parser = argparse.ArgumentParser(description="Local video API and durable media workers")
    parser.add_argument("--root", default=".runtime/video")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("token").add_argument("owner")
    commands.add_parser("cleanup")
    api = commands.add_parser("api")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8080)
    worker = commands.add_parser("worker")
    worker.add_argument("--name", default="worker")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--media-timeout", type=float, default=600)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    objects = (S3Objects(root / "objects", os.environ["VIDEO_S3_BUCKET"],
                endpoint=os.environ.get("VIDEO_S3_ENDPOINT"),
                prefix=os.environ.get("VIDEO_S3_PREFIX", "video/"),
                region=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
               if os.environ.get("VIDEO_S3_BUCKET") else root / "objects")
    service = VideoService(os.environ.get("VIDEO_DATABASE_DSN") or root / "metadata.sqlite3", objects)
    try:
        if args.command == "token":
            print(service.issue(args.owner), flush=True)
        elif args.command == "cleanup":
            print(json.dumps(service.cleanup()))
        elif args.command == "worker":
            print("Video worker ready", flush=True)
            media = FFmpegMedia(timeout=args.media_timeout)
            Worker(service, media, args.name, lease=args.media_timeout + 60).run(args.once)
        else:
            http = server(service, args.host, args.port)
            print("Video API ready", flush=True)
            try:
                http.serve_forever()
            finally:
                http.server_close()
    finally:
        service.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
