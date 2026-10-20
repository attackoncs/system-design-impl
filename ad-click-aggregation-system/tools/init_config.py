import argparse
import json
from pathlib import Path
import secrets


def main():
    parser = argparse.ArgumentParser(description="Create private localhost configuration; refuses overwrite")
    parser.add_argument("--partitions", type=int, default=4)
    parser.add_argument("--base-port", type=int, default=9820)
    parser.add_argument("--output", default=".runtime/secrets.json")
    args = parser.parse_args()
    if not 1 <= args.partitions <= 64 or not 1024 <= args.base_port < 65534 - args.partitions:
        parser.error("invalid partition count or port range")
    config = {r + "_token": secrets.token_hex(24) for r in ("write", "read", "admin", "internal")}
    config.update(partitions=args.partitions, ingress_url=f"http://127.0.0.1:{args.base_port}",
                  worker_urls=[f"http://127.0.0.1:{args.base_port+2+p}" for p in range(args.partitions)],
                  lateness=120, max_events=1_000_000)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as handle:
        json.dump(config, handle, indent=2)
    print(f"Private configuration created: {output}; API port: {args.base_port+1}")


if __name__ == "__main__":
    main()
