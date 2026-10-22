from abc import ABC,abstractmethod
import copy
from datetime import datetime,timezone

from .common import identifier,integer


class SessionPolicy(ABC):
    @abstractmethod
    def permits(self,timestamp,settings):
        pass


class UTCSession(SessionPolicy):
    def permits(self,timestamp,settings):
        moment=datetime.fromtimestamp(timestamp,timezone.utc)
        return moment.weekday() in settings["weekdays"] and settings["start_minute"]<=moment.hour*60+moment.minute<settings["end_minute"]


class RiskPolicy(ABC):
    @abstractmethod
    def permits(self,used,quantity,maximum):
        pass


class SubmittedVolumeRisk(RiskPolicy):
    def permits(self,used,quantity,maximum):
        return used+quantity<=maximum


def validate(body,kind):
    required={"request_id","order_id"}|({"symbol","side","price","quantity"} if kind=="new" else set())
    if kind not in ("new","cancel") or not isinstance(body,dict) or set(body)!=required:
        raise ValueError("exact limit-order/cancel fields required")
    identifier(body["request_id"])
    identifier(body["order_id"])
    if kind=="new":
        identifier(body["symbol"])
        integer(body["price"],1,100_000_000)
        integer(body["quantity"],1,1_000_000)
        if body["side"] not in ("BUY","SELL"):
            raise ValueError("side must be BUY or SELL")
    return body


def book(state,symbol):
    levels={"bids":{},"asks":{}}
    for order in state["orders"].values():
        if order["symbol"]==symbol and order["status"] in ("OPEN","PARTIAL"):
            side="bids" if order["side"]=="BUY" else "asks"
            price=str(order["price"])
            levels[side][price]=levels[side].get(price,0)+order["remaining"]
    return levels


def release(state,order,reason):
    account=state["accounts"][order["owner"]]
    remaining=order["remaining"]
    if order["side"]=="BUY":
        amount=remaining*order["price"]
        account["cash"]["held"]-=amount
        account["cash"]["available"]+=amount
    else:
        position=account["positions"][order["symbol"]]
        position["held"]-=remaining
        position["available"]+=remaining
    order["status"]="CANCELED"
    order["reason"]=reason


