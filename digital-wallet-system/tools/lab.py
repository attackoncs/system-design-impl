"""Owned seven-process loopback laboratory, using independent database files."""
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
from digital_wallet.common import Error,request,shard_for


def configuration(ports=range(9880,9886)):
    ports=list(ports)
    urls=["http://127.0.0.1:"+str(value) for value in ports]
    accounts={}
    for prefix,index,owner in (("alice",0,"alice"),("peer",0,"alice"),("bob",1,"bob")):
        name=next(prefix+"_"+str(i) for i in range(100) if shard_for(prefix+"_"+str(i),2)==index)
        accounts[name]={"owner":owner,"opening":10000}
    return {"coordinator":urls[0],"shards":urls[1:3],"replicas":urls[3:6],"epoch":1,"leader":"coordinator",
            "admin_token":secrets.token_hex(24),"internal_token":secrets.token_hex(24),"owners":{key:secrets.token_hex(24) for key in ("alice","bob")},"accounts":accounts}


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
        time.sleep(.04)
    raise AssertionError("condition timed out; last="+repr(last))


class Lab:
    def __enter__(self):
        (ROOT/".runtime"/"tmp").mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix="wallet-",dir=ROOT/".runtime"/"tmp")
        self.root=Path(self.temp.name)
        sockets=[]
        for _ in range(6):
            sock=socket.socket()
            sock.bind(("127.0.0.1",0))
            sockets.append(sock)
        self.config=configuration([sock.getsockname()[1] for sock in sockets])
        for sock in sockets:
            sock.close()
        self.path=self.root/"private-config.json"
        self.write_config()
        self.processes,self.logs={},{}
        self.source=next(key for key in self.config["accounts"] if key.startswith("alice_"))
        self.peer=next(key for key in self.config["accounts"] if key.startswith("peer_"))
        self.destination=next(key for key in self.config["accounts"] if key.startswith("bob_"))
        try:
            for role in ("replica0","replica1","replica2","shard0","shard1","coordinator","worker"):
                self.start(role)
        except BaseException:
            self.__exit__(None,None,None)
            raise
        return self

    def write_config(self):
        self.path.write_text(json.dumps(self.config))

    def url(self,role):
        if role.startswith("shard"):
            return self.config["shards"][int(role[-1])]
        if role.startswith("replica"):
            return self.config["replicas"][int(role[-1])]
        return self.config["coordinator"]

    def start(self,role):
        env={**os.environ,"PYTHONPATH":str(ROOT/"src"),"TEMP":str(ROOT/".runtime"/"tmp"),"TMP":str(ROOT/".runtime"/"tmp")}
        log=open(self.root/(role+".log"),"ab")
        self.logs[role]=log
        base="shard" if role.startswith("shard") else "replica" if role.startswith("replica") else role
        args=[sys.executable,"-m","digital_wallet.cli",base,"--config",str(self.path),"--root",str(self.root)]
        if base in ("shard","replica"):
            args.extend(["--index",role[-1]])
        self.processes[role]=subprocess.Popen(args,env=env,stdout=log,stderr=log)
        if role!="worker":
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

    def call(self,path,body=None,role="coordinator",auth="alice"):
        token=self.config["owners"].get(auth,self.config.get(auth+"_token"))
        return request(self.url(role)+path,token,body)

    def body(self,id="transfer",amount=100,local=False):
        return {"transaction_id":id,"from_account":self.source,"to_account":self.peer if local else self.destination,"amount":amount,"currency":"USD"}

    def transfer(self,id="transfer",amount=100,local=False):
        return self.call("/v1/wallet/balance_transfer",self.body(id,amount,local))

    def done(self,id="transfer",status="SUCCEEDED"):
        def probe():
            value=self.call("/v1/transfers/"+id)
            return value if value["status"]==status else None
        return wait(probe)

    def step(self):
        return self.call("/internal/step",{},auth="internal")

    def balance(self,account=None,at=None):
        account=account or self.source
        owner=self.config["accounts"][account]["owner"]
        return self.call("/v1/accounts/"+account+("?at="+str(at) if at else ""),auth=owner)

    def audit(self):
        return self.call("/admin/audit",auth="admin")

    def control(self,index,**flags):
        return self.call("/admin/control",{"lose_reply":False,"reject_prepare":False,**flags},role="shard"+str(index),auth="admin")
