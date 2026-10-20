import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from urllib.parse import urlencode
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from metrics_monitor.common import http_json


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        return sock.getsockname()[1]


class Lab:
    def __init__(self):
        self.root=ROOT/".runtime"/("lab-"+uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.processes={}
        self.logs=[]
        self.ports={role:port() for role in ["exporter","collector","api","receiver"]}
        self.urls={role:f"http://127.0.0.1:{value}" for role,value in self.ports.items()}
        spec=importlib.util.spec_from_file_location("chapter20_metrics_lab",ROOT.parent/"distributed-message-queue/tools/lab.py")
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.queue=module.Cluster(lease=2)
        self.config={role+"_token":secrets.token_hex(24) for role in ["admin","read","push","exporter"]}
        self.config.update(queue_url=self.queue.url,queue_token=self.queue.config["client_token"],destinations={"local":{"url":self.urls["receiver"]+"/notify"}})
        self.config_path=self.root/"secrets.json"
        self.config_path.write_text(json.dumps(self.config))
        self.targetfile=self.root/"targets.json"
        self.targetfile.write_text(json.dumps([{"instance":"test-exporter","url":self.urls["exporter"]+"/metrics","token":self.config["exporter_token"]}]))
        self.receiver_control=self.root/"receiver-control.json"
        self.receiver_control.write_text('{"fail_after_commit":false}')

    def spawn(self,name,args):
        output=(self.root/(name+".log")).open("ab")
        self.logs.append(output)
        env=dict(os.environ,PYTHONPATH=str(ROOT/"src"))
        process=subprocess.Popen([sys.executable,*args],env=env,stdout=output,stderr=output,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        self.processes[name]=process
        return process

    def start_role(self,role):
        root=self.root/("collector" if role=="collector" else "store")
        args=["-m","metrics_monitor.cli",role,"--root",str(root),"--config",str(self.config_path),"--interval","0.5"]
        if role in self.ports:
            args.extend(["--port",str(self.ports[role])])
        if role=="collector":
            args.extend(["--targets",str(self.targetfile)])
        self.spawn(role,args)
        if role in ("api","collector","exporter"):
            self.wait(lambda:http_json(self.urls[role]+"/health"))

    def start(self,storage=True,alerts=True):
        self.queue.start()
        self.spawn("receiver",[str(ROOT/"tools/receiver.py"),"--root",str(self.root/"receiver"),"--port",str(self.ports["receiver"]),"--control",str(self.receiver_control)])
        self.wait(lambda:http_json(self.urls["receiver"]+"/stats"))
        for role in ("exporter","collector","api"):
            self.start_role(role)
        if storage:
            self.start_role("storage")
        if alerts:
            self.start_role("alerts")
        return self

    def push(self,samples,id=None):
        return http_json(self.urls["collector"]+"/push","POST",{"samples":samples},self.config["push_token"],{"Idempotency-Key":id or uuid.uuid4().hex})

    def query(self,metric,**options):
        params={"metric":metric,"start":time.time()-300,"end":time.time()+1,**options}
        if isinstance(params.get("match"),dict):
            params["match"]=json.dumps(params["match"])
        return http_json(self.urls["api"]+"/query?"+urlencode(params),token=self.config["read_token"])

    def points(self,metric,**options):
        return [point for series in self.query(metric,**options)["series"] for point in series["points"]]

    def wait(self,predicate,timeout=20):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            try:
                value=predicate()
                if value:
                    return value
            except (OSError,ValueError):
                pass
            time.sleep(0.1)
        raise RuntimeError("lab timed out; inspect isolated process logs")

    def stop(self,name):
        process=self.processes.pop(name,None)
        if process:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def close(self):
        for name in list(self.processes):
            self.stop(name)
        self.queue.close()
        for output in self.logs:
            output.close()

    def __enter__(self):
        try:
            return self.start()
        except BaseException:
            self.close()
            raise

    def __exit__(self,*args):
        self.close()
