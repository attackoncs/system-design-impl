import base64
import hmac
import re
import socketserver
import threading

from .common import Error
from .mail import local_user
from .spool import Spool  # Backward-compatible public import.

__all__ = ["Spool", "smtp_server"]


def smtp_server(config, spool, host, port, *, max_connections=32):
    if type(max_connections) is not int or not 1 <= max_connections <= 1024:
        raise ValueError("SMTP connection limit must be in 1..1024")

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                self.session()
            except OSError:
                # Idle, disconnected and deadline-expired sockets release their
                # capacity through the ordinary request-thread cleanup path.
                return

        def session(self):
            self.connection.settimeout(5)
            self.wfile.write(b"220 local.test local-only SMTP reference\r\n")
            authenticated, sender, recipients = False, None, []
            while True:
                line = self.rfile.readline(65537)
                if not line or len(line) > 65536:
                    return
                try:
                    if not line.endswith(b"\r\n"):
                        raise ValueError("SMTP commands require CRLF")
                    command, _, argument = line[:-2].decode("ascii").partition(" ")
                    command = command.upper()
                    response = b"500 unsupported command\r\n"
                    if command in ("EHLO", "HELO"):
                        sender, recipients = None, []
                        response = (
                            b"250-local.test\r\n250-AUTH PLAIN\r\n250 SIZE 8388608\r\n"
                        )
                    elif command == "AUTH":
                        authenticated, sender, recipients = False, None, []
                        fields = argument.split()
                        if len(fields) != 2 or fields[0].upper() != "PLAIN":
                            raise ValueError("AUTH PLAIN initial response required")
                        decoded = base64.b64decode(fields[1], validate=True).split(
                            b"\x00"
                        )
                        authenticated = (
                            len(decoded) == 3
                            and decoded[0] in (b"", b"local-worker")
                            and decoded[1] == b"local-worker"
                            and hmac.compare_digest(
                                decoded[2], config["smtp_token"].encode()
                            )
                        )
                        response = (
                            b"235 authenticated\r\n"
                            if authenticated
                            else b"535 unauthorized\r\n"
                        )
                    elif command == "QUIT":
                        self.wfile.write(b"221 bye\r\n")
                        return
                    elif command == "RSET":
                        sender, recipients = None, []
                        response = b"250 reset\r\n"
                    elif not authenticated:
                        response = b"530 authentication required\r\n"
                    elif command in ("MAIL", "RCPT"):
                        prefix = "FROM" if command == "MAIL" else "TO"
                        if command == "MAIL":
                            sender, recipients = None, []
                        match = re.fullmatch(
                            prefix
                            + r":<([^<>]+)>"
                            + (r"(?: SIZE=\d+)?" if command == "MAIL" else ""),
                            argument,
                            re.I,
                        )
                        if not match:
                            raise ValueError("invalid envelope")
                        value = match[1]
                        local_user(value, config)
                        if command == "MAIL":
                            sender, recipients = value, []
                        elif sender is None or len(recipients) >= 10:
                            raise ValueError("invalid recipient sequence/count")
                        elif value not in recipients:
                            recipients.append(value)
                        response = b"250 accepted\r\n"
                    elif command == "DATA":
                        if not sender or not recipients:
                            raise ValueError("MAIL and RCPT required")
                        self.wfile.write(b"354 end with dot\r\n")
                        chunks, total = [], 0
                        while True:
                            chunk = self.rfile.readline(65537)
                            if not chunk:
                                return
                            if chunk == b".\r\n":
                                break
                            if chunk.startswith(b".."):
                                chunk = chunk[1:]
                            total += len(chunk)
                            if total > 8 * 1024 * 1024 or len(chunk) > 65536:
                                self.wfile.write(b"552 message too large\r\n")
                                return
                            chunks.append(chunk)
                        envelope_sender, envelope_recipients = sender, recipients
                        # A completed DATA transaction always consumes its envelope,
                        # including invalid messages and temporary acceptance failures.
                        sender, recipients = None, []
                        _, lost = spool.accept(
                            config,
                            b"".join(chunks),
                            envelope_sender,
                            envelope_recipients,
                        )
                        response = (
                            b"451 simulated ACK lost after durable accept\r\n"
                            if lost
                            else b"250 durably queued\r\n"
                        )
                    self.wfile.write(response)
                except ValueError:
                    self.wfile.write(b"550 invalid local message or envelope\r\n")
                except Error as exc:
                    self.wfile.write(
                        b"451 temporary local failure\r\n"
                        if exc.status >= 500
                        else b"550 invalid local message or envelope\r\n"
                    )
                except OSError:
                    return
                except Exception:
                    self.wfile.write(b"451 temporary local failure\r\n")

    class SMTPServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = False
        block_on_close = True
        request_queue_size = max_connections

        def __init__(self, *args):
            self.capacity = threading.BoundedSemaphore(max_connections)
            super().__init__(*args)

        def process_request(self, request, client_address):
            if not self.capacity.acquire(blocking=False):
                try:
                    request.sendall(b"421 local SMTP capacity exhausted\r\n")
                finally:
                    self.shutdown_request(request)
                return
            try:
                super().process_request(request, client_address)
            except BaseException:
                self.capacity.release()
                raise

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                self.capacity.release()

    return SMTPServer((host, port), Handler)