def reduce_command(state,command):
    """Pure sequenced matching, replaying commands never emits network orders."""
    if command["kind"]=="genesis":
        if state:
            raise ValueError("duplicate genesis")
        data=command["settings"]
        accounts={owner:{"cash":{"available":value["cash"],"held":0},"positions":{symbol:{"available":value["positions"].get(symbol,0),"held":0} for symbol in data["symbols"]}} for owner,value in data["accounts"].items()}
        return {"accounts":accounts,"orders":{},"trades":[],"results":{},"daily":{},"settings":data}
    state=copy.deepcopy(state)
    owner,body,seq,stamp=command["owner"],command["body"],command["seq"],command["timestamp"]
    kind=command["kind"]
    validate(body,kind)
    day=datetime.fromtimestamp(stamp,timezone.utc).date().isoformat()
    for old in state["orders"].values():
        if old["status"] in ("OPEN","PARTIAL") and old["day"]!=day:
            release(state,old,"DAY_EXPIRED")
    result={"seq":seq,"request_id":body["request_id"],"order_id":body["order_id"],"accepted":False}
    def reject(reason):
        result["reason"]=reason
        state["results"][str(seq)]=result
        return state
    if kind=="cancel":
        old=state["orders"].get(body["order_id"])
        if not old or old["owner"]!=owner:
            return reject("OWNED_ORDER_NOT_FOUND")
        if old["status"] in ("OPEN","PARTIAL"):
            release(state,old,"USER_CANCEL")
        elif old["status"]!="CANCELED":
            return reject("ORDER_ALREADY_FILLED")
        result.update(accepted=True,status=old["status"])
        state["results"][str(seq)]=result
        return state
    if body["order_id"] in state["orders"]:
        return reject("ORDER_ID_EXISTS")
    symbol,quantity,price=body["symbol"],body["quantity"],body["price"]
    settings=state["settings"]
    if symbol not in settings["symbols"]:
        return reject("UNKNOWN_SYMBOL")
    if not UTCSession().permits(stamp,settings["session"]):
        return reject("SESSION_CLOSED")
    daily_key=owner+":"+symbol+":"+day
    if not SubmittedVolumeRisk().permits(state["daily"].get(daily_key,0),quantity,settings["daily_limit"]):
        return reject("DAILY_SUBMITTED_VOLUME_LIMIT")
    account=state["accounts"][owner]
    buy=body["side"]=="BUY"
    funds=account["cash"] if buy else account["positions"][symbol]
    reserve=quantity*price if buy else quantity
    if funds["available"]<reserve:
        return reject("INSUFFICIENT_CASH" if buy else "INSUFFICIENT_SHARES")
    funds["available"]-=reserve
    funds["held"]+=reserve
    state["daily"][daily_key]=state["daily"].get(daily_key,0)+quantity
    order={**body,"owner":owner,"seq":seq,"timestamp":stamp,"day":day,"remaining":quantity,"filled":0,"status":"OPEN","reason":""}
    state["orders"][body["order_id"]]=order
    opposite=[row for row in state["orders"].values() if row["symbol"]==symbol and row["side"]!=body["side"] and row["status"] in ("OPEN","PARTIAL") and (row["price"]<=price if buy else row["price"]>=price)]
    opposite.sort(key=lambda row:(row["price"] if buy else -row["price"],row["seq"]))
    for maker in opposite:
        if not order["remaining"]:
            break
        if maker["owner"]==owner:
            release(state,order,"SELF_TRADE_PREVENTION")
            break
        count=min(order["remaining"],maker["remaining"])
        execution_price=maker["price"]
        bid,ask=(order,maker) if buy else (maker,order)
        buyer,seller=state["accounts"][bid["owner"]],state["accounts"][ask["owner"]]
        buyer["cash"]["held"]-=bid["price"]*count
        buyer["cash"]["available"]+=(bid["price"]-execution_price)*count
        seller["cash"]["available"]+=execution_price*count
        seller["positions"][symbol]["held"]-=count
        buyer["positions"][symbol]["available"]+=count
        trade_id="trade_"+str(seq)+"_"+str(sum(trade["seq"]==seq for trade in state["trades"])+1)
        state["trades"].append({"id":trade_id,"seq":seq,"symbol":symbol,"price":execution_price,"quantity":count,"timestamp":stamp,"buy_order":bid["order_id"],"sell_order":ask["order_id"],"buyer":bid["owner"],"seller":ask["owner"]})
        for row in (order,maker):
            row["remaining"]-=count
            row["filled"]+=count
            row["status"]="FILLED" if not row["remaining"] else "PARTIAL"
    result.update(accepted=True,status=order["status"])
    state["results"][str(seq)]=result
    return state


def audit(state):
    settings=state["settings"]
    errors=[]
    cash=sum(sum(account["cash"].values()) for account in state["accounts"].values())
    expected_cash=sum(value["cash"] for value in settings["accounts"].values())
    if cash!=expected_cash:
        errors.append("cash supply mismatch")
    positions={}
    for symbol in settings["symbols"]:
        positions[symbol]=sum(sum(account["positions"][symbol].values()) for account in state["accounts"].values())
        if positions[symbol]!=sum(value["positions"].get(symbol,0) for value in settings["accounts"].values()):
            errors.append("share supply mismatch: "+symbol)
    for owner,account in state["accounts"].items():
        open_orders=[row for row in state["orders"].values() if row["owner"]==owner and row["status"] in ("OPEN","PARTIAL")]
        if account["cash"]["held"]!=sum(row["remaining"]*row["price"] for row in open_orders if row["side"]=="BUY"):
            errors.append("cash reservation mismatch: "+owner)
        for symbol,balance in account["positions"].items():
            if min(balance.values())<0 or balance["held"]!=sum(row["remaining"] for row in open_orders if row["side"]=="SELL" and row["symbol"]==symbol):
                errors.append("share reservation mismatch: "+owner)
        if min(account["cash"].values())<0:
            errors.append("negative cash: "+owner)
    return {"matches":not errors,"errors":errors,"cash_supply":cash,"share_supply":positions}
