"""Transactional facade for operation, ledger, jobs and audit components."""

import time
from .common import identifier
from .persistence import ClockedDatabase
from .schema import SCHEMA
from .operations import OperationMixin
from .jobs import JobMixin
from .ledger import LedgerMixin
from .reconciliation import ReconciliationMixin
from .checkouts import CheckoutMixin


class Store(
    CheckoutMixin,
    OperationMixin,
    JobMixin,
    LedgerMixin,
    ReconciliationMixin,
    ClockedDatabase,
):
    def __init__(self, path, merchants=("seller",), clock=time.time):
        super().__init__(path, clock)
        self.initialize(SCHEMA)
        with self.transaction(True) as db:
            for merchant in tuple(merchants):
                db.execute(
                    "INSERT OR IGNORE INTO wallets VALUES(?,0,0)",
                    (identifier(merchant),),
                )
        self.initialize("""
            CREATE TABLE IF NOT EXISTS checkouts(id TEXT PRIMARY KEY,hash TEXT,payload TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS provider_versions(operation TEXT,revision INTEGER,hash TEXT,PRIMARY KEY(operation,revision));
        """)
