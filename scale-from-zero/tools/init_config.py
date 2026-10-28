import argparse
import json
from pathlib import Path
from lab import configuration

parser = argparse.ArgumentParser(description="Generate a local scaling-lab topology")
parser.add_argument(
    "--stage", choices=["single", "balanced", "sharded"], default="sharded"
)
parser.add_argument("--output", default=".runtime/private-config.json")
parser.add_argument("--web-nodes", type=int, default=2)
args = parser.parse_args()
path = Path(args.output)
path.parent.mkdir(parents=True, exist_ok=True)
with path.open("x") as stream:
    json.dump(configuration(args.stage, web_nodes=args.web_nodes), stream, indent=2)
print("Private configuration created:", path.resolve())
