"""Four independent processes, private configuration and owned cleanup only."""
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from stock_exchange.common import Error,request


def configuration(ports=range(9890,9894)):
    urls=["http://127.0.0.1:"+str(port) for port in ports]
    return {"gateway":urls[0],"engines":{"primary":urls[1],"backup":urls[2]},"market":urls[3],
            "admin_token":secrets.token_hex(24),"internal_token":secrets.token_hex(24),"owners":{key:secrets.token_hex(24) for key in ("alice","bob","carol")},
            "feed_retention":8,"simulation_timestamp":int(datetime(2026,10,21,14,tzinfo=timezone.utc).timestamp()),
            "settings":{"symbols":["AAPL","MSFT"],"daily_limit":1_000_000,"session":{"weekdays":[0,1,2,3,4],"start_minute":570,"end_minute":960},
                        "accounts":{key:{"cash":1_000_000,"positions":{"AAPL":1000,"MSFT":1000}} for key in ("alice","bob","carol")}}}


def wait(predicate,timeout=20):
    end=time.monotonic()+timeout
    last=None
    while time.monotonic()<end:
        try:
            last=predicate()
            if last:
                return last
        except (Error,OSError,ValueError):
            pass
        time.sleep(.03)
    raise AssertionError("condition did not become true; last="+repr(last))


class Lab:
    def __enter__(self):
        (ROOT/".runtime"/"tmp").mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix="exchange-",dir=ROOT/".runtime"/"tmp")
        self.root=Path(self.temp.name)
        sockets=[]
        for _ in range(4):
            sock=socket.socket()
            sock.bind(("127.0.0.1",0))
            sockets.append(sock)
        self.config=configuration([sock.getsockname()[1] for sock in sockets])
        for sock in sockets:
            sock.close()
        self.path=self.root/"private-config.json"
        self.path.write_text(json.dumps(self.config))
        self.processes,self.logs={},{}
        try:
            for role in ("gateway","primary","backup","market"):
                self.start(role)
            wait(lambda:self.raw("primary")["seq"]>=1 and self.raw("backup")["seq"]>=1)
        except BaseException:
            self.__exit__(None,None,None)
            raise
        return self

    def url(self,role):
        return self.config["engines"].get(role,self.config.get(role))

    def start(self,role):
        env={**os.environ,"PYTHONPATH":str(ROOT/"src"),"TEMP":str(ROOT/".runtime"/"tmp"),"TMP":str(ROOT/".runtime"/"tmp")}
        log=open(self.root/(role+".log"),"ab")
        self.logs[role]=log
        self.processes[role]=subprocess.Popen([sys.executable,"-m","stock_exchange.cli",role,"--config",str(self.path),"--root",str(self.root)],env=env,stdout=log,stderr=log)
        wait(lambda:request(self.url(role)+"/health",self.config["internal_token"],timeout=.5))

    def stop(self,role):
        proc=self.processes.pop(role,None)
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=8)
        if role in self.logs:
            self.logs.pop(role).close()

    def __exit__(self,*args):
        for role in list(self.processes):
            self.stop(role)
        self.temp.cleanup()

    def call(self,path,body=None,role="gateway",auth="alice"):
        token=self.config["owners"].get(auth,self.config.get(auth+"_token"))
        return request(self.url(role)+path,token,body)

    def order(self,id,side="BUY",price=100,quantity=10,owner="alice",symbol="AAPL"):
        body={"request_id":"request_"+id,"order_id":id,"symbol":symbol,"side":side,"price":price,"quantity":quantity}
        return self.call("/v1/orders",body,auth=owner)

    def result(self,id,owner="alice"):
        def ready():
            value=self.call("/v1/requests/request_"+id,auth=owner)
            return value if "accepted" in value else None
        return wait(ready)

    def raw(self,node="primary"):
        return self.call("/internal/raw",role=node,auth="internal")

    def caught_up(self):
        tail=self.call("/admin/status",auth="admin")["seq"]
        wait(lambda:self.raw("primary")["seq"]==tail and self.raw("backup")["seq"]==tail)
        return tail

    def market_ready(self):
        tail=self.call("/admin/status",auth="admin")["seq"]
        return wait(lambda:self.call("/admin/status",role="market",auth="admin")["seq"]==tail)
