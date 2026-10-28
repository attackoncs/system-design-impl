"""Real SQLite participants and replicas with an in-process RPC adapter."""

from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from lab import configuration
from digital_wallet.coordinator import Coordinator
from digital_wallet.journal import Replica
from digital_wallet.shard import Shard


class AuthorityCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = configuration()
        self.replicas = {
            url: Replica(self.root / (str(i) + ".sqlite"))
            for i, url in enumerate(self.config["replicas"])
        }
        self.patch = patch(
            "digital_wallet.journal.request", side_effect=self.replica_rpc
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.store = Coordinator(
            self.root / "coordinator.sqlite", self.config, clock=lambda: 100
        )
        self.addCleanup(self.store.close)
        self.shards = [
            Shard(
                self.root / ("shard" + str(i) + ".sqlite"),
                i,
                2,
                {key: row["opening"] for key, row in self.config["accounts"].items()},
            )
            for i in range(2)
        ]
        self.store.shard = self.shard_rpc
        self.source = next(k for k in self.config["accounts"] if k.startswith("alice_"))
        self.peer = next(k for k in self.config["accounts"] if k.startswith("peer_"))
        self.destination = next(
            k for k in self.config["accounts"] if k.startswith("bob_")
        )

    def replica_rpc(self, url, token, body=None, **kwargs):
        parsed = urlsplit(url)
        replica = self.replicas["http://" + parsed.netloc]
        if parsed.path == "/internal/status":
            return replica.status(body["epoch"], body["leader"])
        return replica.append(body["entry"], body["epoch"], body["leader"])

    def shard_rpc(self, index, path, body=None):
        shard = self.shards[index]
        if path.startswith("/internal/participants/"):
            return {"participant": shard.participant(path.split("/")[-1])}
        if path == "/internal/action":
            return shard.action(
                body["intent"],
                body["kind"],
                body["role"],
                body["epoch"],
                body["leader"],
            )
        if path == "/internal/view":
            return shard.view()
        if path == "/internal/audit":
            return shard.audit()
        if path.startswith("/internal/accounts/"):
            return shard.balance(path.split("/")[-1])
        raise AssertionError(path)

    def body(self, name="transfer", amount=100, local=False):
        return {
            "transaction_id": name,
            "from_account": self.source,
            "to_account": self.peer if local else self.destination,
            "amount": amount,
            "currency": "USD",
        }

    def finish(self, name="transfer"):
        for _ in range(30):
            if self.store.owned(name, "alice")["status"] != "PENDING":
                return self.store.owned(name, "alice")
            self.store.step()
        self.fail("transfer did not finish")
