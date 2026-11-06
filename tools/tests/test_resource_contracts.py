"""Cross-chapter resource contracts using the real adapters and injected failures."""

from concurrent.futures import ThreadPoolExecutor
import importlib
import inspect
from pathlib import Path
import socket
import sqlite3
import sys
import threading
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
for project in ROOT.iterdir():
    if (project / "pyproject.toml").is_file():
        sys.path.insert(0, str(project / "src"))

HTTP_PACKAGES = [p.parent.name for p in sorted(ROOT.glob("*/src/*/http.py"))]
DB_PACKAGES = [
    "ad_click",
    "digital_wallet",
    "distributed_email",
    "gaming_leaderboard",
    "hotel_reservation",
    "object_storage",
    "payment_system",
    "scale_from_zero",
    "stock_exchange",
    "metrics_monitor",
]


@pytest.mark.parametrize("package", HTTP_PACKAGES)
def test_partial_header_connection_expires(package):
    module = importlib.import_module(package + ".http")
    available = dict(
        role="test",
        config={"owners": {}},
        store=None,
        host="127.0.0.1",
        port=0,
        service=None,
        token="test-token-only",
        drive=None,
        index=0,
        gate=threading.RLock(),
    )
    arguments = {
        name: available[name]
        for name, param in inspect.signature(module.server).parameters.items()
        if param.default is inspect.Parameter.empty or name in ("host", "port")
    }
    server = module.server(**arguments)
    assert 0 < server.RequestHandlerClass.timeout <= 10
    server.RequestHandlerClass.timeout = 0.05
    server.timeout = 1
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    try:
        with socket.create_connection(server.server_address, timeout=1) as client:
            client.sendall(b"GET / HTTP/1.1\r\nX-Unfinished: ")
            assert client.recv(1) == b""
    finally:
        thread.join(2)
        server.server_close()
    assert not thread.is_alive()


@pytest.mark.parametrize("package", DB_PACKAGES)
def test_database_setup_failure_closes_connection(package, tmp_path, monkeypatch):
    module = importlib.import_module(package + ".common")
    connection = Mock()
    connection.execute.side_effect = sqlite3.OperationalError("injected PRAGMA failure")
    monkeypatch.setattr(module.sqlite3, "connect", lambda *args, **kwargs: connection)
    database = module.Database(tmp_path / "test.sqlite")
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        if hasattr(database, "connection"):
            with database.connection():
                pass
        elif package == "metrics_monitor":
            database.connect()
        else:
            with database.transaction():
                pass
    connection.close.assert_called_once()


@pytest.mark.parametrize(
    "module_name", ["maps_service.store", "metrics_monitor.common"]
)
def test_slow_cache_miss_does_not_block_independent_hit(module_name):
    cache = importlib.import_module(module_name).Cache()
    cache.get("warm", lambda: 42)
    entered, release = threading.Event(), threading.Event()

    def slow():
        entered.set()
        assert release.wait(3)
        return 7

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(cache.get, "cold", slow)
        try:
            assert entered.wait(1)
            assert pool.submit(cache.get, "warm", lambda: -1).result(1) == 42
        finally:
            release.set()
        assert future.result(2) == 7
