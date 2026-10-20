import argparse
import json
from pathlib import Path
import secrets


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--queue-config",required=True,help="existing chapter 20 private configuration")
    parser.add_argument("--queue-url",default="http://127.0.0.1:9710")
    parser.add_argument("--output",default=".runtime/secrets.json")
    args=parser.parse_args()
    queue=json.loads(Path(args.queue_config).read_text(encoding="utf-8"))
    config={role+"_token":secrets.token_urlsafe(32) for role in ["admin","read","push","exporter"]}
    config.update(queue_url=args.queue_url,queue_token=queue["client_token"],destinations={},topic="metrics",max_series=10000,per_metric=1000,spool_bytes=16*1024*1024)
    target=Path(args.output)
    target.parent.mkdir(parents=True,exist_ok=True)
    with target.open("x",encoding="utf-8") as output:
        json.dump(config,output,indent=2)
    print("private monitoring configuration created")


if __name__=="__main__":
    main()
