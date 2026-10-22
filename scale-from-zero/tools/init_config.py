import argparse
import json
from pathlib import Path
from lab import configuration

parser=argparse.ArgumentParser(description="Generate a local scaling-lab topology")
parser.add_argument("--stage",choices=["single","balanced","sharded"],default="sharded")
parser.add_argument("--output",default=".runtime/private-config.json")
args=parser.parse_args()
path=Path(args.output)
path.parent.mkdir(parents=True,exist_ok=True)
with path.open("x") as stream:
    json.dump(configuration(args.stage),stream,indent=2)
print("Private configuration created:",path.resolve())
