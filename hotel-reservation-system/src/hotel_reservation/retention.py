"""Bounded receipt GC and guest-data redaction for settled reservations.

Both passes are opt-in. A reservation is eligible only when it is settled:
terminal status, terminal payment state and no unfinished charge/refund job.
Rows whose payment outcome is unknown or whose compensation is pending are
never touched, however old they are.

* Receipt GC clears the idempotency receipt (request_key + payload hash) and
  deletes the finished job rows after ``receipt_retention_seconds`` from
  creation; that is the documented client retry window. The reservation row,
  nightly prices, amounts and inventory effects stay for accounting. A later
  retry with the same key derives the same deterministic reservation ID and is
  rejected with 409 instead of creating a second order (see Bookings.reserve).
* Privacy redaction replaces the guest identity with ``REDACTED_USER`` and
  drops the receipt once the stay ended more than ``privacy_retention_days``
  ago. Inventory, events and the payment ledger never contain guest identity.
"""

from datetime import datetime, timedelta, timezone

from .common import integer

REDACTED_USER = "[redacted]"  # Not a valid identifier, so no guest can match it.

SETTLED = """status IN ('CONFIRMED','CANCELLED','EXPIRED','REJECTED')
    AND payment IN ('NONE','PAID','DECLINED','REFUNDED')
    AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.status!='done'
        AND jobs.id IN ('charge:'||bookings.id,'refund:'||bookings.id))"""


def validate(config):
    """Return (receipt_seconds, privacy_days, interval, batch); None disables."""
    receipt = config.get("receipt_retention_seconds")
    privacy = config.get("privacy_retention_days")
    if receipt is not None:
        integer(receipt, "receipt_retention_seconds", 3600, 3650 * 86400)
    if privacy is not None:
        integer(privacy, "privacy_retention_days", 1, 3650)
    interval = integer(config.get("retention_interval", 60), "retention_interval", 1, 3600)
    batch = integer(config.get("retention_batch", 100), "retention_batch", 1, 1000)
    return receipt, privacy, interval, batch


class Retention:
    def __init__(self, store, receipt_seconds=None, privacy_days=None, batch=100):
        if receipt_seconds is not None:
            integer(receipt_seconds, "receipt_retention_seconds", 3600, 3650 * 86400)
        if privacy_days is not None:
            integer(privacy_days, "privacy_retention_days", 1, 3650)
        self.store, self.batch = store, integer(batch, "retention_batch", 1, 1000)
        self.receipt_seconds, self.privacy_days = receipt_seconds, privacy_days

    def run_once(self):
        """One bounded batch per pass; ``pending`` means another batch may be due."""
        with self.store.transaction(True) as db:
            now = self.store._now()
            receipts = redacted = 0
            if self.receipt_seconds is not None:
                # Partial index receipt_gc: (created,id) WHERE hash IS NOT NULL.
                ids = [
                    row[0]
                    for row in db.execute(
                        f"""SELECT id FROM bookings INDEXED BY receipt_gc
                        WHERE hash IS NOT NULL AND created<=? AND {SETTLED}
                        ORDER BY created,id LIMIT ?""",
                        (now - self.receipt_seconds, self.batch),
                    )
                ]
                receipts = self._drop_receipts(db, ids)
            if self.privacy_days is not None:
                today = datetime.fromtimestamp(now, timezone.utc).date()
                cutoff = (today - timedelta(days=self.privacy_days)).isoformat()
                # Partial index privacy_gc: (end,id) WHERE user!='[redacted]'.
                ids = [
                    row[0]
                    for row in db.execute(
                        f"""SELECT id FROM bookings INDEXED BY privacy_gc
                        WHERE user!='[redacted]' AND end<? AND {SETTLED}
                        ORDER BY end,id LIMIT ?""",
                        (cutoff, self.batch),
                    )
                ]
                self._drop_receipts(db, ids)
                db.executemany(
                    "UPDATE bookings SET user=? WHERE id=?",
                    [(REDACTED_USER, booking) for booking in ids],
                )
                redacted = len(ids)
            if receipts or redacted:
                self.store.changed(db)
        return {
            "receipts_collected": receipts,
            "guests_redacted": redacted,
            "pending": receipts == self.batch or redacted == self.batch,
        }

    @staticmethod
    def _drop_receipts(db, ids):
        db.executemany(
            "UPDATE bookings SET request_key=NULL,hash=NULL WHERE id=?",
            [(booking,) for booking in ids],
        )
        db.executemany(
            "DELETE FROM jobs WHERE id IN (?,?) AND status='done'",
            [("charge:" + booking, "refund:" + booking) for booking in ids],
        )
        return len(ids)
