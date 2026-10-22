"""Actual seven-process transfer and in-transit recovery demonstration."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
from lab import Lab

with Lab() as lab:
    lab.stop("worker")
    lab.transfer("cross",700)
    for _ in range(4):
        lab.step()  # Prepare both, replicate COMMIT, confirm source only.
    intermediate=lab.audit()
    assert intermediate["matches"] and intermediate["in_transit"]==700
    lab.stop("coordinator")
    lab.start("coordinator")
    lab.start("worker")
    lab.done("cross")
    lab.transfer("local",300,True)
    lab.done("local")
    result={"processes":7,"synthetic_only":True,"during_partial_commit":intermediate,
            "balances":[lab.balance(account) for account in lab.config["accounts"]],"final_audit":lab.audit()}
    assert result["final_audit"]["matches"]
    print(json.dumps(result,indent=2))
