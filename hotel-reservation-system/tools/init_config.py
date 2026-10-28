import argparse
import json
from pathlib import Path
import secrets


def main():
    parser = argparse.ArgumentParser(
        description="Create private local simulated-payment configuration"
    )
    parser.add_argument("--output", default=".runtime/secrets.json")
    parser.add_argument("--base-port", type=int, default=9830)
    args = parser.parse_args()
    if not 1024 <= args.base_port <= 65532:
        parser.error("base port out of range")
    config = {
        r + "_token": secrets.token_hex(24) for r in ("admin", "internal", "payment")
    }
    config.update(
        users={u: secrets.token_hex(24) for u in ("alice", "bob")},
        shard_urls=[f"http://127.0.0.1:{args.base_port + i}" for i in (2, 3)],
        payment_url=f"http://127.0.0.1:{args.base_port + 1}",
        hold_seconds=120,
        lease_seconds=2,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as handle:
        json.dump(config, handle, indent=2)
    print(f"Private simulated-payment configuration created at {output}")


if __name__ == "__main__":
    main()
