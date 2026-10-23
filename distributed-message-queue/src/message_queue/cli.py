import argparse
import json
from pathlib import Path
import threading

from .broker import Broker
from .coordinator import Coordinator
from .http import server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["broker", "coordinator"])
    parser.add_argument("--root", required=True)
    parser.add_argument(
        "--config",
        required=True,
        help="private JSON cluster/client tokens and broker ID/URL map",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--lease-seconds", type=float, default=10)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.lease_seconds <= 0:
        parser.error("lease must be positive")
    if args.role == "broker":
        service = Broker(args.root)
        token = config["cluster_token"]
    else:
        service = Coordinator(
            args.root, config["brokers"], config["cluster_token"], args.lease_seconds
        )
        token = config["client_token"]
    httpd = server(args.host, args.port, service, token)
    stop = threading.Event()

    def maintain():
        while not stop.wait(1):
            try:
                service.maintenance()
            except Exception:
                pass

    worker = None
    if args.role == "coordinator":
        worker = threading.Thread(target=maintain, daemon=True)
        worker.start()
    print(
        f"{args.role} listening on http://{args.host}:{httpd.server_port}", flush=True
    )
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
        if worker:
            worker.join(timeout=5)


if __name__ == "__main__":
    main()
