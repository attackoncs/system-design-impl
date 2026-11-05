"""Corrupt persisted source rows remain unavailable without partial reads/repair."""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ad_click.common import Error
from ad_click.log import EventLog


class AggregationSourceReviewTests(unittest.TestCase):
    def test_deep_persisted_json_fails_as_storage_unavailable_after_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.sqlite"
            log = EventLog(path, partitions=1, clock=lambda: 1000)
            log.append(
                [
                    {
                        "event_id": str(index),
                        "ad_id": "hot",
                        "click_timestamp": 601,
                        "user_id": "u",
                        "ip": "127.0.0.1",
                        "country": "US",
                    }
                    for index in range(2)
                ]
            )
            payload = "[" * 4000 + "0" + "]" * 4000
            self.assertLess(len(payload.encode()), 8192)
            with log.transaction(True) as db:
                db.execute("UPDATE events SET payload=? WHERE seq=2", (payload,))
            for source in (log, EventLog(path, partitions=1, clock=lambda: 1000)):
                with self.assertRaises(Error) as caught:
                    source.read(0, 0)
                self.assertEqual(caught.exception.status, 503)
                self.assertEqual(source.status()["heads"], {"0": 2})
                with source.transaction() as db:
                    self.assertEqual(
                        db.execute("SELECT payload FROM events WHERE seq=2").fetchone()[
                            0
                        ],
                        payload,
                    )


if __name__ == "__main__":
    unittest.main()
