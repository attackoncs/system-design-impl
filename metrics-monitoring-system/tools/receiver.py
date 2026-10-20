"""Owned localhost webhook receiver with durable dedupe and lost-ACK injection."""
import argparse
from contextlib import closing
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--root",required=True)
    parser.add_argument("--port",type=int,required=True)
    parser.add_argument("--control",required=True)
    args=parser.parse_args()
    root=Path(args.root)
    root.mkdir(parents=True,exist_ok=True)
    path=root/"receiver.sqlite"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY,payload TEXT,attempts INTEGER)")
        db.commit()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def respond(self,status,value):
            payload=json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Length",str(len(payload)))
            self.send_header("Content-Type","application/json")
            self.end_headers()
            self.wfile.write(payload)
        def do_GET(self):
            db=sqlite3.connect(path)
            try:
                rows=db.execute("SELECT id,payload,attempts FROM receipts ORDER BY rowid").fetchall()
                self.respond(200,{"unique_notifications":len(rows),"attempts":sum(row[2] for row in rows),"receipts":[{"id":row[0],"payload":json.loads(row[1]),"attempts":row[2]} for row in rows]})
            finally:
                db.close()
        def do_POST(self):
            size=int(self.headers.get("Content-Length",0))
            if not 0<size<=1024*1024:
                self.respond(413,{"error":"size limit"})
                return
            payload=self.rfile.read(size).decode()
            key=self.headers.get("Idempotency-Key")
            if not key:
                self.respond(400,{"error":"idempotency key required"})
                return
            db=sqlite3.connect(path)
            try:
                with db:
                    old=db.execute("SELECT payload FROM receipts WHERE id=?",(key,)).fetchone()
                    if old and old[0]!=payload:
                        self.respond(409,{"error":"payload conflict"})
                        return
                    db.execute("INSERT INTO receipts VALUES(?,?,1) ON CONFLICT(id) DO UPDATE SET attempts=attempts+1",(key,payload))
                control=json.loads(Path(args.control).read_text())
                self.respond(500 if control.get("fail_after_commit") else 200,{"received":key})
            finally:
                db.close()
    service=ThreadingHTTPServer(("127.0.0.1",args.port),Handler)
    service.daemon_threads=True
    service.serve_forever(poll_interval=0.2)


if __name__=="__main__":
    main()
