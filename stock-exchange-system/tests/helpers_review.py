from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from lab import configuration
from stock_exchange.storage import Engine, Sequencer
from stock_exchange.market import Market


class ExchangeCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = configuration()
        self.sequencer = Sequencer(
            self.root / "sequencer.sqlite",
            self.config["settings"],
            self.config["simulation_timestamp"],
        )
        self.engine = Engine(self.root / "engine.sqlite")
        self.engine.apply(self.sequencer.events()[0])
        self.market = Market(self.root / "market.sqlite")
        self.market.replace(self.engine.raw())

    def body(
        self, name="order", side="BUY", price=100, quantity=1, symbol="AAPL", kind="new"
    ):
        body = {"request_id": "request_" + name, "order_id": name}
        if kind == "new":
            body.update(symbol=symbol, side=side, price=price, quantity=quantity)
        return body

    def submit(
        self,
        name="order",
        owner="alice",
        side="BUY",
        price=100,
        quantity=1,
        symbol="AAPL",
        kind="new",
        publish=True,
    ):
        body = self.body(name, side, price, quantity, symbol, kind)
        if kind == "cancel":
            body["request_id"] = "cancel_" + name
        result = self.sequencer.submit(owner, kind, body)
        for row in self.sequencer.events(self.engine.cursor()["seq"]):
            self.engine.apply(row)
        if publish:
            for batch in self.engine.feed(self.market.view()["seq"])["batches"]:
                self.market.apply(batch)
        return result
