import argparse
import json
from pathlib import Path
import secrets


def main():
    parser = argparse.ArgumentParser(
        description="Create local-only email configuration; no external delivery"
    )
    parser.add_argument("--output", default=".runtime/secrets.json")
    parser.add_argument("--base-port", type=int, default=9840)
    parser.add_argument(
        "--test-scanner",
        action="store_true",
        help="enable TEST marker rules, not real anti-virus; default quarantines all mail",
    )
    args = parser.parse_args()
    if not 1024 <= args.base_port <= 65528:
        parser.error("base port out of range")
    config = {
        r + "_token": secrets.token_hex(24) for r in ("admin", "internal", "smtp")
    }
    config.update(
        users={u: secrets.token_hex(24) for u in ("alice", "bob", "eve")},
        primary_urls=[f"http://127.0.0.1:{args.base_port + i}" for i in (2, 3)],
        replica_urls=[f"http://127.0.0.1:{args.base_port + i}" for i in (4, 5)],
        object_url=f"http://127.0.0.1:{args.base_port + 1}",
        smtp_port=args.base_port + 7,
        scanner="test-markers" if args.test_scanner else "quarantine-all",
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as handle:
        json.dump(config, handle, indent=2)
    print(f"Private local-only configuration created: {output}")


if __name__ == "__main__":
    main()
