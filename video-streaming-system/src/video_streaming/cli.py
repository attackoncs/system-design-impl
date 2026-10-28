"""Command-line entry points for video APIs and independent workers."""

import argparse
import json
import os
import pathlib

import video_streaming.http
import video_streaming.media
import video_streaming.remote_storage
import video_streaming.service
import video_streaming.worker


def _parser():
    parser = argparse.ArgumentParser(
        description="Local video API and durable media workers"
    )
    parser.add_argument("--root", default=".runtime/video")
    parser.add_argument(
        "--pipeline", help="JSON profiles/thumbnail/preprocess configuration"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("token").add_argument("owner")
    commands.add_parser("cleanup")
    api = commands.add_parser("api")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8080)
    api.add_argument("--concurrency", type=int, default=32)
    api.add_argument("--request-timeout", type=float, default=30)
    worker = commands.add_parser("worker")
    worker.add_argument("--name", default="worker")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--concurrency", type=int, default=1)
    worker.add_argument(
        "--kinds", nargs="+", choices=sorted(video_streaming.worker.KINDS)
    )
    worker.add_argument("--media-timeout", type=float, default=600)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    pipeline = None
    if args.pipeline:
        with open(args.pipeline, encoding="utf-8") as stream:
            pipeline = json.load(stream)
    root = pathlib.Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    objects = (
        video_streaming.remote_storage.S3Objects(
            root / "objects",
            os.environ["VIDEO_S3_BUCKET"],
            endpoint=os.environ.get("VIDEO_S3_ENDPOINT"),
            prefix=os.environ.get("VIDEO_S3_PREFIX", "video/"),
            region=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        )
        if os.environ.get("VIDEO_S3_BUCKET")
        else root / "objects"
    )
    service = video_streaming.service.VideoService(
        os.environ.get("VIDEO_DATABASE_DSN") or root / "metadata.sqlite3",
        objects,
        pipeline=pipeline,
    )
    try:
        if args.command == "token":
            print(service.issue(args.owner), flush=True)
        elif args.command == "cleanup":
            print(json.dumps(service.cleanup()))
        elif args.command == "worker":
            print("Video worker ready", flush=True)
            media = video_streaming.media.FFmpegMedia(timeout=args.media_timeout)
            video_streaming.worker.Worker(
                service,
                media,
                args.name,
                lease=args.media_timeout + 60,
                concurrency=args.concurrency,
                kinds=args.kinds,
            ).run(args.once)
        else:
            http = video_streaming.http.server(
                service,
                args.host,
                args.port,
                concurrency=args.concurrency,
                request_timeout=args.request_timeout,
            )
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
