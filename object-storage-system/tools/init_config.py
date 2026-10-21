import argparse
import json
from pathlib import Path
import secrets


def main():
    parser=argparse.ArgumentParser(description="Create private six-process localhost object-storage configuration")
    parser.add_argument("--output",default=".runtime/secrets.json")
    parser.add_argument("--base-port",type=int,default=9850)
    args=parser.parse_args()
    if not 1024<=args.base_port<=65530:
        parser.error("base port out of range")
    config={k:secrets.token_hex(24) for k in ("internal_token","admin_token","continuation_secret")}
    config.update(users={u:secrets.token_hex(24) for u in ("alice","bob")},
                  metadata_urls=[f"http://127.0.0.1:{args.base_port+i}" for i in (1,2)],
                  data_urls=[f"http://127.0.0.1:{args.base_port+i}" for i in (3,4,5)],write_quorum=2)
    output=Path(args.output)
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open("x") as handle:
        json.dump(config,handle,indent=2)
    print(f"Private configuration created: {output}")


if __name__=="__main__":
    main()
