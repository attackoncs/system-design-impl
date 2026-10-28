import argparse
import json
from pathlib import Path
from lab import configuration

parser = argparse.ArgumentParser(
    description="Generate a loopback-only synthetic wallet configuration"
)
parser.add_argument("--output", default=".runtime/private-config.json")
args = parser.parse_args()
path = Path(args.output)
path.parent.mkdir(parents=True, exist_ok=True)
with path.open("x") as stream:
    json.dump(configuration(), stream, indent=2)
print("Private config created:", path.resolve())
