"""Bound HTTP admission and retain ownership until accepted work has drained."""

from http.server import ThreadingHTTPServer
import logging
import socket
import threading
import time

from .common import integer
from .transport import Deadline, interrupt_socket, seconds


class Server(ThreadingHTTPServer):
    daemon_threads = False
    request_queue_size = 64

    def __init__(self, address, handler, *, concurrency=32, request_timeout=15):
        integer(concurrency, "HTTP concurrency", 1, 256)
        self.request_timeout = seconds(request_timeout)
        self.slots = threading.BoundedSemaphore(concurrency)
        self.stopping, self.local = threading.Event(), threading.local()
        super().__init__(address, handler)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(min(5, self.request_timeout))
        return request, address

    def process_request(self, request, address):
        if self.stopping.is_set() or not self.slots.acquire(False):
            try:
                request.settimeout(0.1)
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                request.shutdown(socket.SHUT_WR)
                request.recv(65536)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        self.local.deadline = time.monotonic() + self.request_timeout
        self.local.cancel = threading.Event()
        cancel = self.local.cancel

        def interrupt():
            cancel.set()
            interrupt_socket(request)

        try:
            with Deadline(self.local.deadline, interrupt, self.stopping):
                super().process_request_thread(request, address)
        finally:
            del self.local.deadline
            del self.local.cancel
            self.slots.release()

    def handle_error(self, request, address):
        if (
            not self.stopping.is_set()
            and not getattr(self.local, "cancel", self.stopping).is_set()
        ):
            logging.getLogger(__name__).warning("email HTTP handler failed")

    def server_close(self):
        self.stopping.set()
        super().server_close()
