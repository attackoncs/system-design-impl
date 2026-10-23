import argparse
import json
from pathlib import Path

from .model import availability, estimate, sweep
from .quantities import arithmetic_context, parse_bytes, text


def main():
    parser = argparse.ArgumentParser(
        description="Chapter 3: explicit-unit capacity estimates"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    scenario = commands.add_parser("estimate")
    scenario.add_argument("--scenario", type=Path)
    scenario.add_argument("--sweep", help="e.g. peak_factor=2,5,10")
    commands.add_parser("units").add_argument("quantity")
    commands.add_parser("availability").add_argument("percent")
    args = parser.parse_args()
    try:
        if args.command == "units":
            value = parse_bytes(args.quantity)
            with arithmetic_context():
                result = {
                    "bytes": text(value),
                    "decimal_MB": text(value / 1_000_000),
                    "binary_MiB": text(value / 1_048_576),
                }
        elif args.command == "availability":
            result = availability(args.percent)
        else:
            inputs = (
                json.loads(args.scenario.read_text(), parse_float=str)
                if args.scenario
                else {}
            )
            if args.sweep:
                field, values = args.sweep.split("=", 1)
                result = sweep(inputs, field, values.split(","))
            else:
                result = estimate(inputs)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
