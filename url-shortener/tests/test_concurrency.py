"""Concurrent shortening retains idempotency across shared storage clients."""
from concurrent.futures import ThreadPoolExecutor
import time

from url_shortener.shortener import URLShortener
from url_shortener.storage import InMemoryStorage


def test_two_clients_share_atomic_creation():
    class Strategy:
        calls = 0
        def generate(self, url, storage):
            self.calls += 1
            time.sleep(0.01)
            return str(self.calls).zfill(7)
    storage, strategy = InMemoryStorage(), Strategy()
    clients = [URLShortener(strategy=strategy, storage=storage) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda i: clients[i % 2].shorten('https://example.org/a'), range(60)))
    assert len(set(results)) == 1
    assert strategy.calls == 1
