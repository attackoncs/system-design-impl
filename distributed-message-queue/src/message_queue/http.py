import hmac
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json

from .common import Error,dumps


def server(host,port,service,token):
    if not isinstance(token,str) or len(token)<8:
        raise ValueError("private credential required")
    class Handler(BaseHTTPRequestHandler):
        protocol_version="HTTP/1.1"

        def log_message(self,*args):
            pass

        def do_POST(self):
            self.connection.settimeout(10)
            try:
                credential=self.headers.get("Authorization","")
                if not hmac.compare_digest(credential.encode(),("Bearer "+token).encode()):
                    raise Error(401,"credential required")
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError("Transfer-Encoding unsupported")
                size=int(self.headers.get("Content-Length","0"))
                if not 0<size<=32*1024*1024:
                    raise Error(413,"body outside reference size limit")
                body=json.loads(self.rfile.read(size))
                if not isinstance(body,dict):
                    raise ValueError("object required")
                result,status=service.dispatch(self.path,body),200
            except Error as exc:
                result,status={"error":exc.message},exc.status
            except (ValueError,TypeError,KeyError,OverflowError):
                result,status={"error":"invalid request"},400
            except OSError:
                result,status={"error":"storage or peer unavailable"},503
            except Exception:
                result,status={"error":"internal service error"},500
            payload=dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(payload)))
            self.send_header("Cache-Control","no-store")
            self.send_header("Connection","close")
            self.end_headers()
            self.close_connection=True
            try:
                self.wfile.write(payload)
            except OSError:
                pass
    httpd=ThreadingHTTPServer((host,port),Handler)
    httpd.daemon_threads=True
    return httpd
