import argparse
import json
from pathlib import Path
import secrets


def main():
    parser=argparse.ArgumentParser(description="Generate private local leaderboard config; Redis must be provisioned separately")
    parser.add_argument("--output",default=".runtime/secrets.json")
    parser.add_argument("--base-port",type=int,default=9860)
    args=parser.parse_args()
    if not 1024<=args.base_port<=65529:
        parser.error("base port out of range")
    config={r+"_token":secrets.token_hex(24) for r in ("admin","read","game","internal")}
    config.update(redis_password=secrets.token_hex(24),namespace="leaderboard_local",late_grace=172800,
                  authority_url=f"http://127.0.0.1:{args.base_port}",
                  projection_urls=[f"http://127.0.0.1:{args.base_port+i}" for i in (1,2)],
                  redis_nodes=[[{"host":"127.0.0.1","port":args.base_port+3+2*p+i} for i in (0,1)] for p in (0,1)])
    output=Path(args.output)
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open("x") as handle:
        json.dump(config,handle,indent=2)
    print(f"Private configuration created: {output}; provision its Redis primary/replica pairs before serving")


if __name__=="__main__":
    main()
