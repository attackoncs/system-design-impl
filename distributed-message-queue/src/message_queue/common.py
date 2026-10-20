import hashlib
import json
import re
from urllib.request import Request,urlopen

ZERO="0"*64


class Error(Exception):
    def __init__(self,status,message):
        self.status,self.message=status,message
        super().__init__(message)


def dumps(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False)


def name(value):
    if not isinstance(value,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}",value):
        raise ValueError("invalid identifier")
    return value


def integer(value,minimum=0,maximum=2**53-1):
    if type(value) is not int or not minimum<=value<=maximum:
        raise ValueError("invalid integer")
    return value


def envelope(record,previous):
    digest=hashlib.sha256((previous+dumps(record)).encode()).hexdigest()
    return {"record":record,"previous":previous,"hash":digest}


def rpc(url,path,body,token,timeout=1):
    request=Request(url.rstrip("/")+path,data=dumps(body).encode(),headers={"Authorization":"Bearer "+token,"Content-Type":"application/json"},method="POST")
    with urlopen(request,timeout=timeout) as response:
        payload=response.read(32*1024*1024+1)
        if len(payload)>32*1024*1024:
            raise ValueError("reference RPC response exceeds 32 MiB")
        return json.loads(payload)
