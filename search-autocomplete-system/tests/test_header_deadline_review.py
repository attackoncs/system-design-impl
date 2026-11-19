"""Request parsing deadlines must release capacity without timing out service work."""

import concurrent.futures
import contextlib
import http.client
import json
import socket
import threading

import pytest
from search_autocomplete import Autocomplete, Store
from search_autocomplete.http import server


@contextlib.contextmanager
def running(service, timeout=0.2):
    httpd = server(service)
    httpd.RequestHandlerClass.timeout = timeout
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def query(httpd):
    with contextlib.closing(
        http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=2)
    ) as client:
        client.request("GET", "/suggest?prefix=a")
        with client.getresponse() as response:
            return response.status, json.load(response)["suggestions"]


@pytest.fixture
def service():
    with contextlib.closing(Store()) as store:
        store.record("one", "alpha", 1)
        store.build(0, 2)
        yield Autocomplete(store)


@pytest.mark.parametrize(
    "partial",
    [
        b"GET /suggest?prefix=a HTTP/1.1",
        b"GET /suggest?prefix=a HTTP/1.1\r\nHost: localhost\r\nX-Slow: ",
    ],
    ids=["request-line", "headers"],
)
def test_trickled_request_parsing_releases_its_slot(service, partial):
    entered, completed, stop = (threading.Event() for _ in range(3))
    served = threading.Event()
    original_suggest = service.suggest

    def suggest(*args, **kwargs):
        served.set()
        return original_suggest(*args, **kwargs)

    service.suggest = suggest
    with running(service) as httpd:
        httpd.slots = threading.BoundedSemaphore(1)
        handler = httpd.RequestHandlerClass
        original_setup = handler.setup
        original_process = httpd.process_request_thread

        def setup(self):
            original_setup(self)
            entered.set()

        def process(request, address):
            try:
                original_process(request, address)
            finally:
                completed.set()

        handler.setup = setup
        httpd.process_request_thread = process
        with socket.create_connection(
            ("127.0.0.1", httpd.server_port), timeout=2
        ) as slow:
            slow.sendall(partial)
            assert entered.wait(timeout=1)

            def trickle():
                while not stop.wait(0.01):
                    try:
                        slow.sendall(b"x")
                    except OSError:
                        return

            writer = threading.Thread(target=trickle)
            writer.start()
            try:
                assert completed.wait(timeout=0.8), "trickled input retained its slot"
                assert not served.is_set(), (
                    "an expired partial request reached the service"
                )
                assert query(httpd) == (200, [{"query": "alpha", "frequency": 1}])
            finally:
                stop.set()
                with contextlib.suppress(OSError):
                    slow.shutdown(socket.SHUT_RDWR)
                writer.join(timeout=2)
                assert not writer.is_alive()


def test_parsed_headers_disarm_deadline_before_slow_service(service):
    entered, release, returned = (threading.Event() for _ in range(3))
    original_suggest = service.suggest

    def waiting(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=2)
        return original_suggest(*args, **kwargs)

    service.suggest = waiting
    with running(service) as httpd:

        def request():
            try:
                return query(httpd)
            finally:
                returned.set()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(request)
            try:
                assert entered.wait(timeout=1)
                assert not returned.wait(timeout=0.4)
            finally:
                release.set()
            assert pending.result(timeout=2) == (
                200,
                [{"query": "alpha", "frequency": 1}],
            )
        assert query(httpd) == (200, [{"query": "alpha", "frequency": 1}])
