from collections import OrderedDict
import copy
import time

from .common import integer


class Cache:
    def __init__(self,capacity=32,ttl=2,clock=time.monotonic):
        self.capacity=integer(capacity,1,1000)
        if not 0<ttl<=3600:
            raise ValueError("TTL must be 0..3600 seconds")
        self.ttl,self.clock=ttl,clock
        self.values=OrderedDict()
        self.hits=self.misses=self.evictions=0

    def get(self,key,min_version=0):
        old=self.values.get(key)
        if old and old[0]>self.clock() and old[1].get("version",0)>=min_version:
            self.values.move_to_end(key)
            self.hits+=1
            return copy.deepcopy(old[1])
        if old and old[0]<=self.clock():
            self.values.pop(key)
        self.misses+=1
        return None

    def put(self,key,value):
        self.values[key]=(self.clock()+self.ttl,copy.deepcopy(value))
        self.values.move_to_end(key)
        if len(self.values)>self.capacity:
            self.values.popitem(last=False)
            self.evictions+=1

    def delete(self,key):
        self.values.pop(key,None)

    def status(self):
        return {"entries":len(self.values),"hits":self.hits,"misses":self.misses,"evictions":self.evictions,"capacity":self.capacity,"ttl_seconds":self.ttl}
