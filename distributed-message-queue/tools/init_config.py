import argparse
import json
from pathlib import Path
import secrets


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",default=".runtime/secrets.json")
    parser.add_argument("--broker-port",type=int,default=9711)
    args=parser.parse_args()
    if not 1024<=args.broker_port<=65533:
        parser.error("broker-port must be 1024..65533")
    path=Path(args.output)
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("x",encoding="utf-8") as output:
        json.dump({"client_token":secrets.token_urlsafe(32),"cluster_token":secrets.token_urlsafe(32),"brokers":{f"b{i}":f"http://127.0.0.1:{args.broker_port+i}" for i in range(3)}},output,indent=2)
    print("private configuration created")


if __name__=="__main__":
    main()
