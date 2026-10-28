"""Bounded replication and verified fallback reads of immutable chunks."""

from concurrent.futures import TimeoutError, wait

from .common import CHUNK, Error, checksum, sha


class ChunkIO:
    def __init__(self, config, runtime, rpc):
        self.config, self.runtime, self.rpc = config, runtime, rpc

    def write(self, data):
        if not isinstance(data, bytes) or not 0 < len(data) <= CHUNK:
            raise ValueError("chunk requires 1..262144 immutable bytes")
        key = sha(data)
        deadline, cancel = self.runtime.context()

        def put(url):
            try:
                result = self.rpc(
                    url + "/chunks/" + key,
                    self.config["internal_token"],
                    data,
                    "PUT",
                    deadline=deadline,
                    cancel=cancel,
                )
                return (
                    isinstance(result, dict)
                    and result.get("hash") == key
                    and type(result.get("size")) is int
                    and result["size"] == len(data)
                )
            except (Error, OSError, ValueError):
                return False

        futures = [
            self.runtime.pool.submit(put, url) for url in self.config["data_urls"]
        ]
        try:
            acknowledgements = sum(
                future.result(timeout=self.runtime.remaining(deadline, cancel))
                for future in futures
            )
        except TimeoutError:
            raise Error(503, "replication deadline exceeded") from None
        finally:
            for future in futures:
                future.cancel()
            # Retain request/GC ownership until every accepted replica RPC has
            # stopped. Real transport enforces the shared socket deadline.
            wait(futures)
        if acknowledgements < self.config.get("write_quorum", 2):
            raise Error(503, "insufficient durable data replicas; object not published")
        return {"hash": key, "size": len(data)}

    def read(self, key):
        checksum(key)
        for url in self.config["data_urls"]:
            try:
                data = self.rpc(
                    url + "/chunks/" + key, self.config["internal_token"], raw=True
                )
                if (
                    isinstance(data, bytes)
                    and 0 < len(data) <= CHUNK
                    and sha(data) == key
                ):
                    return data
            except (Error, OSError, ValueError):
                pass
        raise Error(503, "no verified replica available")

    def store(self, data):
        if not isinstance(data, bytes) or len(data) > 64 * 1024 * 1024:
            raise ValueError("object requires at most 64 MiB immutable bytes")
        return [
            self.write(data[offset : offset + CHUNK])
            for offset in range(0, len(data), CHUNK)
        ]
