"""Disposable localhost TCP fault proxy. Contains no queue behavior."""
import argparse
import json
from pathlib import Path
import select
import socket
import socketserver


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--port",type=int,required=True)
    parser.add_argument("--target",type=int,required=True)
    parser.add_argument("--control",required=True)
    args=parser.parse_args()
    def blocked():
        try:
            return json.loads(Path(args.control).read_text()).get("blocked",False)
        except (OSError,ValueError):
            return True
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            if blocked():
                return
            try:
                with socket.create_connection(("127.0.0.1",args.target),timeout=1) as remote:
                    streams=[self.request,remote]
                    while not blocked():
                        readable,_,_=select.select(streams,[],[],0.05)
                        for source in readable:
                            data=source.recv(65536)
                            if not data:
                                return
                            (remote if source is self.request else self.request).sendall(data)
            except OSError:
                return
    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address=True
        daemon_threads=True
    with Server(("127.0.0.1",args.port),Handler) as service:
        service.serve_forever(poll_interval=0.2)


if __name__=="__main__":
    main()
