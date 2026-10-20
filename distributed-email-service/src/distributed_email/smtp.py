import base64
from email import policy
from email.parser import BytesParser
import hmac
import json
import re
import socketserver
import threading
import time

from .common import Database, Error, digest, dumps, http_json, shard_for
from .mail import local_user,parse_received
from .objects import Objects


class Spool(Database):
    def __init__(self,path):
        super().__init__(path)
        self.initialize("""
        CREATE TABLE IF NOT EXISTS spool(id TEXT PRIMARY KEY,hash TEXT,raw TEXT,sender TEXT,recipients TEXT,
            status TEXT,attempts INTEGER,due REAL,error TEXT);
        CREATE TABLE IF NOT EXISTS control(id INTEGER PRIMARY KEY CHECK(id=1),lose_ack INTEGER,scanner_down INTEGER);
        INSERT OR IGNORE INTO control VALUES(1,0,0);
        """)

    def accept(self,config,raw,sender,recipients):
        delivery=str(BytesParser(policy=policy.default).parsebytes(raw,headersonly=True).get("X-Delivery-ID",""))
        if not re.fullmatch(r"[a-f0-9]{64}",delivery):
            raise ValueError("local transport requires stable X-Delivery-ID")
        key=Objects(config).put(raw)["key"]
        fingerprint=digest([key,sender,recipients])
        with self.transaction(True) as db:
            prior=db.execute("SELECT hash FROM spool WHERE id=?",(delivery,)).fetchone()
            if prior and prior[0]!=fingerprint:
                raise Error(409,"SMTP delivery ID payload conflict")
            db.execute("INSERT OR IGNORE INTO spool VALUES(?,?,?,?,?,'pending',0,?,'')",(delivery,fingerprint,key,sender,dumps(recipients),time.time()))
            lost=db.execute("SELECT lose_ack FROM control").fetchone()[0]
        return delivery,lost

    def control(self,body=None):
        with self.transaction(True) as db:
            if body is not None:
                if not isinstance(body,dict) or set(body)!={"lose_ack","scanner_down"} or any(type(v) is not bool for v in body.values()):
                    raise ValueError("control requires boolean lose_ack,scanner_down")
                db.execute("UPDATE control SET lose_ack=?,scanner_down=?",(body["lose_ack"],body["scanner_down"]))
            return dict(db.execute("SELECT * FROM control").fetchone())

    def status(self):
        with self.transaction() as db:
            return {"unique_deliveries":db.execute("SELECT count(*) FROM spool").fetchone()[0],
                    "pending":db.execute("SELECT count(*) FROM spool WHERE status='pending'").fetchone()[0],
                    "rows":[dict(r) for r in db.execute("SELECT id,status,attempts,error FROM spool ORDER BY id LIMIT 100")]}

    def process_once(self,config):
        with self.transaction() as db:
            row=db.execute("SELECT * FROM spool WHERE status='pending' AND due<=? ORDER BY due,id LIMIT 1",(time.time(),)).fetchone()
        if not row:
            return False
        try:
            if self.control()["scanner_down"]:
                raise Error(503,"scanner unavailable; message remains in durable spool")
            recipients=json.loads(row["recipients"])
            descriptor,body=parse_received(config,Objects(config).get(row["raw"]),row["sender"],recipients,row["id"])
            for recipient in recipients:
                user=local_user(recipient,config)
                url=config["primary_urls"][shard_for(user,len(config["primary_urls"]))]
                http_json(url+"/internal/receive",config["internal_token"],{"user":user,"delivery":row["id"],"descriptor":descriptor,"body":body})
            with self.transaction(True) as db:
                db.execute("UPDATE spool SET status='done',attempts=attempts+1,error='' WHERE id=?",(row["id"],))
        except ValueError as exc:
            with self.transaction(True) as db:
                db.execute("UPDATE spool SET status='rejected',attempts=attempts+1,error=? WHERE id=?",(str(exc)[:300],row["id"]))
        except Exception as exc:
            with self.transaction(True) as db:
                db.execute("UPDATE spool SET attempts=attempts+1,due=?,error=? WHERE id=?",(time.time()+min(10,.1*2**min(row["attempts"],6)),str(exc)[:300],row["id"]))
        return True


def smtp_server(config,spool,host,port):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(15)
            self.wfile.write(b"220 local.test local-only SMTP reference\r\n")
            authenticated,sender,recipients=False,None,[]
            while True:
                line=self.rfile.readline(65537)
                if not line or len(line)>65536:
                    return
                command,_,argument=line.decode("ascii",errors="replace").strip().partition(" ")
                command=command.upper()
                try:
                    response=b"500 unsupported command\r\n"
                    if command in ("EHLO","HELO"):
                        response=b"250-local.test\r\n250-AUTH PLAIN\r\n250 SIZE 8388608\r\n"
                    elif command=="AUTH":
                        fields=argument.split()
                        if len(fields)!=2 or fields[0].upper()!="PLAIN":
                            raise ValueError("AUTH PLAIN initial response required")
                        decoded=base64.b64decode(fields[1],validate=True).split(b"\x00")
                        authenticated=len(decoded)==3 and hmac.compare_digest(decoded[2],config["smtp_token"].encode())
                        response=b"235 authenticated\r\n" if authenticated else b"535 unauthorized\r\n"
                    elif command=="QUIT":
                        self.wfile.write(b"221 bye\r\n")
                        return
                    elif command=="RSET":
                        sender,recipients=None,[]
                        response=b"250 reset\r\n"
                    elif not authenticated:
                        response=b"530 authentication required\r\n"
                    elif command in ("MAIL","RCPT"):
                        match=re.fullmatch(r"(?:FROM|TO):<([^<>]+)>(?: SIZE=\d+)?",argument,re.I)
                        if not match:
                            raise ValueError("invalid envelope")
                        value=match[1]
                        local_user(value,config)
                        if command=="MAIL":
                            sender,recipients=value,[]
                        elif sender is None or len(recipients)>=10:
                            raise ValueError("invalid recipient sequence/count")
                        elif value not in recipients:
                            recipients.append(value)
                        response=b"250 accepted\r\n"
                    elif command=="DATA":
                        if not sender or not recipients:
                            raise ValueError("MAIL and RCPT required")
                        self.wfile.write(b"354 end with dot\r\n")
                        chunks,total=[],0
                        while True:
                            chunk=self.rfile.readline(65537)
                            if not chunk:
                                return
                            if chunk==b".\r\n":
                                break
                            if chunk.startswith(b".."):
                                chunk=chunk[1:]
                            total+=len(chunk)
                            if total>8*1024*1024 or len(chunk)>65536:
                                self.wfile.write(b"552 message too large\r\n")
                                return
                            chunks.append(chunk)
                        _,lost=spool.accept(config,b"".join(chunks),sender,recipients)
                        response=b"451 simulated ACK lost after durable accept\r\n" if lost else b"250 durably queued\r\n"
                        sender,recipients=None,[]
                    self.wfile.write(response)
                except (ValueError,Error):
                    self.wfile.write(b"550 invalid local message or envelope\r\n")
                except Exception:
                    self.wfile.write(b"451 temporary local failure\r\n")

    class SMTPServer(socketserver.ThreadingTCPServer):
        allow_reuse_address=True
        daemon_threads=True

    return SMTPServer((host,port),Handler)
