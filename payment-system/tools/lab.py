"""Four real processes on private loopback ports; only owned processes are stopped."""
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
from payment_system.common import http_json


def configuration(ports=(9870,9871,9872)):
    value={key+"_url":"http://127.0.0.1:"+str(port) for key,port in zip(("api","provider","receiver"),ports)}
    value.update({key+"_token":secrets.token_hex(24) for key in ("admin","internal","provider","event")})
    value["webhook_secret"]=secrets.token_hex(24)
    value["merchants"]={key:secrets.token_hex(24) for key in ("seller","other")}
    return value


def wait(predicate,timeout=15):
    end=time.monotonic()+timeout
    last=None
    while time.monotonic()<end:
        try:
            last=predicate()
            if last:
                return last
        except (OSError,ValueError):
            pass
        time.sleep(.04)
    raise AssertionError("condition did not become true; last="+repr(last))


class Lab:
    def __enter__(self):
        (ROOT/".runtime"/"tmp").mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix="payments-",dir=ROOT/".runtime"/"tmp")
        self.root=Path(self.temp.name)
        sockets=[]
        for _ in range(3):
            sock=socket.socket()
            sock.bind(("127.0.0.1",0))
            sockets.append(sock)
        ports=[sock.getsockname()[1] for sock in sockets]
        self.config=configuration(ports)
        for sock in sockets:
            sock.close()
        self.path=self.root/"private-config.json"
        self.path.write_text(json.dumps(self.config))
        self.processes={}
        self.logs={}
        try:
            for role in ("api","provider","receiver","worker"):
                self.start(role)
        except BaseException:
            self.__exit__(None,None,None)
            raise
        return self

    def start(self,role):
        env={**os.environ,"PYTHONPATH":str(ROOT/"src"),"TEMP":str(ROOT/".runtime"/"tmp"),"TMP":str(ROOT/".runtime"/"tmp")}
        log=open(self.root/(role+".log"),"ab")
        self.logs[role]=log
        args=[sys.executable,"-m","payment_system.cli",role,"--config",str(self.path),"--root",str(self.root)]
        if role!="worker":
            args.extend(["--port",self.config[role+"_url"].rsplit(":",1)[1]])
        self.processes[role]=subprocess.Popen(args,env=env,stdout=log,stderr=log)
        if role!="worker":
            wait(lambda:http_json(self.config[role+"_url"]+"/health"))

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

    def request(self,path,body=None,role="api",auth="seller"):
        token=self.config["merchants"].get(auth,self.config.get(auth+"_token"))
        return http_json(self.config[role+"_url"]+path,token,body)

    def control(self,**flags):
        return self.request("/admin/control",{"lose_response":False,"webhooks_down":False,"query_down":False,"statement_delta":0,**flags},role="provider",auth="admin")

    def operation(self,id):
        return self.request("/v1/operations/"+id)

    def await_state(self,id,state="SUCCEEDED"):
        def probe():
            op=self.operation(id)
            return op if op["status"]==state else None
        return wait(probe)

    def pay(self,amount=10000,key="pay"):
        op=self.request("/v1/payments",{"request_key":key,"order_id":"order_"+key,"amount":amount,"currency":"USD"})
        ready=wait(lambda:self.operation(op["id"])["checkout"])
        http_json(ready,body={"choice":"test-success"})
        return self.await_state(op["id"])

    def settled(self):
        return wait(lambda:self.request("/admin/status",auth="admin")["pending_jobs"]==0)

    def reconcile(self):
        run=self.request("/admin/reconcile",{},auth="admin")["run"]
        def done():
            result=self.request("/admin/reconciliations/"+run,auth="admin")
            return result if result["status"]!="pending" else None
        return wait(done)
